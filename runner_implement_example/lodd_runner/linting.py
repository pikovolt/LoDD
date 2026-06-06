from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .boundary import BoundaryPreflightResult, format_boundary_preflight, validate_task_boundary
from .config import RunnerConfig, format_runner_config_issues, load_runner_config
from .task_parser import (
    ContextBoundary,
    TaskParseDiagnostic,
    collect_task_diagnostics,
    format_task_diagnostics,
    parse_task_md,
)

STRICT_TASK_DIAGNOSTIC_CODES = {
    "TASK-UNKNOWN-SECTION",
    "TASK-DUPLICATE-SECTION",
    "TASK-MISSING-STATUS",
    "TASK-MISSING-CONTEXT-BOUNDARY",
    "TASK-MISSING-FUNCTIONAL-CONTRACT",
    "TASK-BOUNDARY-WRITE-EMPTY",
    "TASK-DUPLICATE-BOUNDARY-ENTRY",
    "TASK-DONE-UNKNOWN-TYPE",
    "TASK-DONE-AUTO-MISSING-TESTS",
    "TASK-DONE-COMMANDS-EMPTY",
    "TASK-DONE-MALFORMED-NESTING",
    "TASK-DEBT-NON-CANONICAL-FIELD",
    "TASK-DEBT-MISSING-CANONICAL-FIELD",
}
SOURCE_LIKE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".m",
    ".mm",
}
DCC_TOKENS = ("maya", "houdini", "blender")


@dataclass(frozen=True)
class BoundaryReviewFinding:
    code: str
    message: str


