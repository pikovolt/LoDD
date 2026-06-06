from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .task_parser import StructureType, TaskDefinition


@dataclass
class MaterializedWorkspace:
    agent_root: Path
    manifest_path: Path
    missing_write_files: list[str] = field(default_factory=list)


@dataclass
class MaterializedConflict:
    path: str
    reason: str


@dataclass
class PatchApplicationResult:
    applied_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    ignored_files: list[str] = field(default_factory=list)
    conflicted_files: list[str] = field(default_factory=list)
    conflict_details: list[MaterializedConflict] = field(default_factory=list)


def materialize_agent_workspace(task: TaskDefinition, repo_root: Path) -> MaterializedWorkspace:
    agent_root = (
        repo_root
        / ".lodd"
        / "runs"
        / task.task_id
        / f"iter-{task.iteration.current_iteration:02d}"
        / "agent_workspace"
    )
    if agent_root.exists():
        shutil.rmtree(agent_root)
    agent_root.mkdir(parents=True)

    copied: set[str] = set()
    missing_write_files: list[str] = []

    if task.source_path:
        _copy_path(task.source_path, agent_root / task.source_path.relative_to(repo_root), copied)

    for rel_path in _applicable_agents_paths(task, repo_root):
        _copy_existing_rel(repo_root, agent_root, rel_path, copied)

    if task.structure == StructureType.PHASE and task.phase_ctx:
        for path in (
            task.phase_ctx.overview_path,
            task.phase_ctx.specification_path,
            task.phase_ctx.decision_log_path,
            task.phase_ctx.work_log_path,
        ):
            if path.is_file():
                _copy_path(path, agent_root / path.relative_to(repo_root), copied)

    for rel_path in task.boundary.read:
        _copy_existing_rel(repo_root, agent_root, rel_path, copied)

    for rel_path in task.boundary.write:
        source = repo_root / rel_path
        target = agent_root / rel_path
        if source.exists():
            _copy_path(source, target, copied)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            missing_write_files.append(_normalize_rel(rel_path))

    write_file_states = _collect_write_file_states(repo_root, task.boundary.write)
    for missing in missing_write_files:
        write_file_states.setdefault(missing, None)

    manifest_path = agent_root / ".lodd_agent_manifest.json"
    manifest = {
        "schema_version": 2,
        "task_id": task.task_id,
        "iteration": task.iteration.current_iteration,
        "copied_files": sorted(copied),
        "missing_write_files": sorted(missing_write_files),
        "write_entries": [_normalize_rel(path) for path in task.boundary.write],
        "write_file_states": dict(sorted(write_file_states.items())),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return MaterializedWorkspace(
        agent_root=agent_root,
        manifest_path=manifest_path,
        missing_write_files=sorted(missing_write_files),
    )


def apply_materialized_write_patch(
    task: TaskDefinition,
    repo_root: Path,
    agent_root: Path,
) -> PatchApplicationResult:
    result = PatchApplicationResult(
        ignored_files=_changed_agent_files_outside_write(task, agent_root)
    )
    conflicts = _detect_materialized_conflicts(task, repo_root, agent_root)
    result.conflict_details = conflicts
    result.conflicted_files = sorted({conflict.path for conflict in conflicts})
    conflict_paths = set(result.conflicted_files)

    for rel_path in task.boundary.write:
        normalized = _normalize_rel(rel_path)
        source = repo_root / normalized
        agent_path = agent_root / normalized
        if source.is_dir() or agent_path.is_dir():
            _apply_write_directory(agent_path, source, normalized, result, conflict_paths)
            continue
        if normalized in conflict_paths:
            continue
        if agent_path.is_file():
            _copy_path(agent_path, source, set())
            result.applied_files.append(normalized)
        elif source.exists():
            source.unlink()
            result.deleted_files.append(normalized)
    result.applied_files = sorted(set(result.applied_files))
    result.deleted_files = sorted(set(result.deleted_files))
    result.ignored_files = sorted(set(result.ignored_files))
    result.conflicted_files = sorted(set(result.conflicted_files))
    result.conflict_details = sorted(result.conflict_details, key=lambda item: item.path)
    return result


def _apply_write_directory(
    agent_path: Path,
    source: Path,
    rel_path: str,
    result: PatchApplicationResult,
    conflict_paths: set[str] | None = None,
) -> None:
    conflict_paths = conflict_paths or set()
    before_files = {
        child.relative_to(source).as_posix()
        for child in source.rglob("*")
        if source.is_dir() and child.is_file()
    }
    after_files = {
        child.relative_to(agent_path).as_posix()
        for child in agent_path.rglob("*")
        if agent_path.is_dir() and child.is_file()
    }
    for child_rel in after_files:
        normalized_child = f"{rel_path.rstrip('/')}/{child_rel}"
        if normalized_child in conflict_paths:
            continue
        _copy_path(agent_path / child_rel, source / child_rel, set())
        result.applied_files.append(normalized_child)
    for child_rel in before_files - after_files:
        normalized_child = f"{rel_path.rstrip('/')}/{child_rel}"
        if normalized_child in conflict_paths:
            continue
        target = source / child_rel
        if target.is_file():
            target.unlink()
            result.deleted_files.append(normalized_child)


def _detect_materialized_conflicts(
    task: TaskDefinition,
    repo_root: Path,
    agent_root: Path,
) -> list[MaterializedConflict]:
    manifest = _read_manifest(agent_root)
    baseline: dict[str, str | None] = {
        _normalize_rel(str(path)): digest
        for path, digest in (manifest.get("write_file_states") or {}).items()
    }
    if not baseline:
        baseline = _collect_agent_manifest_fallback_states(task, repo_root, manifest)

    current = _collect_write_file_states(repo_root, task.boundary.write)
    conflicts: list[MaterializedConflict] = []
    for rel_path in sorted(set(baseline) | set(current)):
        if not _is_write_allowed(rel_path, [_normalize_rel(path) for path in task.boundary.write]):
            continue
        before_digest = baseline.get(rel_path)
        current_digest = current.get(rel_path)
        if before_digest == current_digest:
            continue
        if before_digest is None and current_digest is not None:
            reason = "created in repository after materialization"
        elif before_digest is not None and current_digest is None:
            reason = "deleted in repository after materialization"
        else:
            reason = "modified in repository after materialization"
        conflicts.append(MaterializedConflict(path=rel_path, reason=reason))
    return conflicts


def _read_manifest(agent_root: Path) -> dict[str, Any]:
    manifest_path = agent_root / ".lodd_agent_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _collect_agent_manifest_fallback_states(
    task: TaskDefinition,
    repo_root: Path,
    manifest: dict[str, Any],
) -> dict[str, str | None]:
    states = _collect_write_file_states(repo_root, task.boundary.write)
    for rel_path in manifest.get("missing_write_files") or []:
        states[_normalize_rel(str(rel_path))] = None
    return states


