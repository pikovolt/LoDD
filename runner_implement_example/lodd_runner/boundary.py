from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import RunnerConfig, load_runner_config
from .task_parser import ContextBoundary


IGNORED_PARTS = {
    ".git",
    ".lodd",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".tox",
    ".nox",
    ".cache",
    "htmlcov",
    ".coverage",
    "coverage.xml",
}
DEPENDENCY_MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
}
DEPENDENCY_AUTH_PHRASES = {
    "dependency change allowed",
    "dependencies allowed",
    "ld-004 allowed",
}
INTERFACE_AUTH_PHRASES = {
    "interface change allowed",
    "signature change allowed",
    "ld-003 allowed",
}
REFACTOR_AUTH_PHRASES = {
    "refactor allowed",
    "refactoring allowed",
    "broad refactor allowed",
    "broad refactoring allowed",
    "ld-005 allowed",
}
REFACTOR_STRICT_PHRASES = {
    "ld-005 strict",
    "strict ld-005",
    "strict refactor policy",
    "strict refactoring policy",
}


@dataclass(frozen=True)
class FileState:
    digest: str
    text: str | None = None


RepoSnapshot = dict[str, FileState]


@dataclass
class BoundaryValidationResult:
    changed_files: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass
class BoundaryPreflightResult:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class TbdMarker:
    path: str
    line: int
    text: str


@dataclass
class TbdMarkerResult:
    markers: list[TbdMarker] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.markers


@dataclass
class DependencyAddition:
    path: str
    line: int
    text: str


@dataclass
class DependencyAdditionResult:
    findings: list[DependencyAddition] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass
class InterfaceChange:
    path: str
    reason: str


@dataclass
class InterfaceChangeResult:
    findings: list[InterfaceChange] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass
class RefactorWarning:
    code: str
    name: str
    message: str
    paths: list[str] = field(default_factory=list)


@dataclass
class RefactorWarningResult:
    warnings: list[RefactorWarning] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return True


def snapshot_repo(repo_root: Path) -> RepoSnapshot:
    snapshot: RepoSnapshot = {}
    root = repo_root.resolve()
    config = load_runner_config(root)
    for path in root.rglob("*"):
        if not path.is_file() or _is_ignored(path, root, config):
            continue
        rel = path.relative_to(root).as_posix()
        snapshot[rel] = FileState(
            digest=_file_digest(path),
            text=_read_snapshot_text(path, rel),
        )
    return snapshot


def validate_task_boundary(
    boundary: ContextBoundary,
    repo_root: Path,
    require_write: bool = True,
) -> BoundaryPreflightResult:
    root = repo_root.resolve()
    errors: list[str] = []

    if not boundary.read:
        errors.append("Context Boundary Read entries are required.")
    if require_write and not boundary.write:
        errors.append("Context Boundary Write entries are required.")

    for rel_path in boundary.read:
        target = _resolve_boundary_path(root, rel_path)
        if target is None:
            errors.append(f"Read path escapes repository: {rel_path}")
        elif not target.exists():
            errors.append(f"Read path not found: {rel_path}")

    for rel_path in boundary.write:
        target = _resolve_boundary_path(root, rel_path)
        if target is None:
            errors.append(f"Write path escapes repository: {rel_path}")

    return BoundaryPreflightResult(errors=errors)


def validate_changed_files(
    before: RepoSnapshot,
    after: RepoSnapshot,
    boundary: ContextBoundary,
    repo_root: Path,
) -> BoundaryValidationResult:
    changed = sorted(
        rel for rel in set(before) | set(after)
        if before.get(rel) != after.get(rel)
    )
    violations = [rel for rel in changed if not _is_write_allowed(rel, boundary.write)]
    return BoundaryValidationResult(changed_files=changed, violations=violations)


