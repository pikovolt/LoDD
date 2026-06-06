from __future__ import annotations

from pathlib import Path

from .task_parser import StructureType, TaskDefinition


FALLBACK_PROMPT_PROFILE = """# Runner Prompt Profile
Codex is executing one LoDD task under LoDD Runner supervision.

- Follow the task file and applicable AGENTS.md rules.
- Treat docs/runner/ as LoDD Runner product behavior.
- Treat docs/reference/LoDD_Reference.md as upstream reference material, not live task context unless explicitly listed in the task Read boundary.
- Read only declared Read entries plus Write entries and runner-injected context.
- Edit only declared Write entries.
- Do not create or require top-level interfaces/, knowledge/, or plans/ as repository management structure.
- If the task cannot proceed within the declared boundary, explain the missing boundary requirement.
- On retries, prioritize current_delta_instruction and keep work focused on the current delta.
- Leave manual validation explicit for manual or hybrid Functional Contracts.
"""


def load_prompt_profile(repo_root: Path) -> str:
    profile_path = repo_root / "docs" / "runner" / "prompt_profile.md"
    if profile_path.is_file():
        return profile_path.read_text(encoding="utf-8")
    return FALLBACK_PROMPT_PROFILE


def collect_context_files(task: TaskDefinition, repo_root: Path) -> dict[str, str]:
    collected: dict[str, str] = {}
    for rel_path in task.boundary.read:
        path = repo_root / rel_path
        if path.is_file():
            collected[rel_path] = path.read_text(encoding="utf-8")
        elif path.is_dir():
            collected[rel_path] = _summarize_directory(path, repo_root)
        else:
            print(f"[WARN] Read boundary path not found: {path}")
    return collected


def collect_phase_context(task: TaskDefinition) -> dict[str, str]:
    if task.structure != StructureType.PHASE or task.phase_ctx is None:
        return {}

    ctx = task.phase_ctx
    files = {
        f"plans/{ctx.workstream}/overview.md": ctx.overview_path,
        f"plans/{ctx.workstream}/{ctx.phase}/specification.md": ctx.specification_path,
        f"plans/{ctx.workstream}/{ctx.phase}/decision_log.md": ctx.decision_log_path,
        f"plans/{ctx.workstream}/{ctx.phase}/work_log.md": ctx.work_log_path,
    }
    return {
        rel: path.read_text(encoding="utf-8")
        for rel, path in files.items()
        if path.is_file()
    }


def build_developer_instructions(
    task: TaskDefinition,
    context_files: dict[str, str],
    repo_root: Path,
    generate_plan: bool = False,
) -> str:
    parts: list[str] = [
        load_prompt_profile(repo_root),
        "",
    ]

    agents_path = repo_root / "AGENTS.md"
    if agents_path.is_file():
        parts.extend(["# AGENTS.md", agents_path.read_text(encoding="utf-8"), ""])

    phase_files = collect_phase_context(task)
    if phase_files:
        parts.append("# Phase Context")
        for rel_path, content in phase_files.items():
            parts.extend([f"## {rel_path}", content, ""])

    if context_files:
        parts.append("# Boundary Read Context")
        for rel_path, content in context_files.items():
            parts.extend([f"## {rel_path}", content, ""])

    if task.strict_constraints:
        parts.append("# Strict Constraints")
        parts.extend(f"- {constraint}" for constraint in task.strict_constraints)
        parts.append("")

    parts.append("# Write Boundary")
    if generate_plan:
        parts.append("- Plan generation mode: do not edit repository files during the Codex turn.")
    elif task.boundary.write:
        parts.extend(f"- {path}" for path in task.boundary.write)
    else:
        parts.append("- No write paths were declared; do not edit files.")
    parts.append("")

    return "\n".join(parts)


def build_task_instruction(
    task: TaskDefinition,
    plan_content: str | None = None,
    generate_plan: bool = False,
    repo_root: Path | None = None,
) -> str:
    parts: list[str] = []

    if generate_plan:
        parts.extend(
            [
                f"# Plan task: {task.title}",
                "Create a concise implementation plan in Markdown.",
                "Do not edit files. Return the plan as your final response.",
                "",
            ]
        )
    else:
        parts.extend([f"# {task.title}", "Implement the task according to the LoDD context.", ""])

    if task.phase_ctx:
        parts.append(f"Workstream: {task.phase_ctx.workstream}")
        parts.append(f"Phase: {task.phase_ctx.phase}")
        parts.append("")

    parts.append("# Task Source")
    parts.append(f"- {_format_task_source_path(task, repo_root)}")
    parts.append("")

    if plan_content:
        parts.extend(["# Override Plan", plan_content, ""])

    if task.iteration.current_delta_instruction:
        parts.extend(
            [
                f"# Retry Iteration {task.iteration.current_iteration}",
                task.iteration.current_delta_instruction,
                "",
            ]
        )

    parts.append("# Functional Contract / Done Condition")
    parts.append(f"- type: {task.done_condition.type}")
    if task.done_condition.auto_tests:
        parts.append("- auto:")
        parts.extend(f"  - {test}" for test in task.done_condition.auto_tests)
    else:
        parts.append("- auto: None")
    if task.done_condition.commands:
        parts.append("- commands:")
        for command in task.done_condition.commands:
            if getattr(command, "cwd", None) or getattr(command, "env", None):
                parts.append(f"  - command: {command.command}")
                if command.cwd:
                    parts.append(f"    cwd: {command.cwd}")
                if command.env:
                    parts.append("    env:")
                    parts.extend(
                        f"      - {key}={value}"
                        for key, value in sorted(command.env.items())
                    )
            else:
                parts.append(f"  - {command}")
    else:
        parts.append("- commands: None")
    parts.append(f"- manual: {task.done_condition.manual_desc or 'None'}")
    parts.append("")

    parts.append("# Context Boundary")
    parts.append("Read:")
    _extend_list_or_none(parts, task.boundary.read)
    parts.append("Write:")
    _extend_list_or_none(parts, task.boundary.write)
    parts.append("")

    if task.input_in_prompt:
        parts.append("# Input")
        parts.extend(f"- {item}" for item in task.input_in_prompt)
        parts.append("")

    return "\n".join(parts)


def _format_task_source_path(task: TaskDefinition, repo_root: Path | None) -> str:
    if task.source_path is None:
        return "Unknown"
    path = task.source_path
    if repo_root is not None:
        try:
            return path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _extend_list_or_none(parts: list[str], items: list[str]) -> None:
    if items:
        parts.extend(f"- {item}" for item in items)
    else:
        parts.append("- None")


def _summarize_directory(path: Path, repo_root: Path) -> str:
    entries = []
    for child in sorted(path.rglob("*")):
        if child.is_file():
            entries.append(child.relative_to(repo_root).as_posix())
    return "\n".join(entries)
