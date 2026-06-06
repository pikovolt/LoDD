from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .task_parser import StructureType, TaskDefinition, get_iterations_dir


CANONICAL_DEBT_MARKER_FIELDS = (
    "unreviewed_functions",
    "boundary_issues",
    "knowledge_gaps",
)


@dataclass
class FailureDelta:
    error_summary: str
    result_state: str = ""
    command: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    changed_files: list[str] = field(default_factory=list)
    agent_changed_files: list[str] = field(default_factory=list)
    runner_metadata_files: list[str] = field(default_factory=list)
    boundary_violations: list[str] = field(default_factory=list)
    interface_findings: list[str] = field(default_factory=list)
    tbd_markers: list[str] = field(default_factory=list)
    dependency_findings: list[str] = field(default_factory=list)
    materialized_conflicts: list[str] = field(default_factory=list)
    next_delta_instruction: str = ""

    def __post_init__(self) -> None:
        if self.changed_files and not self.agent_changed_files:
            self.agent_changed_files = list(self.changed_files)
        elif self.agent_changed_files and not self.changed_files:
            self.changed_files = list(self.agent_changed_files)


def record_iteration(
    task: TaskDefinition,
    repo_root: Path,
    error_summary: str | FailureDelta,
    violation_log: str = "",
) -> Path:
    delta = (
        error_summary
        if isinstance(error_summary, FailureDelta)
        else FailureDelta(error_summary=error_summary)
    )
    if violation_log and not delta.boundary_violations:
        delta.boundary_violations = violation_log.splitlines()

    iter_dir = get_iterations_dir(task, repo_root)
    iter_dir.mkdir(parents=True, exist_ok=True)
    iter_file = iter_dir / f"iter-{task.iteration.current_iteration:02d}.md"
    iter_rel = _relative_to_repo(iter_file, repo_root)
    delta.runner_metadata_files = _dedupe_strings([*delta.runner_metadata_files, iter_rel])
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    next_step = (
        delta.next_delta_instruction
        or "Review the failure and update current_delta_instruction before retrying if needed."
    )
    content = f"""# Iteration {task.iteration.current_iteration} - {task.task_id}

## Timestamp
{timestamp}

## Result State
{delta.result_state or "unknown"}

## Error Summary
{delta.error_summary}

## Failing Command
{delta.command or "None"}

## Stdout Tail
{delta.stdout_tail or "None"}

## Stderr Tail
{delta.stderr_tail or "None"}

## Agent Changed Files
{_format_list(delta.agent_changed_files)}

## Runner Metadata Files
{_format_list(delta.runner_metadata_files)}

## Changed Files
Compatibility alias: this means Agent Changed Files only; runner metadata is listed separately above.

## Boundary Log
{_format_list(delta.boundary_violations)}

## LD-003 Interface Findings
{_format_list(delta.interface_findings)}

## TBD Markers
{_format_list(delta.tbd_markers)}

## LD-004 Dependency Findings
{_format_list(delta.dependency_findings)}

## Materialized Workspace Conflicts
{_format_list(delta.materialized_conflicts)}

## Next Step
{next_step}
"""
    iter_file.write_text(content, encoding="utf-8")
    print(f"[iteration] Logged to {iter_file}")
    return iter_file


def update_task_iteration(
    task: TaskDefinition,
    repo_root: Path,
    error_summary: str,
    delta_instruction: str = "",
) -> Path | None:
    if task.source_path is None:
        return None

    text = task.source_path.read_text(encoding="utf-8")
    next_iteration = task.iteration.current_iteration + 1
    history = json.dumps(task.iteration.history + [error_summary], ensure_ascii=False)
    delta_instruction = delta_instruction or f"Resolve previous failure: {error_summary}"
    new_section = f"""## Iteration Control
- current_iteration: {next_iteration}
- history: {history}
- current_delta_instruction: "{delta_instruction}"
"""
    updated = _replace_or_append_section(text, "Iteration Control", new_section)
    task.source_path.write_text(updated, encoding="utf-8")
    print(f"[iteration] Updated {task.source_path} -> iteration {next_iteration}")
    return task.source_path