def detect_tbd_markers(changed_files: list[str], repo_root: Path) -> TbdMarkerResult:
    markers: list[TbdMarker] = []
    root = repo_root.resolve()
    for rel_path in changed_files:
        path = (root / rel_path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        for line_no, line in enumerate(lines, start=1):
            if "TBD(LD-" in line:
                markers.append(
                    TbdMarker(
                        path=rel_path,
                        line=line_no,
                        text=line.strip()[:160],
                    )
                )
    return TbdMarkerResult(markers=markers)


def detect_dependency_additions(
    before: RepoSnapshot,
    after: RepoSnapshot,
    changed_files: list[str],
    strict_constraints: list[str],
) -> DependencyAdditionResult:
    if _dependency_changes_allowed(strict_constraints):
        return DependencyAdditionResult()

    findings: list[DependencyAddition] = []
    for rel_path in changed_files:
        if Path(rel_path).name not in DEPENDENCY_MANIFESTS:
            continue
        before_text = before.get(rel_path, FileState(digest="")).text or ""
        after_text = after.get(rel_path, FileState(digest="")).text or ""
        if not after_text:
            continue
        findings.extend(_detect_manifest_additions(rel_path, before_text, after_text))
    return DependencyAdditionResult(findings=findings)


def detect_interface_changes(
    changed_files: list[str],
    boundary: ContextBoundary,
    strict_constraints: list[str],
    repo_root: Path,
    before: RepoSnapshot | None = None,
    after: RepoSnapshot | None = None,
) -> InterfaceChangeResult:
    if _interface_changes_allowed(strict_constraints):
        return InterfaceChangeResult()

    findings: list[InterfaceChange] = []
    for rel_path in changed_files:
        signature_reason = _python_signature_change_reason(
            rel_path,
            boundary,
            strict_constraints,
            before,
            after,
        )
        if signature_reason:
            findings.append(InterfaceChange(path=rel_path, reason=signature_reason))
            continue
        markdown_signature_reason = _markdown_signature_change_reason(
            rel_path,
            boundary,
            strict_constraints,
            before,
            after,
        )
        if markdown_signature_reason:
            findings.append(InterfaceChange(path=rel_path, reason=markdown_signature_reason))
            continue
        reason = _interface_change_reason(
            rel_path,
            boundary,
            strict_constraints,
            repo_root,
        )
        if reason:
            findings.append(InterfaceChange(path=rel_path, reason=reason))
    return InterfaceChangeResult(findings=findings)


def format_boundary_preflight(result: BoundaryPreflightResult) -> str:
    if result.ok:
        return "Context Boundary preflight passed."
    lines = [f"Context Boundary preflight failed: {len(result.errors)}"]
    lines.extend(f"  [LD-001] {error}" for error in result.errors)
    return "\n".join(lines)


def format_boundary_violations(result: BoundaryValidationResult) -> str:
    if result.ok:
        return "No boundary violations detected."
    lines = [f"Boundary violations: {len(result.violations)}"]
    lines.extend(f"  [LD-002] write: {path}" for path in result.violations)
    return "\n".join(lines)


def format_tbd_markers(result: TbdMarkerResult) -> str:
    if result.ok:
        return "No TBD lock-down markers detected."
    lines = [f"TBD lock-down markers: {len(result.markers)}"]
    lines.extend(
        f"  {marker.path}:{marker.line}: {marker.text}"
        for marker in result.markers
    )
    return "\n".join(lines)


def format_dependency_additions(result: DependencyAdditionResult) -> str:
    if result.ok:
        return "No unauthorized dependency additions detected."
    lines = [f"Unauthorized dependency additions: {len(result.findings)}"]
    lines.extend(
        f"  [LD-004] {finding.path}:{finding.line}: {finding.text}"
        for finding in result.findings
    )
    return "\n".join(lines)


def format_interface_changes(result: InterfaceChangeResult) -> str:
    if result.ok:
        return "No unauthorized interface changes detected."
    lines = [f"Unauthorized interface changes: {len(result.findings)}"]
    lines.extend(
        f"  [LD-003] {finding.path}: {finding.reason}"
        for finding in result.findings
    )
    return "\n".join(lines)


def detect_refactor_warnings(changed_files: list[str]) -> RefactorWarningResult:
    """Return advisory LD-005 warnings for broad same-extension churn.

    The initial heuristic intentionally under-reports: it only fires when one
    extension has at least five changed source files spread across at least two
    top-level directories. It is advisory and does not duplicate boundary or
    dependency policy.
    """
    source_paths = [
        path.replace("\\", "/").strip("/")
        for path in changed_files
        if _refactor_warning_candidate(path)
    ]
    by_extension: dict[str, list[str]] = {}
    for path in source_paths:
        suffix = Path(path).suffix.lower()
        by_extension.setdefault(suffix, []).append(path)

    warnings: list[RefactorWarning] = []
    for suffix, paths in sorted(by_extension.items()):
        unique_paths = sorted(set(paths))
        top_dirs = {path.split("/", 1)[0] for path in unique_paths if "/" in path}
        if len(unique_paths) >= 5 and len(top_dirs) >= 2:
            warnings.append(
                RefactorWarning(
                    code="LD-005",
                    name="broad same-extension churn",
                    message=(
                        f"{len(unique_paths)} {suffix or 'extensionless'} files changed "
                        f"across {len(top_dirs)} top-level directories"
                    ),
                    paths=unique_paths,
                )
            )
    return RefactorWarningResult(warnings=warnings)


def refactor_strict_requested(strict_constraints: list[str]) -> bool:
    """Return True when task Strict Constraints opt into hard LD-005 handling."""
    text = "\n".join(strict_constraints).lower()
    return any(phrase in text for phrase in REFACTOR_STRICT_PHRASES)


def refactor_changes_allowed(strict_constraints: list[str]) -> bool:
    """Return True when task Strict Constraints explicitly allow refactoring.

    Authorization is intentionally line-scoped so negated constraints such as
    ``No broad refactor allowed`` do not suppress strict LD-005 failures merely
    because they contain a positive phrase as a substring.
    """
    for line in strict_constraints:
        normalized = _normalize_constraint_line(line)
        if not any(phrase in normalized for phrase in REFACTOR_AUTH_PHRASES):
            continue
        if _negates_refactor_authorization(normalized):
            continue
        return True
    return False


def _normalize_constraint_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower())


