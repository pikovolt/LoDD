from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CANONICAL_RESULT_STATES = (
    "green",
    "red",
    "breach",
    "needs-human-validation",
)
CANONICAL_DEBT_MARKER_FIELDS = (
    "unreviewed_functions",
    "boundary_issues",
    "knowledge_gaps",
)
HIGH_ITERATION_THRESHOLD = 3


@dataclass
class DebtMarkerDetail:
    task_file: str
    field: str
    value: str
    state: str = "unresolved"


@dataclass
class CompletedTaskMetadata:
    task_file: str
    result_state: str
    iterations: int = 0
    boundary_breaches: int = 0
    knowledge_added: int = 0
    manual_follow_up: str = ""


@dataclass
class RetrospectiveSummary:
    task_files: int = 0
    completed_tasks: int = 0
    total_iterations: int = 0
    total_boundary_breaches: int = 0
    total_knowledge_added: int = 0
    manual_follow_up_tasks: list[str] = field(default_factory=list)
    missing_or_malformed: list[str] = field(default_factory=list)
    tasks_with_debt_markers: int = 0
    tasks_with_recorded_debt: list[str] = field(default_factory=list)
    debt_marker_details: list[DebtMarkerDetail] = field(default_factory=list)
    accepted_debt_marker_details: list[DebtMarkerDetail] = field(default_factory=list)
    resolved_debt_marker_details: list[DebtMarkerDetail] = field(default_factory=list)
    missing_debt_markers: list[str] = field(default_factory=list)
    result_state_counts: dict[str, int] = field(
        default_factory=lambda: {state: 0 for state in CANONICAL_RESULT_STATES} | {"unknown": 0}
    )
    breach_tasks: list[str] = field(default_factory=list)
    needs_human_validation_tasks: list[str] = field(default_factory=list)
    missing_or_unknown_result_state: list[str] = field(default_factory=list)
    completed_task_details: list[CompletedTaskMetadata] = field(default_factory=list)


def summarize_retrospectives(path: str | Path) -> RetrospectiveSummary:
    root = Path(path).resolve()
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        raise ValueError(f"Task directory not found: {tasks_dir}")

    summary = RetrospectiveSummary()
    for task_path in sorted(tasks_dir.glob("*.md")):
        summary.task_files += 1
        text = task_path.read_text(encoding="utf-8")
        status = _section(text, "Status").strip()
        if status != "Done":
            continue

        summary.completed_tasks += 1
        debt_markers = _section(text, "Debt Markers")
        if debt_markers:
            summary.tasks_with_debt_markers += 1
            details = _recorded_debt_details(task_path.name, debt_markers)
            if details:
                unresolved = [detail for detail in details if detail.state == "unresolved"]
                if unresolved:
                    summary.tasks_with_recorded_debt.append(task_path.name)
                    summary.debt_marker_details.extend(unresolved)
                summary.accepted_debt_marker_details.extend(
                    detail for detail in details if detail.state == "accepted"
                )
                summary.resolved_debt_marker_details.extend(
                    detail for detail in details if detail.state == "resolved"
                )
        else:
            summary.missing_debt_markers.append(task_path.name)

        retro = _section(text, "Retrospective")
        if not retro:
            summary.missing_or_malformed.append(task_path.name)
            _record_result_state(summary, task_path.name, "")
            summary.completed_task_details.append(
                CompletedTaskMetadata(task_file=task_path.name, result_state="unknown")
            )
            continue

        values = _parse_retro_values(retro)
        malformed = False
        iterations, ok = _parse_nonnegative_int(values.get("iterations"))
        malformed = malformed or not ok
        boundary_breaches, ok = _parse_nonnegative_int(values.get("boundary_breaches"))
        malformed = malformed or not ok
        knowledge_added, ok = _parse_nonnegative_int(values.get("knowledge_added"))
        malformed = malformed or not ok
        if malformed:
            summary.missing_or_malformed.append(task_path.name)

        summary.total_iterations += iterations
        summary.total_boundary_breaches += boundary_breaches
        summary.total_knowledge_added += knowledge_added
        result_state = _record_result_state(summary, task_path.name, values.get("result_state", ""))

        manual_follow_up = values.get("manual_follow_up", "").strip()
        if manual_follow_up and manual_follow_up.lower() != "none":
            summary.manual_follow_up_tasks.append(task_path.name)

        summary.completed_task_details.append(
            CompletedTaskMetadata(
                task_file=task_path.name,
                result_state=result_state,
                iterations=iterations,
                boundary_breaches=boundary_breaches,
                knowledge_added=knowledge_added,
                manual_follow_up=manual_follow_up,
            )
        )

    return summary