def mark_task_done(
    task: TaskDefinition,
    repo_root: Path,
    iterations_count: int,
    boundary_breaches: int,
    manual_follow_up: str | None = None,
    result_state: str = "green",
) -> list[Path]:
    if task.source_path is None:
        return []

    text = task.source_path.read_text(encoding="utf-8")
    text = _replace_or_append_section(text, "Status", "## Status\nDone\n")
    retro = f"""## Retrospective
- iterations: {iterations_count}
- boundary_breaches: {boundary_breaches}
- knowledge_added: 0
- result_state: {result_state}
"""
    if manual_follow_up:
        retro += f"- manual_follow_up: {manual_follow_up}\n"
    text = _replace_or_append_section(text, "Retrospective", retro)
    debt_markers = _build_debt_markers_section(_section(text, "Debt Markers"))
    text = _replace_or_append_section(text, "Debt Markers", debt_markers)
    task.source_path.write_text(text, encoding="utf-8")

    written = [task.source_path]
    if task.structure == StructureType.PHASE and task.phase_ctx:
        work_log = _append_work_log(
            task,
            f"Task {task.task_id} completed; iterations={iterations_count}, breaches={boundary_breaches}",
        )
        if work_log is not None:
            written.append(work_log)
    return written


def record_failure_to_work_log(task: TaskDefinition, error_summary: str) -> Path | None:
    if task.structure == StructureType.PHASE and task.phase_ctx:
        return _append_work_log(
            task,
            f"Task {task.task_id} iteration {task.iteration.current_iteration} failed: {error_summary}",
        )
    return None


def _append_work_log(task: TaskDefinition, entry: str) -> Path | None:
    if task.phase_ctx is None:
        return None
    path = task.phase_ctx.work_log_path
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"- [{timestamp}] {entry}\n"
    if path.exists():
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    else:
        path.write_text(f"# Work Log\n\n{line}", encoding="utf-8")
    return path