@dataclass
class BoundaryDependencyReviewResult:
    findings: list[BoundaryReviewFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return True


@dataclass
class TaskLintResult:
    task_id: str
    diagnostics: list[TaskParseDiagnostic] = field(default_factory=list)
    preflight: BoundaryPreflightResult = field(default_factory=BoundaryPreflightResult)
    config: RunnerConfig = field(default_factory=RunnerConfig)
    strict_task_format: bool = False
    boundary_review: BoundaryDependencyReviewResult | None = None

    @property
    def strict_diagnostics(self) -> list[TaskParseDiagnostic]:
        return [
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.code in STRICT_TASK_DIAGNOSTIC_CODES
        ]

    @property
    def ok(self) -> bool:
        if not self.strict_task_format:
            return True
        return not self.strict_diagnostics and self.preflight.ok and self.config.ok


def lint_task(
    task_path: str | Path,
    repo_root: str | Path,
    *,
    strict_task_format: bool = False,
    require_write: bool = True,
    boundary_review: bool = False,
) -> TaskLintResult:
    task_path = Path(task_path).resolve()
    repo_root = Path(repo_root).resolve()
    task = parse_task_md(task_path, repo_root)
    diagnostics = collect_task_diagnostics(task_path)
    preflight = validate_task_boundary(task.boundary, repo_root, require_write=require_write)
    config = load_runner_config(repo_root)
    review_result = (
        review_boundary_dependencies(task_path, repo_root, task.boundary)
        if boundary_review
        else None
    )
    return TaskLintResult(
        task_id=task.task_id,
        diagnostics=diagnostics,
        preflight=preflight,
        config=config,
        strict_task_format=strict_task_format,
        boundary_review=review_result,
    )


def review_boundary_dependencies(
    task_path: str | Path,
    repo_root: str | Path,
    boundary: ContextBoundary,
) -> BoundaryDependencyReviewResult:
    """Return bounded advisory prompts for task-author Context Boundary review."""
    repo = Path(repo_root).resolve()
    task_path = Path(task_path).resolve()
    result = BoundaryDependencyReviewResult()
    try:
        task_text = task_path.read_text(encoding="utf-8")
    except OSError as exc:
        task_text = ""
        result.notes.append(f"Task text could not be read for review: {_bounded(str(exc))}")

    _review_contract_like_reads(boundary, result)
    _review_dcc_knowledge(task_text, boundary, result)
    _review_transitive_interfaces(repo, boundary, result)
    return result


def format_task_lint_result(result: TaskLintResult) -> str:
    lines = [f"Task lint: {result.task_id}"]
    lines.append(format_task_diagnostics(result.diagnostics))
    lines.append(format_boundary_preflight(result.preflight))
    if result.boundary_review is not None:
        lines.append(format_boundary_dependency_review(result.boundary_review))
    lines.append(format_runner_config_issues(result.config))
    if result.strict_task_format:
        if result.ok:
            lines.append("Strict task format: passed")
        else:
            lines.append("Strict task format: failed")
            if result.strict_diagnostics:
                lines.append("Strict diagnostic failures:")
                lines.extend(
                    f"  - {diagnostic.code}"
                    for diagnostic in result.strict_diagnostics
                )
            if not result.preflight.ok:
                lines.append("Strict preflight failures:")
                lines.extend(f"  - {error}" for error in result.preflight.errors)
            if not result.config.ok:
                lines.append("Strict config failures:")
                lines.extend(f"  - {issue.code}: {issue.message}" for issue in result.config.issues)
    else:
        lines.append("Strict task format: not requested")
    return "\n".join(lines)


def format_boundary_dependency_review(result: BoundaryDependencyReviewResult) -> str:
    lines = ["Boundary dependency review:"]
    if not result.findings and not result.notes:
        lines.append("  No advisory findings.")
        return "\n".join(lines)
    for finding in result.findings:
        lines.append(f"  [advisory] {finding.code}: {finding.message}")
    for note in result.notes:
        lines.append(f"  [note] {note}")
    return "\n".join(lines)


def _review_contract_like_reads(
    boundary: ContextBoundary,
    result: BoundaryDependencyReviewResult,
) -> None:
    source_writes = [entry for entry in boundary.write if _is_source_like(entry)]
    if not source_writes:
        return
    if any(_is_contract_like_read(entry) for entry in boundary.read):
        return
    result.findings.append(
        BoundaryReviewFinding(
            code="BOUNDARY-REVIEW-CONTRACT-COVERAGE",
            message=(
                "Source-like Write entries are present without an interfaces/, docs/, "
                "or contract-like Read entry. Confirm the contract Read boundary for: "
                + _format_sample(source_writes)
            ),
        )
    )


def _review_dcc_knowledge(
    task_text: str,
    boundary: ContextBoundary,
    result: BoundaryDependencyReviewResult,
) -> None:
    haystack = "\n".join([task_text, *boundary.read, *boundary.write]).lower()
    reads = [entry.lower().replace("-", "_") for entry in boundary.read]
    for token in DCC_TOKENS:
        if not re.search(rf"\b{re.escape(token)}\b", haystack):
            continue
        if any(_matches_dcc_knowledge(read, token) for read in reads):
            continue
        result.findings.append(
            BoundaryReviewFinding(
                code="BOUNDARY-REVIEW-DCC-KNOWLEDGE",
                message=(
                    f"Task text or paths mention {token.title()} but Read entries do not include "
                    f"a matching knowledge/dcc_{token}... file. Confirm DCC-specific knowledge coverage."
                ),
            )
        )


def _review_transitive_interfaces(
    repo: Path,
    boundary: ContextBoundary,
    result: BoundaryDependencyReviewResult,
) -> None:
    read_set = {_normalize_rel(entry) for entry in boundary.read}
    for entry in sorted(read_set):
        if not entry.startswith("interfaces/"):
            continue
        path = (repo / entry).resolve()
        try:
            path.relative_to(repo)
        except ValueError:
            result.notes.append(f"Interface Read path escapes repository and was not reviewed: {entry}")
            continue
        if not path.is_file():
            result.notes.append(f"Interface Read path could not be reviewed because it was not found: {entry}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.notes.append(f"Interface Read path could not be reviewed: {entry}: {_bounded(str(exc))}")
            continue
        for dependency in _mentioned_interface_paths(text):
            normalized = _normalize_rel(dependency)
            if normalized == entry or normalized in read_set:
                continue
            result.findings.append(
                BoundaryReviewFinding(
                    code="BOUNDARY-REVIEW-TRANSITIVE-INTERFACE",
                    message=(
                        f"Read interface {entry} mentions {normalized}, which is not in the Read boundary. "
                        "Confirm whether this transitive interface dependency is relevant."
                    ),
                )
            )


def _matches_dcc_knowledge(read: str, token: str) -> bool:
    return (
        read.startswith(f"knowledge/dcc_{token}")
        or f"/dcc_{token}" in read
        or read.startswith(f"knowledge/{token}")
    )


def _mentioned_interface_paths(text: str) -> list[str]:
    matches = re.findall(r"interfaces/[A-Za-z0-9_.\-/]+", text)
    cleaned = [match.rstrip(".,);:]}") for match in matches]
    return list(dict.fromkeys(cleaned))


def _is_source_like(entry: str) -> bool:
    return Path(_normalize_rel(entry)).suffix.lower() in SOURCE_LIKE_SUFFIXES


def _is_contract_like_read(entry: str) -> bool:
    normalized = _normalize_rel(entry)
    name = Path(normalized).name.lower()
    parts = set(Path(normalized).parts)
    if "interfaces" in parts or "docs" in parts:
        return True
    contract_terms = ("interface", "contract", "spec", "specification", "api", "architecture")
    return any(term in name for term in contract_terms)


def _normalize_rel(entry: str) -> str:
    return entry.split("#", 1)[0].strip().strip("`").replace("\\", "/")


def _format_sample(entries: list[str], *, limit: int = 5) -> str:
    sample = [_normalize_rel(entry) for entry in entries[:limit]]
    suffix = "" if len(entries) <= limit else f"; +{len(entries) - limit} more"
    return ", ".join(sample) + suffix


def _bounded(value: str, limit: int = 160) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
