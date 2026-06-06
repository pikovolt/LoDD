from __future__ import annotations

import re
from pathlib import Path

from .task_parser import TaskDefinition, get_iterations_dir, parse_task_md

DEBT_FIELDS = ("unreviewed_functions", "boundary_issues", "knowledge_gaps")


def format_new_chat_handoff(task: TaskDefinition, repo_root: Path) -> str:
    if task.source_path is None:
        raise ValueError("Task source path is required to build a handoff packet.")

    text = task.source_path.read_text(encoding="utf-8")
    sections = _split_sections(text)
    read_entries, write_entries = _raw_boundary_entries(sections.get("Context Boundary", ""))
    functional_values = _functional_contract_lines(sections.get("Functional Contract", ""), task)
    latest_failure = _latest_failure_summary(task, repo_root)
    result_state = _result_state_so_far(sections.get("Retrospective", ""), latest_failure)
    debt_values = _debt_values(sections.get("Debt Markers", ""))

    task_rel = _rel(task.source_path, repo_root)
    read_first = ["AGENTS.md", task_rel, *read_entries]

    lines = [
        f"# New Chat Handoff: {task.task_id} {task.title}",
        "",
        "## Read First",
        *_format_bullets(read_first),
        "",
        "## Task State",
        f"- status: {task.status}",
        f"- current_iteration: {task.iteration.current_iteration}",
        f"- result_state so far: {result_state}",
        "",
        "## Allowed Write Boundary",
        *_format_bullets(write_entries or task.boundary.write),
        "",
        "## Functional Contract / Done Condition",
        *functional_values,
        "",
        "## Strict Constraints",
        *_format_bullets(task.strict_constraints or ["None"]),
        "",
        "## Latest Delta",
        f"- failure_summary: {latest_failure or 'None'}",
        f"- current_delta_instruction: {task.iteration.current_delta_instruction or 'None'}",
        "",
        "## Active Debt Markers",
    ]
    for field in DEBT_FIELDS:
        lines.append(f"- {field}: {debt_values.get(field, 'None')}")
    return "\n".join(lines).rstrip() + "\n"


def build_new_chat_handoff(task_path: str | Path, repo_root: str | Path) -> str:
    repo = Path(repo_root).resolve()
    task = parse_task_md(task_path, repo)
    return format_new_chat_handoff(task, repo)


def write_new_chat_handoff(
    task_path: str | Path,
    repo_root: str | Path,
    output_path: str | Path,
) -> Path:
    """Write a compact New Chat handoff packet as a durable UTF-8 artifact."""
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_new_chat_handoff(task_path, repo_root), encoding="utf-8")
    return target


def _format_bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] if items else ["- None"]


def _split_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = match.group(1).strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _raw_boundary_entries(section: str) -> tuple[list[str], list[str]]:
    read: list[str] = []
    write: list[str] = []
    current: list[str] | None = None
    for line in section.splitlines():
        stripped = line.strip()
        if re.match(r"^-\s*Read\s*:", stripped, re.IGNORECASE):
            current = read
            continue
        if re.match(r"^-\s*Write\s*:", stripped, re.IGNORECASE):
            current = write
            continue
        match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if match and current is not None:
            current.append(match.group(1).strip())
    return read, write


def _functional_contract_lines(section: str, task: TaskDefinition) -> list[str]:
    lines = [f"- type: {task.done_condition.type}"]
    if task.done_condition.auto_tests:
        lines.append("- auto:")
        lines.extend(f"  - {path}" for path in task.done_condition.auto_tests)
    else:
        auto_inline = _find_inline_value(section, "auto")
        lines.append(f"- auto: {auto_inline or 'None'}")
    if task.done_condition.commands:
        lines.append("- commands:")
        lines.extend(f"  - {command}" for command in task.done_condition.commands)
    else:
        commands_inline = _find_inline_value(section, "commands")
        lines.append(f"- commands: {commands_inline or 'None'}")
    lines.append(f"- manual: {task.done_condition.manual_desc or 'None'}")
    return lines


def _find_inline_value(section: str, key: str) -> str:
    pattern = rf"^-?\s*{re.escape(key)}\s*:\s*(.+)$"
    for line in section.splitlines():
        match = re.match(pattern, line.strip(), re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _latest_failure_summary(task: TaskDefinition, repo_root: Path) -> str:
    history_failure = task.iteration.history[-1] if task.iteration.history else ""
    iter_dir = get_iterations_dir(task, repo_root)
    latest_file = _latest_iteration_file(iter_dir)
    if latest_file is None:
        return history_failure
    text = latest_file.read_text(encoding="utf-8")
    summary = _section(text, "Error Summary")
    if summary and summary != "None":
        return _bounded_one_line(summary)
    return history_failure


def _latest_iteration_file(iter_dir: Path) -> Path | None:
    if not iter_dir.is_dir():
        return None
    files = sorted(iter_dir.glob("iter-*.md"))
    return files[-1] if files else None


def _result_state_so_far(retro: str, latest_failure: str) -> str:
    values = _parse_key_values(retro)
    state = values.get("result_state", "").strip()
    if state:
        return state
    return "red" if latest_failure else "unknown"


def _debt_values(section: str) -> dict[str, str]:
    values = _parse_key_values(section)
    result: dict[str, str] = {}
    for field in DEBT_FIELDS:
        value = values.get(field, "None").strip() or "None"
        result[field] = value
    return result


def _parse_key_values(section: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in section.splitlines():
        match = re.match(r"^\s*-\s*([A-Za-z_-]+)\s*:\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _section(text: str, section_name: str) -> str:
    pattern = rf"^##\s+{re.escape(section_name)}\s*\n(.*?)(?=^##\s+|\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _bounded_one_line(value: str, limit: int = 300) -> str:
    line = " ".join(value.split())
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()
