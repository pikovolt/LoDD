from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class StructureType(Enum):
    FLAT = "flat"
    PHASE = "phase"


KNOWN_TASK_SECTIONS = {
    "Status",
    "Input in the prompt",
    "Context Boundary",
    "Functional Contract",
    "Strict Constraints",
    "Iteration Control",
    "Retrospective",
    "Debt Markers",
}
VALID_DONE_TYPES = {"auto", "manual", "hybrid"}
CANONICAL_DEBT_MARKER_FIELDS = (
    "unreviewed_functions",
    "boundary_issues",
    "knowledge_gaps",
)
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class TaskParseDiagnostic:
    severity: str
    code: str
    message: str
    section: str | None = None


@dataclass
class ContextBoundary:
    read: list[str] = field(default_factory=list)
    write: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BoundaryEntrySource:
    path: str
    raw: str


@dataclass(eq=False)
class DoneCommand:
    command: str
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.command

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.command == other and self.cwd is None and not self.env
        if not isinstance(other, DoneCommand):
            return False
        return (
            self.command == other.command
            and self.cwd == other.cwd
            and self.env == other.env
        )


@dataclass
class DoneCondition:
    type: str = "auto"
    auto_tests: list[str] = field(default_factory=list)
    commands: list[DoneCommand] = field(default_factory=list)
    manual_desc: str | None = None


@dataclass
class IterationControl:
    current_iteration: int = 1
    history: list[str] = field(default_factory=list)
    current_delta_instruction: str = ""


@dataclass
class PhaseContext:
    workstream: str
    phase: str
    phase_dir: Path
    overview_path: Path
    specification_path: Path
    decision_log_path: Path
    work_log_path: Path
    iterations_dir: Path


@dataclass
class TaskDefinition:
    task_id: str
    title: str
    status: str
    boundary: ContextBoundary
    strict_constraints: list[str]
    done_condition: DoneCondition
    iteration: IterationControl
    structure: StructureType
    input_in_prompt: list[str] = field(default_factory=list)
    source_path: Path | None = None
    phase_ctx: PhaseContext | None = None


def parse_task_md(file_path: str | Path, repo_root: str | Path | None = None) -> TaskDefinition:
    path = Path(file_path).resolve()
    text = path.read_text(encoding="utf-8")
    sections = _split_sections(text)

    title_match = re.search(r"^#\s+([Tt]ask[-_]?\d+[:\s]+)?(.+)$", text, re.MULTILINE)
    title = title_match.group(2).strip() if title_match else path.stem
    task_id = path.stem

    status = _parse_status(sections.get("Status", ""))
    input_in_prompt = _get_bullet_list(sections.get("Input in the prompt", ""))
    boundary = _parse_context_boundary(sections.get("Context Boundary", ""))
    strict_constraints = _get_bullet_list(sections.get("Strict Constraints", ""))
    done_condition = _parse_done_condition(sections.get("Functional Contract", ""))
    iteration = _parse_iteration_control(sections.get("Iteration Control", ""))
    structure, phase_ctx = _detect_structure(path)

    return TaskDefinition(
        task_id=task_id,
        title=title,
        status=status,
        boundary=boundary,
        strict_constraints=strict_constraints,
        done_condition=done_condition,
        iteration=iteration,
        structure=structure,
        input_in_prompt=input_in_prompt,
        source_path=path,
        phase_ctx=phase_ctx,
    )