def _negates_refactor_authorization(line: str) -> bool:
    return re.search(
        r"\b(?:no|not|never|without|do\s+not|don't|cannot|can't|disallow(?:ed)?|forbid(?:den)?)\b"
        r"[^.;\n]*\b(?:ld-005|refactor(?:ing)?)\b",
        line,
    ) is not None


def format_refactor_warnings(result: RefactorWarningResult) -> str:
    if not result.warnings:
        return "No LD-005 refactor warnings detected."
    lines = [f"Advisory refactor warnings: {len(result.warnings)}"]
    for warning in result.warnings:
        lines.append(f"  [{warning.code}] {warning.name}: {warning.message}")
        for path in warning.paths:
            lines.append(f"    - {path}")
    return "\n".join(lines)


def _interface_change_reason(
    rel_path: str,
    boundary: ContextBoundary,
    strict_constraints: list[str],
    repo_root: Path,
) -> str:
    rel = rel_path.replace("\\", "/").strip("/")
    if rel == "interfaces" or rel.startswith("interfaces/") or "/interfaces/" in rel:
        return "path is under an interfaces directory"
    if (
        rel.endswith(".md")
        and _boundary_contains(boundary.read, rel)
        and _markdown_has_interface_heading(repo_root / rel)
    ):
        return "changed Markdown Read boundary file has an Interface heading"
    if _boundary_contains(boundary.write, rel) and _strict_marks_interface_file(
        strict_constraints
    ):
        return "task strict constraints mark this Write entry as an interface file"
    return ""