def _collect_write_file_states(repo_root: Path, write_entries: list[str]) -> dict[str, str | None]:
    states: dict[str, str | None] = {}
    root = repo_root.resolve()
    for raw in write_entries:
        rel = _normalize_rel(raw)
        if not rel:
            continue
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if target.is_file():
            states[rel] = _file_digest(target)
        elif target.is_dir():
            for child in target.rglob("*"):
                if child.is_file():
                    states[child.relative_to(root).as_posix()] = _file_digest(child)
        elif not target.exists():
            states.setdefault(rel, None)
    return states


def _changed_agent_files_outside_write(task: TaskDefinition, agent_root: Path) -> list[str]:
    ignored: set[str] = set()
    allowed = [_normalize_rel(path) for path in task.boundary.write]
    repo_root = _infer_repo_root_from_agent_root(task, agent_root)

    for path in agent_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(agent_root).as_posix()
        if rel == ".lodd_agent_manifest.json" or _is_write_allowed(rel, allowed):
            continue
        source = repo_root / rel
        if not source.is_file() or source.read_bytes() != path.read_bytes():
            ignored.add(rel)

    for rel in _expected_non_write_files(task, repo_root):
        if _is_write_allowed(rel, allowed):
            continue
        source = repo_root / rel
        agent_path = agent_root / rel
        if source.is_file() and not agent_path.exists():
            ignored.add(rel)

    return sorted(ignored)