def format_retrospective_summary(summary: RetrospectiveSummary) -> str:
    state_counts = _format_state_counts(summary)
    debt_details = _format_debt_details(summary.debt_marker_details)
    accepted_details = _format_debt_details(summary.accepted_debt_marker_details)
    resolved_details = _format_debt_details(summary.resolved_debt_marker_details)
    lines = [
        "Retrospective Summary",
        f"- task_files: {summary.task_files}",
        f"- completed_tasks: {summary.completed_tasks}",
        f"- total_iterations: {summary.total_iterations}",
        f"- total_boundary_breaches: {summary.total_boundary_breaches}",
        f"- total_knowledge_added: {summary.total_knowledge_added}",
        f"- result_state_counts: {state_counts}",
        "- breach_tasks: " + (", ".join(summary.breach_tasks) or "None"),
        "- needs_human_validation_tasks: "
        + (", ".join(summary.needs_human_validation_tasks) or "None"),
        "- missing_or_unknown_result_state: "
        + (", ".join(summary.missing_or_unknown_result_state) or "None"),
        "- manual_follow_up_tasks: "
        + (", ".join(summary.manual_follow_up_tasks) or "None"),
        "- missing_or_malformed_retrospectives: "
        + (", ".join(summary.missing_or_malformed) or "None"),
        f"- tasks_with_debt_markers: {summary.tasks_with_debt_markers}",
        "- tasks_with_recorded_debt: "
        + (", ".join(summary.tasks_with_recorded_debt) or "None"),
        "- debt_marker_details: "
        + ("; ".join(debt_details) or "None"),
        "- debt_marker_state_counts: "
        + (
            f"unresolved={len(summary.debt_marker_details)}, "
            f"accepted={len(summary.accepted_debt_marker_details)}, "
            f"resolved={len(summary.resolved_debt_marker_details)}"
        ),
        "- accepted_debt_marker_details: "
        + ("; ".join(accepted_details) or "None"),
        "- resolved_debt_marker_details: "
        + ("; ".join(resolved_details) or "None"),
        "- missing_debt_markers: "
        + (", ".join(summary.missing_debt_markers) or "None"),
    ]
    return "\n".join(lines)


def _record_result_state(summary: RetrospectiveSummary, task_name: str, raw_state: str) -> str:
    state = raw_state.strip().lower()
    if state in CANONICAL_RESULT_STATES:
        summary.result_state_counts[state] += 1
        if state == "breach":
            summary.breach_tasks.append(task_name)
        elif state == "needs-human-validation":
            summary.needs_human_validation_tasks.append(task_name)
        return state

    summary.result_state_counts["unknown"] += 1
    summary.missing_or_unknown_result_state.append(task_name)
    if task_name not in summary.missing_or_malformed:
        summary.missing_or_malformed.append(task_name)
    return "unknown"