def collect_task_diagnostics(file_path: str | Path) -> list[TaskParseDiagnostic]:
    path = Path(file_path).resolve()
    text = path.read_text(encoding="utf-8")
    sections = _split_sections(text)
    section_names = _section_names(text)
    diagnostics: list[TaskParseDiagnostic] = []

    section_counts = Counter(section_names)
    for section_name in section_names:
        if section_name not in KNOWN_TASK_SECTIONS:
            diagnostics.append(
                TaskParseDiagnostic(
                    severity="warning",
                    code="TASK-UNKNOWN-SECTION",
                    section=section_name,
                    message=(
                        f'Unknown top-level section "{section_name}" is ignored by '
                        "the structured parser and is not sent as structured task data."
                    ),
                )
            )
    for section_name in sorted(name for name, count in section_counts.items() if count > 1):
        diagnostics.append(
            TaskParseDiagnostic(
                severity="warning",
                code="TASK-DUPLICATE-SECTION",
                section=section_name,
                message=(
                    f'Top-level section "{section_name}" appears {section_counts[section_name]} times; '
                    "the lightweight parser keeps only the last occurrence."
                ),
            )
        )

    _add_missing_section_diagnostics(diagnostics, sections)
    _add_context_boundary_diagnostics(diagnostics, sections.get("Context Boundary", ""))
    _add_done_condition_diagnostics(diagnostics, sections.get("Functional Contract", ""))
    _add_debt_marker_diagnostics(diagnostics, sections.get("Debt Markers", ""))
    return diagnostics


def format_task_diagnostics(diagnostics: list[TaskParseDiagnostic]) -> str:
    lines = ["Parser warnings:"]
    if not diagnostics:
        lines.append("  None")
        return "\n".join(lines)
    for diagnostic in diagnostics:
        section = f" ({diagnostic.section})" if diagnostic.section else ""
        lines.append(
            f"  [{diagnostic.severity}] {diagnostic.code}{section}: {diagnostic.message}"
        )
    return "\n".join(lines)


def _add_missing_section_diagnostics(
    diagnostics: list[TaskParseDiagnostic],
    sections: dict[str, str],
) -> None:
    missing_specs = [
        (
            "Status",
            "TASK-MISSING-STATUS",
            "Missing Status section; parser will default task status to Not Started.",
        ),
        (
            "Context Boundary",
            "TASK-MISSING-CONTEXT-BOUNDARY",
            "Missing Context Boundary section; Read/Write entries will be empty and preflight is expected to fail.",
        ),
        (
            "Functional Contract",
            "TASK-MISSING-FUNCTIONAL-CONTRACT",
            "Missing Functional Contract section; parser will default to type auto with no auto tests.",
        ),
        (
            "Iteration Control",
            "TASK-MISSING-ITERATION-CONTROL",
            "Missing Iteration Control section; parser will use first-run defaults.",
        ),
    ]
    for section_name, code, message in missing_specs:
        if section_name not in sections:
            diagnostics.append(
                TaskParseDiagnostic(
                    severity="warning",
                    code=code,
                    section=section_name,
                    message=message,
                )
            )


def _add_context_boundary_diagnostics(
    diagnostics: list[TaskParseDiagnostic],
    section_text: str,
) -> None:
    if not section_text.strip():
        return

    boundary = _parse_context_boundary(section_text)
    if not boundary.write:
        diagnostics.append(
            TaskParseDiagnostic(
                severity="warning",
                code="TASK-BOUNDARY-WRITE-EMPTY",
                section="Context Boundary",
                message="Context Boundary Write entries are empty; task execution cannot edit repository files.",
            )
        )

    raw_entries = _parse_context_boundary_entries(section_text)
    if len(raw_entries["Read"]) > 5:
        diagnostics.append(
            TaskParseDiagnostic(
                severity="warning",
                code="TASK-BOUNDARY-READ-BROAD",
                section="Context Boundary",
                message=(
                    f"Context Boundary Read has {len(raw_entries['Read'])} entries; "
                    "LoDD recommends considering task splitting when Read exceeds five files."
                ),
            )
        )

    for label, entries in (("Read", boundary.read), ("Write", boundary.write)):
        normalized = [_normalize_boundary_entry(entry) for entry in entries]
        for entry, count in sorted(Counter(normalized).items()):
            if entry and count > 1:
                diagnostics.append(
                    TaskParseDiagnostic(
                        severity="warning",
                        code="TASK-DUPLICATE-BOUNDARY-ENTRY",
                        section="Context Boundary",
                        message=f"Duplicate Context Boundary {label} entry: {entry}",
                    )
                )

    for label in ("Read", "Write"):
        for entry in raw_entries[label]:
            if entry.path and not _boundary_entry_has_reason(entry.raw):
                diagnostics.append(
                    TaskParseDiagnostic(
                        severity="warning",
                        code="TASK-BOUNDARY-MISSING-REASON",
                        section="Context Boundary",
                        message=(
                            f"Context Boundary {label} entry lacks an inline why-comment/reason: "
                            f"{entry.path}"
                        ),
                    )
                )