def _python_signature_change_reason(
    rel_path: str,
    boundary: ContextBoundary,
    strict_constraints: list[str],
    before: RepoSnapshot | None,
    after: RepoSnapshot | None,
) -> str:
    rel = rel_path.replace("\\", "/").strip("/")
    if not rel.endswith(".py") or before is None or after is None:
        return ""
    if not _is_python_interface_classified(rel, boundary, strict_constraints):
        return ""
    before_text = (before.get(rel) or FileState(digest="")).text or ""
    after_text = (after.get(rel) or FileState(digest="")).text or ""
    before_signatures = _python_callable_signatures(before_text)
    after_signatures = _python_callable_signatures(after_text)
    if before_signatures is None or after_signatures is None:
        return ""
    if before_signatures == after_signatures:
        return ""

    removed = sorted(set(before_signatures) - set(after_signatures))
    added = sorted(set(after_signatures) - set(before_signatures))
    changed = sorted(
        name
        for name in set(before_signatures) & set(after_signatures)
        if before_signatures[name] != after_signatures[name]
    )
    parts: list[str] = []
    if changed:
        parts.append("signature changed for " + ", ".join(changed))
    if removed:
        parts.append("callable removed: " + ", ".join(removed))
    if added:
        parts.append("callable added: " + ", ".join(added))
    return "Python interface callable change detected (LD-003): " + "; ".join(parts)


def _markdown_signature_change_reason(
    rel_path: str,
    boundary: ContextBoundary,
    strict_constraints: list[str],
    before: RepoSnapshot | None,
    after: RepoSnapshot | None,
) -> str:
    rel = rel_path.replace("\\", "/").strip("/")
    if not rel.endswith(".md") or before is None or after is None:
        return ""
    before_text = (before.get(rel) or FileState(digest="")).text or ""
    after_text = (after.get(rel) or FileState(digest="")).text or ""
    if not _is_markdown_interface_classified(
        rel,
        boundary,
        strict_constraints,
        before_text,
        after_text,
    ):
        return ""
    before_signatures = _markdown_interface_signatures(before_text)
    after_signatures = _markdown_interface_signatures(after_text)
    if before_signatures == after_signatures:
        return ""

    before_by_name = _signature_map_by_name(before_signatures)
    after_by_name = _signature_map_by_name(after_signatures)
    changed = sorted(
        name
        for name in set(before_by_name) & set(after_by_name)
        if before_by_name[name] != after_by_name[name]
    )
    removed_names = sorted(set(before_by_name) - set(after_by_name))
    added_names = sorted(set(after_by_name) - set(before_by_name))
    parts: list[str] = []
    if changed:
        parts.append("signature changed for " + ", ".join(changed))
    if removed_names:
        parts.append("signature removed: " + ", ".join(removed_names))
    if added_names:
        parts.append("signature added: " + ", ".join(added_names))

    # Fallback for unusual duplicate names or extraction-only churn. The
    # conservative extractor should still surface that the signature set moved.
    if not parts:
        removed = sorted(before_signatures - after_signatures)
        added = sorted(after_signatures - before_signatures)
        if removed:
            parts.append("signature removed: " + ", ".join(removed))
        if added:
            parts.append("signature added: " + ", ".join(added))

    return "Markdown interface signature change detected (LD-003): " + "; ".join(parts)


def _is_markdown_interface_classified(
    rel_path: str,
    boundary: ContextBoundary,
    strict_constraints: list[str],
    before_text: str,
    after_text: str,
) -> bool:
    return (
        rel_path == "interfaces"
        or rel_path.startswith("interfaces/")
        or "/interfaces/" in rel_path
        or (
            _boundary_contains(boundary.read, rel_path)
            and (
                _markdown_text_has_interface_heading(before_text)
                or _markdown_text_has_interface_heading(after_text)
            )
        )
        or (_boundary_contains(boundary.write, rel_path) and _strict_marks_interface_file(strict_constraints))
    )


def _markdown_interface_signatures(text: str) -> set[str]:
    signatures: set[str] = set()
    for line in text.splitlines():
        signature = _markdown_signature_from_line(line)
        if signature:
            signatures.add(signature)
    return signatures