def _section(text: str, section_name: str) -> str:
    pattern = rf"^##\s+{re.escape(section_name)}\s*\n(.*?)(?=^##\s+|\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _parse_retro_values(retro: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in retro.splitlines():
        match = re.match(r"^\s*-\s*([A-Za-z_-]+)\s*:\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _parse_nonnegative_int(raw: str | None) -> tuple[int, bool]:
    if raw is None:
        return 0, False
    try:
        value = int(raw)
    except ValueError:
        return 0, False
    if value < 0:
        return 0, False
    return value, True


def _recorded_debt_details(task_name: str, debt_markers: str) -> list[DebtMarkerDetail]:
    values = _parse_retro_values(debt_markers)
    details: list[DebtMarkerDetail] = []
    for field_name in CANONICAL_DEBT_MARKER_FIELDS:
        value = values.get(field_name, "").strip()
        if value and value.lower() != "none":
            state, cleaned = _parse_debt_marker_state(value)
            details.append(
                DebtMarkerDetail(
                    task_file=task_name,
                    field=field_name,
                    value=cleaned,
                    state=state,
                )
            )
    return details


def _parse_debt_marker_state(value: str) -> tuple[str, str]:
    match = re.match(r"^(accepted|resolved|unresolved)\s*:\s*(.*)$", value.strip(), re.IGNORECASE)
    if not match:
        return "unresolved", value.strip()
    state = match.group(1).lower()
    cleaned = match.group(2).strip()
    return state, cleaned or "None"


def _has_recorded_debt(debt_markers: str) -> bool:
    return any(
        detail.state == "unresolved"
        for detail in _recorded_debt_details("", debt_markers)
    )


def parse_tool_lifecycle(text: str) -> tuple[str | None, str]:
    """Return normalized expected_lifespan and a note from Markdown text."""
    lifecycle = _section(text, "Tool Lifecycle")
    search_text = lifecycle or text
    match = re.search(r"expected_lifespan\s*:\s*([^\n#]+)", search_text, re.IGNORECASE)
    if not match:
        return None, "Tool Lifecycle expected_lifespan not found; using default repayment policy."
    raw = match.group(1).strip().strip("`* ")
    normalized = _normalize_expected_lifespan(raw)
    if normalized is None:
        return None, f'Unrecognized expected_lifespan "{raw}"; using default repayment policy.'
    return normalized, f"Tool Lifecycle expected_lifespan: {normalized}."


def read_tool_lifecycle(path: str | Path) -> tuple[str | None, str]:
    text = Path(path).read_text(encoding="utf-8")
    return parse_tool_lifecycle(text)


def _normalize_expected_lifespan(raw: str) -> str | None:
    value = re.sub(r"\s+", " ", raw.strip().lower())
    if value == "single-shot":
        return "single-shot"
    if value.startswith("short-term"):
        return "short-term"
    if value == "long-term":
        return "long-term"
    return None


def find_architecture_file(root: str | Path) -> Path | None:
    base = Path(root).resolve()
    candidates = [base / "architecture.md", base / "docs" / "runner" / "architecture.md"]
    existing = [path for path in candidates if path.is_file()]
    if len(existing) == 1:
        return existing[0]
    return None


@dataclass
class RepaymentPlanData:
    lifecycle: str | None
    note: str
    high: list[str]
    follow_up: list[str]
    knowledge: list[str]
    skipped: list[str]
    demoted: list[str]
    accepted: list[str]
    resolved: list[str]
    state_counts: str

    @property
    def active_work(self) -> list[str]:
        return [*self.high, *self.follow_up, *self.knowledge]


def _build_repayment_plan_data(
    summary: RetrospectiveSummary,
    expected_lifespan: str | None = None,
    lifecycle_note: str | None = None,
) -> RepaymentPlanData:
    lifecycle = expected_lifespan if expected_lifespan in {"single-shot", "short-term", "long-term"} else None
    note = lifecycle_note or "Tool Lifecycle not provided; using default repayment policy."

    high: list[str] = []
    follow_up: list[str] = []
    knowledge: list[str] = []
    skipped: list[str] = []
    demoted: list[str] = []

    for task_name in summary.breach_tasks:
        high.append(f"- R-002 boundary/interface check: {task_name} recorded result_state=breach.")
    for task_name in summary.needs_human_validation_tasks:
        high.append(f"- R-001 human validation review: {task_name} recorded result_state=needs-human-validation.")
    for task_name in summary.manual_follow_up_tasks:
        if task_name not in summary.needs_human_validation_tasks:
            high.append(f"- R-001 manual follow-up review: {task_name} has manual_follow_up metadata.")

    for detail in summary.debt_marker_details:
        line = f"{detail.task_file}: {detail.field}={detail.value}"
        if lifecycle == "single-shot":
            if detail.field == "boundary_issues" and detail.task_file in summary.breach_tasks:
                high.append(f"- R-002 boundary repayment: {line}")
            else:
                skipped.append(f"- single-shot skip: {line}")
            continue
        if lifecycle == "short-term":
            if detail.field == "boundary_issues":
                high.append(f"- R-002 short-term contract/boundary repayment: {line}")
            elif detail.field == "unreviewed_functions":
                follow_up.append(f"- R-001 short-term understanding review: {line}")
            elif detail.field == "knowledge_gaps":
                demoted.append(f"- next-cycle knowledge watch: {line}")
            continue
        if detail.field == "boundary_issues":
            high.append(f"- R-002 boundary repayment: {line}")
        elif detail.field == "knowledge_gaps":
            knowledge.append(f"- R-004 knowledge transfer: {line}")
        else:
            follow_up.append(f"- R-001 code review: {line}")

    if lifecycle == "single-shot" and not high:
        skipped.insert(0, "- Active repayment is normally skipped for a single-shot tool when Done verification passed.")

    return RepaymentPlanData(
        lifecycle=lifecycle,
        note=note,
        high=high,
        follow_up=follow_up,
        knowledge=knowledge,
        skipped=skipped,
        demoted=demoted,
        accepted=_format_debt_details(summary.accepted_debt_marker_details),
        resolved=_format_debt_details(summary.resolved_debt_marker_details),
        state_counts=_format_state_counts(summary),
    )


def format_repayment_plan(
    summary: RetrospectiveSummary,
    expected_lifespan: str | None = None,
    lifecycle_note: str | None = None,
) -> str:
    """Format a bounded local debt repayment plan from retrospective metadata."""
    data = _build_repayment_plan_data(summary, expected_lifespan, lifecycle_note)
    lines = [
        "# Debt Repayment Plan",
        "",
        "## Tool Lifecycle Policy",
        f"- {data.note}",
        "- repayment priority is based on likely next-cycle recurrence, not on repaying everything at once.",
        "",
        "## High-Priority Active Debt",
        *(data.high or ["- None"]),
        "",
        "## Medium / Low Priority Follow-up",
        *(data.follow_up or ["- None"]),
        "",
        "## Knowledge Transfer Follow-up",
        *(data.knowledge or ["- None"]),
        "",
    ]
    if data.skipped:
        lines.extend(["## Skipped Ordinary Repayment", *data.skipped, ""])
    if data.demoted:
        lines.extend(["## Demoted Knowledge Watch", *data.demoted, ""])
    lines.extend([
        "## Manual Validation Follow-up",
        *(
            [f"- {task_name}" for task_name in summary.manual_follow_up_tasks]
            or ["- None"]
        ),
        "",
        "## Audit Context (Not Active Work)",
        "- accepted_debt_marker_details: " + ("; ".join(data.accepted) or "None"),
        "- resolved_debt_marker_details: " + ("; ".join(data.resolved) or "None"),
        "",
        "## Summary Counts",
        f"- task_files: {summary.task_files}",
        f"- completed_tasks: {summary.completed_tasks}",
        f"- active_debt_items: {len(summary.debt_marker_details)}",
        f"- breach_tasks: {len(summary.breach_tasks)}",
        f"- needs_human_validation_tasks: {len(summary.needs_human_validation_tasks)}",
        f"- manual_follow_up_tasks: {len(summary.manual_follow_up_tasks)}",
        f"- result_state_counts: {data.state_counts}",
    ])
    if not data.active_work and not summary.manual_follow_up_tasks and data.lifecycle != "single-shot":
        lines.insert(2, "No active repayment work was found in completed-task metadata.")
        lines.insert(3, "")
    return "\n".join(lines).rstrip() + "\n"


def write_repayment_plan(
    summary: RetrospectiveSummary,
    output_path: str | Path,
    expected_lifespan: str | None = None,
    lifecycle_note: str | None = None,
) -> Path:
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_repayment_plan(summary, expected_lifespan, lifecycle_note), encoding="utf-8")
    return target