def _add_done_condition_diagnostics(
    diagnostics: list[TaskParseDiagnostic],
    section_text: str,
) -> None:
    if not section_text.strip():
        return

    raw_type = _extract_done_type_value(section_text)
    done_type = raw_type.lower() if raw_type else "auto"
    if raw_type and done_type not in VALID_DONE_TYPES:
        diagnostics.append(
            TaskParseDiagnostic(
                severity="warning",
                code="TASK-DONE-UNKNOWN-TYPE",
                section="Functional Contract",
                message=(
                    f'Unknown Done condition type "{raw_type}"; supported values are '
                    "auto, manual, and hybrid. The parser may fall back to its default behavior."
                ),
            )
        )
        return

    for message in _find_done_condition_nesting_warnings(section_text):
        diagnostics.append(
            TaskParseDiagnostic(
                severity="warning",
                code="TASK-DONE-MALFORMED-NESTING",
                section="Functional Contract",
                message=message,
            )
        )

    done_condition = _parse_done_condition(section_text)
    effective_type = done_type if done_type in VALID_DONE_TYPES else done_condition.type
    if _has_empty_commands_list(section_text, done_condition):
        diagnostics.append(
            TaskParseDiagnostic(
                severity="warning",
                code="TASK-DONE-COMMANDS-EMPTY",
                section="Functional Contract",
                message=(
                    "Done condition declares commands: but no command entries were parsed; "
                    "add nested command items or remove the empty commands block."
                ),
            )
        )
    if (
        effective_type in {"auto", "hybrid"}
        and not done_condition.auto_tests
        and not done_condition.commands
    ):
        diagnostics.append(
            TaskParseDiagnostic(
                severity="warning",
                code="TASK-DONE-AUTO-MISSING-TESTS",
                section="Functional Contract",
                message=(
                    f"Done condition type {effective_type} has no auto test targets or commands; "
                    "Done verification will fail unless automatic verification is added."
                ),
            )
        )
    if effective_type in {"manual", "hybrid"} and not done_condition.manual_desc:
        diagnostics.append(
            TaskParseDiagnostic(
                severity="warning",
                code="TASK-DONE-MANUAL-MISSING-TEXT",
                section="Functional Contract",
                message=(
                    f"Done condition type {effective_type} has no manual validation text; "
                    "human follow-up will use a generic message."
                ),
            )
        )


def _add_debt_marker_diagnostics(
    diagnostics: list[TaskParseDiagnostic],
    section_text: str,
) -> None:
    if not section_text.strip():
        return

    fields = _parse_debt_marker_field_names(section_text)
    canonical = set(CANONICAL_DEBT_MARKER_FIELDS)
    for field_name in fields:
        if field_name not in canonical:
            diagnostics.append(
                TaskParseDiagnostic(
                    severity="warning",
                    code="TASK-DEBT-NON-CANONICAL-FIELD",
                    section="Debt Markers",
                    message=(
                        f'Non-canonical Debt Marker field "{field_name}" is ignored by summary/completion helpers. '
                        f"Use: {', '.join(CANONICAL_DEBT_MARKER_FIELDS)}."
                    ),
                )
            )
    for field_name in CANONICAL_DEBT_MARKER_FIELDS:
        if field_name not in fields:
            diagnostics.append(
                TaskParseDiagnostic(
                    severity="warning",
                    code="TASK-DEBT-MISSING-CANONICAL-FIELD",
                    section="Debt Markers",
                    message=f'Missing canonical Debt Marker field "{field_name}".',
                )
            )