def _relative_to_repo(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _dedupe_strings(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _format_list(items: list[str]) -> str:
    if not items:
        return "None"
    return "\n".join(f"- {item}" for item in items)


def _build_debt_markers_section(existing_section: str) -> str:
    values = _parse_debt_marker_values(existing_section)
    lines = ["## Debt Markers"]
    for field_name in CANONICAL_DEBT_MARKER_FIELDS:
        value = values.get(field_name, "").strip() or "None"
        lines.append(f"- {field_name}: {value}")
    return "\n".join(lines) + "\n"


def _parse_debt_marker_values(section_text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    canonical = "|".join(re.escape(field) for field in CANONICAL_DEBT_MARKER_FIELDS)
    pattern = rf"^\s*[-*]\s*({canonical})\s*:\s*(.*?)\s*$"
    for line in section_text.splitlines():
        match = re.match(pattern, line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _section(text: str, section_name: str) -> str:
    pattern = rf"^##\s+{re.escape(section_name)}\s*\n(.*?)(?=^##\s+|\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _replace_or_append_section(text: str, section_name: str, replacement: str) -> str:
    pattern = rf"## {re.escape(section_name)}\n(?:.*\n)*?(?=\n## |\Z)"
    if re.search(pattern, text):
        return re.sub(pattern, replacement.rstrip() + "\n", text)
    return text.rstrip() + "\n\n" + replacement.rstrip() + "\n"


def build_closeout_checklist(task_path: str | Path, repo_root: str | Path) -> str:
    """Build a bounded, non-mutating closeout checklist for one task."""
    repo = Path(repo_root).resolve()
    task = __import__("lodd_runner.task_parser", fromlist=["parse_task_md"]).parse_task_md(task_path, repo)
    source = Path(task_path).resolve()
    text = source.read_text(encoding="utf-8")
    retrospective = _section(text, "Retrospective") or "None"
    debt_markers = _section(text, "Debt Markers") or "None"
    iterations_dir = get_iterations_dir(task, repo)
    records = _read_iteration_record_summaries(iterations_dir)
    rel_task = _relative_to_repo(source, repo)
    rel_iter = _relative_to_repo(iterations_dir, repo)

    lines = [
        f"# Iteration Closeout Checklist: {task.task_id}",
        "",
        "## Task Identity",
        f"- task: {rel_task}",
        f"- title: {task.title}",
        f"- structure: {task.structure.value}",
        f"- status: {task.status}",
        "",
        "## Retrospective",
        *(_format_section_lines(retrospective)),
        "",
        "## Active Debt Markers",
        *(_format_active_debt_lines(debt_markers)),
        "",
        "## Iteration Records Found",
        f"- directory: {rel_iter}",
    ]
    if not iterations_dir.is_dir():
        lines.append("- No iteration directory found.")
    elif not records:
        lines.append("- No iteration record files found.")
    else:
        for record in records:
            lines.extend([
                f"- {record['filename']}",
                f"  - result_state: {record['result_state']}",
                f"  - error_summary: {record['error_summary']}",
                f"  - boundary_findings: {record['boundary_findings']}",
                f"  - next_delta: {record['next_delta']}",
            ])
    lines.extend([
        "",
        "## Closeout Actions",
        "- Review iteration records for reusable DCC/API/library knowledge.",
        "- Move stable facts into knowledge/ or other stable project documentation when appropriate.",
        "- Move durable design decisions into decision_log.md or architecture.md when appropriate.",
        "- Confirm Debt Markers are resolved, accepted, or intentionally carried forward.",
        "- Only after transfer, manually purge the iteration logs to avoid future-chat context pollution.",
        "- Do not paste full old reasoning logs into New Chat handoff; use bounded summaries only.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def write_closeout_checklist(task_path: str | Path, repo_root: str | Path, output_path: str | Path) -> Path:
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_closeout_checklist(task_path, repo_root), encoding="utf-8")
    return target


def _read_iteration_record_summaries(iterations_dir: Path) -> list[dict[str, str]]:
    if not iterations_dir.is_dir():
        return []
    summaries: list[dict[str, str]] = []
    for path in sorted(iterations_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        summaries.append({
            "filename": path.name,
            "result_state": _bounded_inline(_section(text, "Result State") or "unknown"),
            "error_summary": _bounded_inline(_section(text, "Error Summary") or "None"),
            "boundary_findings": _bounded_inline(_first_list_value(_section(text, "Boundary Log"))),
            "next_delta": _bounded_inline(_section(text, "Next Step") or "None"),
        })
    return summaries


def _first_list_value(section_text: str) -> str:
    if not section_text.strip() or section_text.strip().lower() == "none":
        return "None"
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            return stripped[1:].strip()
    return section_text.strip()


def _bounded_inline(value: str, limit: int = 160) -> str:
    collapsed = " ".join(value.strip().split())
    if not collapsed:
        return "None"
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _format_section_lines(section_text: str) -> list[str]:
    if section_text == "None":
        return ["- None"]
    lines = []
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(stripped if stripped.startswith("-") else f"- {stripped}")
    return lines or ["- None"]


def _format_active_debt_lines(section_text: str) -> list[str]:
    if section_text == "None":
        return ["- None"]
    values = _parse_debt_marker_values(section_text)
    active = []
    for field_name in CANONICAL_DEBT_MARKER_FIELDS:
        value = values.get(field_name, "").strip()
        if value and value.lower() != "none" and not value.lower().startswith(("accepted:", "resolved:")):
            active.append(f"- {field_name}: {_bounded_inline(value)}")
    return active or ["- None"]