def format_repayment_work_log_template(
    summary: RetrospectiveSummary,
    expected_lifespan: str | None = None,
    lifecycle_note: str | None = None,
) -> str:
    data = _build_repayment_plan_data(summary, expected_lifespan, lifecycle_note)
    skipped_or_demoted = [*data.skipped, *data.demoted]
    lines = [
        "## YYYY-MM-DD: Repayment Sprint",
        "",
        "Human-review required before copying this draft into `work_log.md`. Keep only verified facts and do not paste full iteration logs.",
        "",
        "### Tool Lifecycle Policy",
        f"- {data.note}",
        "",
        "### Active Repayment Actions",
        *(data.active_work or ["- None"]),
        "",
        "### Skipped / Demoted Items",
        *(skipped_or_demoted or ["- None"]),
        "",
        "### Audit Context (Not Active Work)",
        "- accepted_debt_marker_details: " + ("; ".join(data.accepted) or "None"),
        "- resolved_debt_marker_details: " + ("; ".join(data.resolved) or "None"),
        "",
        "### Summary Counts",
        f"- completed_tasks: {summary.completed_tasks}",
        f"- active_debt_items: {len(summary.debt_marker_details)}",
        f"- result_state_counts: {data.state_counts}",
        "",
        "### Human Review Notes",
        "- Confirm which actions were completed before appending to the durable phase work log.",
        "- Leave unresolved items visible in Debt Markers or a future repayment plan.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_repayment_work_log_template(
    summary: RetrospectiveSummary,
    output_path: str | Path,
    expected_lifespan: str | None = None,
    lifecycle_note: str | None = None,
) -> Path:
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_repayment_work_log_template(summary, expected_lifespan, lifecycle_note), encoding="utf-8")
    return target