def _extract_done_type_value(section_text: str) -> str | None:
    match = re.search(r"type\s*:\s*([^\n#]+)", section_text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip().strip("`* ")


def infer_repo_root(task_path: str | Path) -> Path:
    path = Path(task_path).resolve()
    tasks_dir = path.parent
    if tasks_dir.name != "tasks":
        return path.parent.parent

    phase_dir = tasks_dir.parent
    ws_dir = phase_dir.parent
    plans_dir = ws_dir.parent
    if plans_dir.name == "plans":
        return plans_dir.parent
    return tasks_dir.parent


def get_iterations_dir(task: TaskDefinition, repo_root: Path) -> Path:
    if task.structure == StructureType.PHASE and task.phase_ctx:
        return task.phase_ctx.iterations_dir / task.task_id
    return repo_root / "iterations" / task.task_id


def _detect_structure(task_path: Path) -> tuple[StructureType, PhaseContext | None]:
    tasks_dir = task_path.parent
    if tasks_dir.name != "tasks":
        return StructureType.FLAT, None

    phase_dir = tasks_dir.parent
    ws_dir = phase_dir.parent
    plans_dir = ws_dir.parent
    if plans_dir.name != "plans":
        return StructureType.FLAT, None

    return StructureType.PHASE, PhaseContext(
        workstream=ws_dir.name,
        phase=phase_dir.name,
        phase_dir=phase_dir,
        overview_path=ws_dir / "overview.md",
        specification_path=phase_dir / "specification.md",
        decision_log_path=phase_dir / "decision_log.md",
        work_log_path=phase_dir / "work_log.md",
        iterations_dir=phase_dir / "iterations",
    )


def _section_names(text: str) -> list[str]:
    return [match.group(1).strip() for match in re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE)]


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
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _get_bullet_list(section_text: str) -> list[str]:
    items: list[str] = []
    for line in section_text.splitlines():
        match = re.match(r"^\s*-\s+(.+)$", line)
        if match:
            items.append(match.group(1).strip())
    return items


def _strip_path_decorations(raw: str) -> str:
    value = raw.split("#", 1)[0].strip()
    return value.strip("`").strip()


def _parse_status(section_text: str) -> str:
    text = section_text.strip()
    if not text:
        return "Not Started"
    checked = re.search(r"\[[xX]\]\s*([^\|\[\]]+)", text)
    if checked:
        return checked.group(1).strip()
    return re.sub(r"^[-*]\s*", "", text.splitlines()[0]).strip()



def _parse_context_boundary_entries(section_text: str) -> dict[str, list[BoundaryEntrySource]]:
    entries = {"Read": [], "Write": []}
    current: str | None = None

    for line in section_text.splitlines():
        stripped = line.strip()
        if re.match(r"^-\s*Read\s*:", stripped, re.IGNORECASE) or re.match(
            r"^#{1,4}\s*Read\b", stripped, re.IGNORECASE
        ):
            current = "Read"
            continue
        if re.match(r"^-\s*Write\s*:", stripped, re.IGNORECASE) or re.match(
            r"^#{1,4}\s*Write\b", stripped, re.IGNORECASE
        ):
            current = "Write"
            continue

        item = re.match(r"^\s*[-*]\s+(.+)$", line)
        if item and current is not None:
            raw = item.group(1).strip()
            path = _strip_path_decorations(raw)
            if path:
                entries[current].append(BoundaryEntrySource(path=path, raw=raw))

    return entries


def _boundary_entry_has_reason(raw: str) -> bool:
    if "#" in raw and raw.split("#", 1)[1].strip():
        return True
    lowered = raw.lower()
    reason_markers = (" why:", " reason:", " because ", " rationale:", " // why:")
    return any(marker in lowered for marker in reason_markers)