def _expected_non_write_files(task: TaskDefinition, repo_root: Path) -> list[str]:
    expected: set[str] = set()
    if task.source_path:
        try:
            expected.add(task.source_path.relative_to(repo_root).as_posix())
        except ValueError:
            pass
    expected.update(_applicable_agents_paths(task, repo_root))
    for rel_path in task.boundary.read:
        source = repo_root / rel_path
        if source.is_file():
            expected.add(_normalize_rel(rel_path))
        elif source.is_dir():
            for child in source.rglob("*"):
                if child.is_file():
                    expected.add(child.relative_to(repo_root).as_posix())
    if task.structure == StructureType.PHASE and task.phase_ctx:
        for path in (
            task.phase_ctx.overview_path,
            task.phase_ctx.specification_path,
            task.phase_ctx.decision_log_path,
            task.phase_ctx.work_log_path,
        ):
            if path.is_file():
                expected.add(path.relative_to(repo_root).as_posix())
    return sorted(expected)


def _infer_repo_root_from_agent_root(task: TaskDefinition, agent_root: Path) -> Path:
    if task.source_path is not None:
        return _repo_root_from_task_path(task.source_path)

    marker = agent_root
    for _ in range(5):
        marker = marker.parent
    return marker


def _repo_root_from_task_path(task_path: Path) -> Path:
    tasks_dir = task_path.parent
    if tasks_dir.name != "tasks":
        return task_path.parent.parent
    phase_dir = tasks_dir.parent
    ws_dir = phase_dir.parent
    plans_dir = ws_dir.parent
    if plans_dir.name == "plans":
        return plans_dir.parent
    return tasks_dir.parent


def _applicable_agents_paths(task: TaskDefinition, repo_root: Path) -> list[str]:
    paths: set[str] = set()
    candidates: list[Path] = []
    if task.source_path:
        candidates.append(task.source_path)
    candidates.extend(repo_root / path for path in task.boundary.read)
    candidates.extend(repo_root / path for path in task.boundary.write)

    for candidate in candidates:
        current = candidate if candidate.is_dir() else candidate.parent
        try:
            current.relative_to(repo_root)
        except ValueError:
            continue
        while True:
            agents = current / "AGENTS.md"
            if agents.is_file():
                paths.add(agents.relative_to(repo_root).as_posix())
            if current == repo_root:
                break
            current = current.parent
    root_agents = repo_root / "AGENTS.md"
    if root_agents.is_file():
        paths.add("AGENTS.md")
    return sorted(paths)


def _copy_existing_rel(
    repo_root: Path,
    agent_root: Path,
    rel_path: str,
    copied: set[str],
) -> None:
    source = repo_root / rel_path
    if source.exists():
        _copy_path(source, agent_root / rel_path, copied)


def _copy_path(source: Path, target: Path, copied: set[str]) -> None:
    if source.is_dir():
        for child in source.rglob("*"):
            if child.is_file():
                child_target = target / child.relative_to(source)
                _copy_file(child, child_target)
                copied.add(child_target.as_posix())
        return
    if source.is_file():
        _copy_file(source, target)
        copied.add(target.as_posix())


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _is_write_allowed(rel_path: str, allowed: list[str]) -> bool:
    rel = _normalize_rel(rel_path)
    for raw in allowed:
        candidate = _normalize_rel(raw)
        if rel == candidate or rel.startswith(candidate.rstrip("/") + "/"):
            return True
    return False


def _normalize_rel(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