def _markdown_signature_from_line(line: str) -> str:
    candidate = line.strip()
    if not candidate:
        return ""
    if candidate.startswith("`") and candidate.endswith("`") and len(candidate) >= 2:
        candidate = candidate.strip("`").strip()
    heading = re.match(r"^#{1,6}\s+(.+)$", candidate)
    if heading:
        candidate = heading.group(1).strip()
    bullet = re.match(r"^[-*+]\s+(.+)$", candidate)
    if bullet:
        candidate = bullet.group(1).strip()
    if candidate.startswith("`") and candidate.endswith("`") and len(candidate) >= 2:
        candidate = candidate.strip("`").strip()
    candidate = re.sub(r"\s+", " ", candidate)
    if not re.match(
        r"^[A-Za-z_][A-Za-z0-9_.]*\s*\([^`\n]*\)\s*(?:->\s*[^`\n]+)?$",
        candidate,
    ):
        return ""
    return candidate


def _signature_map_by_name(signatures: set[str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for signature in signatures:
        name = signature.split("(", 1)[0].strip()
        # Duplicate names are represented by the full signature so additions or
        # removals are still reported by the fallback path.
        if name in mapped and mapped[name] != signature:
            mapped[signature] = signature
        else:
            mapped[name] = signature
    return mapped


def _is_python_interface_classified(
    rel_path: str,
    boundary: ContextBoundary,
    strict_constraints: list[str],
) -> bool:
    return (
        rel_path == "interfaces"
        or rel_path.startswith("interfaces/")
        or "/interfaces/" in rel_path
        or (_boundary_contains(boundary.write, rel_path) and _strict_marks_interface_file(strict_constraints))
    )


def _python_callable_signatures(text: str) -> dict[str, str] | None:
    try:
        tree = ast.parse(text or "")
    except SyntaxError:
        return None
    signatures: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signatures[node.name] = _signature_for(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    signatures[f"{node.name}.{child.name}"] = _signature_for(child)
    return signatures


def _signature_for(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return f"{_callable_kind(node)} {ast.unparse(node.args)}"


def _callable_kind(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    if isinstance(node, ast.AsyncFunctionDef):
        base = "async"
    else:
        base = "def"
    for decorator in node.decorator_list:
        name = _decorator_name(decorator)
        if name in {"classmethod", "staticmethod"}:
            return f"{base} {name}"
    return base


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _refactor_warning_candidate(path: str) -> bool:
    rel = path.replace("\\", "/").strip("/")
    suffix = Path(rel).suffix.lower()
    if suffix not in {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb"}:
        return False
    if Path(rel).name in DEPENDENCY_MANIFESTS:
        return False
    return True

def _markdown_has_interface_heading(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return _markdown_text_has_interface_heading(text)


def _markdown_text_has_interface_heading(text: str) -> bool:
    return re.search(r"^#{1,6}\s+.*\bInterface\b", text, flags=re.MULTILINE) is not None


def _strict_marks_interface_file(strict_constraints: list[str]) -> bool:
    return "interface file" in "\n".join(strict_constraints).lower()


def _interface_changes_allowed(strict_constraints: list[str]) -> bool:
    text = "\n".join(strict_constraints).lower()
    return any(phrase in text for phrase in INTERFACE_AUTH_PHRASES)


def _boundary_contains(entries: list[str], rel_path: str) -> bool:
    return any(rel_path == entry.replace("\\", "/").strip("/") for entry in entries)


def _detect_manifest_additions(
    rel_path: str,
    before_text: str,
    after_text: str,
) -> list[DependencyAddition]:
    name = Path(rel_path).name
    added = _added_lines(before_text, after_text)
    if name.startswith("requirements") and name.endswith(".txt"):
        return [
            DependencyAddition(rel_path, line_no, line.strip())
            for line_no, line in added
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if name == "pyproject.toml":
        return _detect_pyproject_additions(rel_path, after_text, added)
    if name in {"setup.py", "setup.cfg"}:
        return _detect_setup_additions(rel_path, after_text, added)
    return []


def _added_lines(before_text: str, after_text: str) -> list[tuple[int, str]]:
    before_counts = Counter(before_text.splitlines())
    added: list[tuple[int, str]] = []
    for line_no, line in enumerate(after_text.splitlines(), start=1):
        if before_counts[line] > 0:
            before_counts[line] -= 1
        else:
            added.append((line_no, line))
    return added


def _detect_pyproject_additions(
    rel_path: str,
    after_text: str,
    added: list[tuple[int, str]],
) -> list[DependencyAddition]:
    dependency_lines = set(_pyproject_dependency_line_numbers(after_text))
    findings: list[DependencyAddition] = []
    for line_no, line in added:
        stripped = line.strip()
        if line_no not in dependency_lines:
            continue
        if not stripped or stripped in {"[", "]", "{", "}"}:
            continue
        findings.append(DependencyAddition(rel_path, line_no, stripped))
    return findings


def _pyproject_dependency_line_numbers(text: str) -> set[int]:
    lines = text.splitlines()
    line_numbers: set[int] = set()
    section = ""
    in_project_dependencies = False
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]")
            in_project_dependencies = False
            continue
        if section == "project":
            if stripped.startswith("dependencies"):
                in_project_dependencies = True
                line_numbers.add(index)
                if "]" in stripped:
                    in_project_dependencies = False
                continue
            if in_project_dependencies:
                line_numbers.add(index)
                if "]" in stripped:
                    in_project_dependencies = False
            continue
        if (
            section == "project.optional-dependencies"
            or section == "tool.poetry.dependencies"
            or section.startswith("tool.poetry.group.")
            and section.endswith(".dependencies")
            or section == "dependency-groups"
        ):
            line_numbers.add(index)
    return line_numbers


def _detect_setup_additions(
    rel_path: str,
    after_text: str,
    added: list[tuple[int, str]],
) -> list[DependencyAddition]:
    block_lines = set(_setup_dependency_line_numbers(after_text))
    findings: list[DependencyAddition] = []
    for line_no, line in added:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _setup_dependency_keyword(stripped) or (
            line_no in block_lines and _looks_like_dependency_string(stripped)
        ):
            findings.append(DependencyAddition(rel_path, line_no, stripped))
    return findings


def _setup_dependency_line_numbers(text: str) -> set[int]:
    line_numbers: set[int] = set()
    in_block = False
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if _setup_dependency_keyword(stripped):
            in_block = True
            line_numbers.add(index)
            if _block_closes(stripped):
                in_block = False
            continue
        if in_block:
            line_numbers.add(index)
            if _block_closes(stripped):
                in_block = False
    return line_numbers


def _setup_dependency_keyword(stripped: str) -> bool:
    return "install_requires" in stripped or "extras_require" in stripped


def _looks_like_dependency_string(stripped: str) -> bool:
    return (
        (stripped.startswith(("'", '"')) or stripped.startswith(("- ",)))
        and any(ch.isalpha() for ch in stripped)
    )


def _block_closes(stripped: str) -> bool:
    return stripped.endswith(("]", ")", "}"))


def _dependency_changes_allowed(strict_constraints: list[str]) -> bool:
    text = "\n".join(strict_constraints).lower()
    return any(phrase in text for phrase in DEPENDENCY_AUTH_PHRASES)


def _is_write_allowed(rel_path: str, allowed: list[str]) -> bool:
    rel = rel_path.replace("\\", "/").strip("/")
    for raw in allowed:
        candidate = raw.replace("\\", "/").strip("/")
        if not candidate:
            continue
        if rel == candidate or rel.startswith(candidate.rstrip("/") + "/"):
            return True
    return False


def _resolve_boundary_path(root: Path, rel_path: str) -> Path | None:
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _is_ignored(path: Path, root: Path, config: RunnerConfig | None = None) -> bool:
    rel = path.relative_to(root).as_posix()
    rel_parts = path.relative_to(root).parts
    if any(part in IGNORED_PARTS for part in rel_parts):
        return True
    if config is None:
        return False
    if any(part in set(config.snapshot_ignore_names) for part in rel_parts):
        return True
    for prefix in config.snapshot_ignore_prefixes:
        normalized = prefix.strip("/")
        if rel == normalized or rel.startswith(normalized + "/"):
            return True
    return False


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_snapshot_text(path: Path, rel_path: str) -> str | None:
    if (
        Path(rel_path).name not in DEPENDENCY_MANIFESTS
        and not rel_path.endswith(".py")
        and not rel_path.endswith(".md")
    ):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