def _parse_context_boundary(section_text: str) -> ContextBoundary:
    boundary = ContextBoundary()
    current: list[str] | None = None

    for line in section_text.splitlines():
        stripped = line.strip()
        if re.match(r"^-\s*Read\s*:", stripped, re.IGNORECASE) or re.match(
            r"^#{1,4}\s*Read\b", stripped, re.IGNORECASE
        ):
            current = boundary.read
            continue
        if re.match(r"^-\s*Write\s*:", stripped, re.IGNORECASE) or re.match(
            r"^#{1,4}\s*Write\b", stripped, re.IGNORECASE
        ):
            current = boundary.write
            continue

        item = re.match(r"^\s*[-*]\s+(.+)$", line)
        if item and current is not None:
            path = _strip_path_decorations(item.group(1))
            if path:
                current.append(path)

    return boundary


def _parse_done_condition(section_text: str) -> DoneCondition:
    dc = DoneCondition()
    mode: str | None = None
    current_command: DoneCommand | None = None
    env_mode = False

    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if mode == "commands" and current_command is not None:
            cwd_match = re.match(r"^-?\s*cwd\s*:\s*(.*)$", stripped, re.IGNORECASE)
            if cwd_match:
                current_command.cwd = cwd_match.group(1).strip() or None
                env_mode = False
                continue

            env_match = re.match(r"^-?\s*env\s*:\s*(.*)$", stripped, re.IGNORECASE)
            if env_match:
                inline = env_match.group(1).strip()
                if inline:
                    _assign_env_value(current_command.env, inline)
                env_mode = True
                continue

            if env_mode:
                parsed = _parse_env_assignment_line(line)
                if parsed is not None:
                    key, value = parsed
                    current_command.env[key] = value
                    continue

        type_match = re.match(
            r"^-?\s*type\s*:\s*(auto|manual|hybrid)\s*$",
            stripped,
            re.IGNORECASE,
        )
        if type_match:
            dc.type = type_match.group(1).lower()
            mode = None
            current_command = None
            env_mode = False
            continue

        manual_match = re.match(r"^-?\s*manual\s*:\s*(.*)$", stripped, re.IGNORECASE)
        if manual_match:
            value = manual_match.group(1).strip()
            dc.manual_desc = value if value else None
            mode = None
            current_command = None
            env_mode = False
            continue

        auto_match = re.match(r"^-?\s*auto\s*:\s*(.*)$", stripped, re.IGNORECASE)
        if auto_match:
            value = auto_match.group(1).strip()
            if value:
                test = _strip_path_decorations(value)
                if test:
                    dc.auto_tests.append(test)
                mode = None
            else:
                mode = "auto"
            current_command = None
            env_mode = False
            continue

        commands_match = re.match(
            r"^-?\s*commands\s*:\s*(.*)$",
            stripped,
            re.IGNORECASE,
        )
        if commands_match:
            value = commands_match.group(1).strip()
            if value:
                current_command = DoneCommand(command=value)
                dc.commands.append(current_command)
                mode = None
            else:
                mode = "commands"
                current_command = None
            env_mode = False
            continue

        item = re.match(r"^\s*[-*]\s+(.+)$", line)
        if item and mode == "auto":
            test = _strip_path_decorations(item.group(1))
            if test:
                dc.auto_tests.append(test)
            continue
        if item and mode == "commands":
            value = item.group(1).strip()
            command_match = re.match(r"command\s*:\s*(.+)$", value, re.IGNORECASE)
            if command_match:
                current_command = DoneCommand(command=command_match.group(1).strip())
                dc.commands.append(current_command)
                env_mode = False
                continue
            if current_command is not None:
                cwd_item = re.match(r"cwd\s*:\s*(.*)$", value, re.IGNORECASE)
                if cwd_item:
                    current_command.cwd = cwd_item.group(1).strip() or None
                    env_mode = False
                    continue
                env_item = re.match(r"env\s*:\s*(.*)$", value, re.IGNORECASE)
                if env_item:
                    inline = env_item.group(1).strip()
                    if inline:
                        _assign_env_value(current_command.env, inline)
                    env_mode = True
                    continue
                if env_mode:
                    parsed = _parse_env_assignment_value(value)
                    if parsed is not None:
                        key, env_value = parsed
                        current_command.env[key] = env_value
                        continue
            if value:
                current_command = DoneCommand(command=value)
                dc.commands.append(current_command)
                env_mode = False
            continue

        if stripped and mode is not None and not stripped.startswith(("-", "*")):
            mode = None
            current_command = None
            env_mode = False

    return dc