def format_cycle_review(
    summary: RetrospectiveSummary,
    expected_lifespan: str | None = None,
    lifecycle_note: str | None = None,
) -> str:
    note = lifecycle_note or "Tool Lifecycle not provided; using default cycle-review context."
    lines = [
        "# Cycle Review",
        "",
        "## Tool Lifecycle Context",
        f"- {note}",
        "",
        "## Summary Counts",
        f"- task_files: {summary.task_files}",
        f"- completed_tasks: {summary.completed_tasks}",
        f"- result_state_counts: {_format_state_counts(summary)}",
        f"- total_iterations: {summary.total_iterations}",
        f"- total_boundary_breaches: {summary.total_boundary_breaches}",
        f"- total_knowledge_added: {summary.total_knowledge_added}",
        "- manual_follow_up_tasks: " + (", ".join(summary.manual_follow_up_tasks) or "None"),
        f"- active_debt_items: {len(summary.debt_marker_details)}",
        f"- accepted_debt_items: {len(summary.accepted_debt_marker_details)}",
        f"- resolved_debt_items: {len(summary.resolved_debt_marker_details)}",
        "",
    ]
    if summary.completed_tasks == 0:
        lines.extend([
            "## R-006 Trend Signals",
            "- No completed task metadata found; cycle-review has no trend data.",
            "",
            "## Next-Cycle Recommendations",
            "- None until at least one completed task has Retrospective metadata.",
        ])
        return "\n".join(lines).rstrip() + "\n"

    signals, recommendations = _cycle_review_signals(summary)
    lines.extend([
        "## R-006 Trend Signals",
        f"- iteration_threshold: >= {HIGH_ITERATION_THRESHOLD} iterations is treated as a review signal.",
        *(signals or ["- None"]),
        "",
        "## Next-Cycle Recommendations",
        *(recommendations or ["- None"]),
        "",
        "## Human Follow-up Visibility",
        "- needs_human_validation_tasks: " + (", ".join(summary.needs_human_validation_tasks) or "None"),
        "- manual_follow_up_tasks: " + (", ".join(summary.manual_follow_up_tasks) or "None"),
    ])
    return "\n".join(lines).rstrip() + "\n"


def write_cycle_review(
    summary: RetrospectiveSummary,
    output_path: str | Path,
    expected_lifespan: str | None = None,
    lifecycle_note: str | None = None,
) -> Path:
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_cycle_review(summary, expected_lifespan, lifecycle_note), encoding="utf-8")
    return target


def _cycle_review_signals(summary: RetrospectiveSummary) -> tuple[list[str], list[str]]:
    signals: list[str] = []
    recommendations: list[str] = []
    average_iterations = summary.total_iterations / summary.completed_tasks if summary.completed_tasks else 0.0
    high_iteration_tasks = [
        item.task_file for item in summary.completed_task_details
        if item.iterations >= HIGH_ITERATION_THRESHOLD
    ]
    if average_iterations >= HIGH_ITERATION_THRESHOLD or high_iteration_tasks:
        signals.append(
            "- R-006 task-size/interface-quality signal: "
            f"average_iterations={average_iterations:.2f}; high_iteration_tasks="
            + (", ".join(high_iteration_tasks) or "None")
        )
        recommendations.append(
            "- Split next-cycle tasks smaller and refine interfaces/ contracts before implementation."
        )

    boundary_tasks = _dedupe([
        item.task_file for item in summary.completed_task_details
        if item.boundary_breaches > 0 or item.result_state == "breach"
    ])
    if boundary_tasks:
        signals.append(
            "- R-006 boundary/module-design signal: boundary breach tasks="
            + ", ".join(boundary_tasks)
        )
        recommendations.append(
            "- Review Context Boundary construction, module ownership, and consider materialized mode for high-risk edits."
        )

    knowledge_tasks = [
        f"{item.task_file}({item.knowledge_added})"
        for item in summary.completed_task_details
        if item.knowledge_added > 0
    ]
    if knowledge_tasks:
        signals.append(
            "- R-006 knowledge-transfer signal: knowledge_added tasks="
            + ", ".join(knowledge_tasks)
        )
        recommendations.append(
            "- Transfer verified DCC/API/library landmines into knowledge/ or durable architecture notes."
        )

    if summary.needs_human_validation_tasks:
        signals.append(
            "- R-006 human-validation signal: needs-human-validation tasks="
            + ", ".join(summary.needs_human_validation_tasks)
        )
        recommendations.append(
            "- Resolve manual validation before treating the cycle as fully green."
        )
    return signals, _dedupe(recommendations)


def _format_debt_details(details: list[DebtMarkerDetail]) -> list[str]:
    return [f"{detail.task_file}: {detail.field}={detail.value}" for detail in details]


def _format_state_counts(summary: RetrospectiveSummary) -> str:
    return ", ".join(
        f"{state}={summary.result_state_counts.get(state, 0)}"
        for state in (*CANONICAL_RESULT_STATES, "unknown")
    )


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
