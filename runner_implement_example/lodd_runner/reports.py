from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def run_result_to_report(result: Any, handoff_artifact: str | Path | None = None) -> dict[str, Any]:
    state = getattr(result, "state", "")
    if hasattr(state, "value"):
        state = state.value
    failure_delta = getattr(result, "failure_delta", None)
    report = {
        "schema_version": 1,
        "task_id": getattr(result, "task_id", ""),
        "state": state,
        "ok": bool(getattr(result, "ok", False)),
        "attempts": getattr(result, "attempts", 0),
        "boundary_breaches": getattr(result, "boundary_breaches", 0),
        "manual_follow_up": getattr(result, "manual_follow_up", None),
        "agent_changed_files": list(getattr(result, "agent_changed_files", [])),
        "runner_metadata_files": list(getattr(result, "runner_metadata_files", [])),
        "materialized_conflicts": list(getattr(result, "materialized_conflicts", [])),
        "refactor_warnings": list(getattr(result, "refactor_warnings", [])),
        "failure_delta": _failure_delta_to_dict(failure_delta),
    }
    if handoff_artifact is not None:
        report["handoff_artifact"] = str(handoff_artifact)
    return report


def write_run_report(result: Any, path: str | Path, handoff_artifact: str | Path | None = None) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    report = run_result_to_report(result, handoff_artifact=handoff_artifact)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def _failure_delta_to_dict(delta: Any) -> dict[str, Any] | None:
    if delta is None:
        return None
    data = asdict(delta)
    # Keep the existing bounded stdout/stderr tails, but do not add full logs.
    return {
        "error_summary": data.get("error_summary", ""),
        "result_state": data.get("result_state", ""),
        "command": data.get("command", ""),
        "stdout_tail": data.get("stdout_tail", ""),
        "stderr_tail": data.get("stderr_tail", ""),
        "agent_changed_files": data.get("agent_changed_files", []),
        "runner_metadata_files": data.get("runner_metadata_files", []),
        "boundary_violations": data.get("boundary_violations", []),
        "interface_findings": data.get("interface_findings", []),
        "tbd_markers": data.get("tbd_markers", []),
        "dependency_findings": data.get("dependency_findings", []),
        "materialized_conflicts": data.get("materialized_conflicts", []),
        "next_delta_instruction": data.get("next_delta_instruction", ""),
    }


@dataclass
class PrReadinessDecision:
    decision: str
    normal_pr_eligible: bool
    draft_pr_eligible: bool = False
    reasons: list[str] = field(default_factory=list)


def classify_pr_readiness(report: dict[str, Any]) -> PrReadinessDecision:
    """Classify a bounded local run report for future PR publishing policy."""
    state = str(report.get("state", "") or "").strip().lower()
    manual_follow_up = str(report.get("manual_follow_up") or "").strip()
    conflicts = list(report.get("materialized_conflicts") or [])
    failure_delta = report.get("failure_delta")

    if state == "green":
        reasons: list[str] = []
        if manual_follow_up:
            reasons.append("manual_follow_up is present")
        if conflicts:
            reasons.append("materialized_conflicts are present")
        if _has_unresolved_failure_delta(failure_delta):
            reasons.append("failure_delta is present")
        if reasons:
            return PrReadinessDecision(
                decision="needs-manual-review",
                normal_pr_eligible=False,
                reasons=reasons,
            )
        return PrReadinessDecision(
            decision="eligible-normal",
            normal_pr_eligible=True,
            reasons=["green result with no manual follow-up, materialized conflicts, or failure delta"],
        )

    if state == "needs-human-validation":
        reasons = []
        if manual_follow_up:
            reasons.append(f"manual_follow_up: {manual_follow_up}")
        if conflicts:
            reasons.append(f"materialized_conflicts: {len(conflicts)} item(s)")
        if _has_unresolved_failure_delta(failure_delta):
            reasons.append("failure_delta requires review")
        if not reasons:
            reasons.append("needs-human-validation result requires human review")
        return PrReadinessDecision(
            decision="eligible-draft-human-validation",
            normal_pr_eligible=False,
            draft_pr_eligible=True,
            reasons=reasons,
        )

    if state == "red":
        return PrReadinessDecision(
            decision="not-eligible-red",
            normal_pr_eligible=False,
            reasons=["red result must not be published as a normal PR"],
        )

    if state == "breach":
        return PrReadinessDecision(
            decision="not-eligible-breach",
            normal_pr_eligible=False,
            reasons=["boundary breach result must not be published"],
        )

    return PrReadinessDecision(
        decision="not-eligible-malformed-report",
        normal_pr_eligible=False,
        reasons=[f"unknown or missing result state: {state or 'None'}"],
    )


def format_pr_readiness(decision: PrReadinessDecision) -> str:
    lines = [
        "PR Readiness",
        f"- decision: {decision.decision}",
        f"- normal_pr_eligible: {str(decision.normal_pr_eligible).lower()}",
        f"- draft_pr_eligible: {str(decision.draft_pr_eligible).lower()}",
        "- reasons:",
    ]
    lines.extend(f"  - {reason}" for reason in (decision.reasons or ["None"]))
    return "\n".join(lines) + "\n"


def read_pr_readiness_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _has_unresolved_failure_delta(delta: Any) -> bool:
    if not delta:
        return False
    if not isinstance(delta, dict):
        return True
    return any(bool(delta.get(key)) for key in (
        "error_summary",
        "boundary_violations",
        "interface_findings",
        "tbd_markers",
        "dependency_findings",
        "materialized_conflicts",
        "next_delta_instruction",
    ))