def _assign_env_value(env: dict[str, str], raw: str) -> None:
    parsed = _parse_env_assignment_value(raw)
    if parsed is None:
        return
    key, value = parsed
    env[key] = value


def _parse_env_assignment_line(line: str) -> tuple[str, str] | None:
    item = re.match(r"^\s*[-*]\s+(.+)$", line)
    if item:
        return _parse_env_assignment_value(item.group(1).strip())
    return _parse_env_assignment_value(line.strip())


def _parse_env_assignment_value(raw: str) -> tuple[str, str] | None:
    if not raw:
        return None
    if "=" in raw:
        key, value = raw.split("=", 1)
    elif ":" in raw:
        key, value = raw.split(":", 1)
    else:
        return None
    key = key.strip()
    if not ENV_NAME_PATTERN.match(key):
        return None
    return key, value.strip().strip("`\"")


def _has_empty_commands_list(section_text: str, done_condition: DoneCondition) -> bool:
    return bool(
        re.search(r"^\s*-?\s*commands\s*:\s*$", section_text, re.IGNORECASE | re.MULTILINE)
        and not done_condition.commands
    )


def _find_done_condition_nesting_warnings(section_text: str) -> list[str]:
    warnings: list[str] = []
    mode: str | None = None
    has_current_command = False
    env_mode = False

    for line_no, line in enumerate(section_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^-?\s*(type|auto|manual)\s*:", stripped, re.IGNORECASE):
            mode = None
            has_current_command = False
            env_mode = False
            continue
        if re.match(r"^-?\s*commands\s*:\s*$", stripped, re.IGNORECASE):
            mode = "commands"
            has_current_command = False
            env_mode = False
            continue
        if re.match(r"^-?\s*commands\s*:\s*.+$", stripped, re.IGNORECASE):
            mode = None
            has_current_command = True
            env_mode = False
            continue
        if mode != "commands":
            continue

        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        value = bullet.group(1).strip() if bullet else stripped

        if re.match(r"command\s*:\s*.+$", value, re.IGNORECASE):
            has_current_command = True
            env_mode = False
            continue
        if re.match(r"cwd\s*:", value, re.IGNORECASE):
            if not has_current_command:
                warnings.append(
                    f"Line {line_no}: cwd must be nested under a command entry."
                )
            env_mode = False
            continue
        if re.match(r"env\s*:", value, re.IGNORECASE):
            if not has_current_command:
                warnings.append(
                    f"Line {line_no}: env must be nested under a command entry."
                )
            env_mode = True
            continue
        if env_mode:
            if _parse_env_assignment_value(value) is None:
                warnings.append(
                    f"Line {line_no}: env entries must use KEY=value or KEY: value syntax."
                )
            continue
        if bullet:
            has_current_command = True
            continue
        if not line.startswith((" ", "\t")):
            mode = None
            has_current_command = False
            env_mode = False

    return warnings


def _parse_debt_marker_field_names(section_text: str) -> list[str]:
    fields: list[str] = []
    for line in section_text.splitlines():
        match = re.match(r"^\s*[-*]\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
        if match:
            fields.append(match.group(1))
    return fields


def _normalize_boundary_entry(raw: str) -> str:
    return raw.replace("\\", "/").strip().strip("/")


def _parse_iteration_control(section_text: str) -> IterationControl:
    ic = IterationControl()
    iter_match = re.search(r"current_iteration:\s*(\d+)", section_text)
    if iter_match:
        ic.current_iteration = int(iter_match.group(1))

    delta_match = re.search(r'current_delta_instruction:\s*"([^"]*)"', section_text)
    if delta_match:
        ic.current_delta_instruction = delta_match.group(1)

    history_match = re.search(r"history:\s*\[([^\]]*)\]", section_text)
    if history_match:
        raw = history_match.group(1).strip()
        if raw:
            ic.history = [item.strip().strip('"').strip("'") for item in raw.split(",")]
    return ic
