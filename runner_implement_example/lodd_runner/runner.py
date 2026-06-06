from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .backend import (
    AgentBackend,
    AgentSandbox,
    AgentTurnRequest,
    CodexAgentBackend,
)
from .boundary import (
    detect_dependency_additions,
    detect_interface_changes,
    detect_refactor_warnings,
    detect_tbd_markers,
    format_boundary_preflight,
    format_boundary_violations,
    format_dependency_additions,
    format_interface_changes,
    format_refactor_warnings,
    refactor_changes_allowed,
    refactor_strict_requested,
    format_tbd_markers,
    snapshot_repo,
    validate_task_boundary,
    validate_changed_files,
)
from .context_builder import (
    build_developer_instructions,
    build_task_instruction,
    collect_context_files,
)
from .iteration import (
    FailureDelta,
    mark_task_done,
    record_failure_to_work_log,
    record_iteration,
    update_task_iteration,
)
from .task_parser import (
    DoneCommand,
    TaskDefinition,
    collect_task_diagnostics,
    format_task_diagnostics,
    get_iterations_dir,
    parse_task_md,
)
from .workspace import apply_materialized_write_patch, materialize_agent_workspace

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 3
DONE_COMMAND_TIMEOUT_SECONDS = 120
DONE_STDOUT_TAIL_CHARS = 2000
DONE_STDERR_TAIL_CHARS = 1000
UNSUPPORTED_DONE_SHELL_TOKENS = {
    "|",
    "||",
    "&&",
    ";",
    ">",
    ">>",
    "<",
    "2>",
    "2>>",
    "&",
}


class RunState(str, Enum):
    GREEN = "green"
    RED = "red"
    BREACH = "breach"
    NEEDS_HUMAN_VALIDATION = "needs-human-validation"


class WorkspaceMode(str, Enum):
    REPO_ROOT = "repo-root"
    MATERIALIZED = "materialized"


@dataclass
class RunResult:
    state: RunState
    task_id: str
    attempts: int = 0
    boundary_breaches: int = 0
    response: str = ""
    manual_follow_up: str | None = None
    agent_changed_files: list[str] = field(default_factory=list)
    runner_metadata_files: list[str] = field(default_factory=list)
    failure_delta: FailureDelta | None = None
    materialized_conflicts: list[str] = field(default_factory=list)
    refactor_warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state in {
            RunState.GREEN,
            RunState.NEEDS_HUMAN_VALIDATION,
        }


@dataclass
class DoneVerification:
    passed: bool
    state: RunState
    manual_follow_up: str | None = None
    command: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""

    def __bool__(self) -> bool:
        return self.passed


@dataclass
class AttemptResult:
    state: RunState
    response: str = ""
    boundary_breaches: int = 0
    manual_follow_up: str | None = None
    changed_files: list[str] = field(default_factory=list)
    agent_changed_files: list[str] = field(default_factory=list)
    runner_metadata_files: list[str] = field(default_factory=list)
    failure_delta: FailureDelta | None = None
    materialized_conflicts: list[str] = field(default_factory=list)
    refactor_warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.changed_files and not self.agent_changed_files:
            self.agent_changed_files = list(self.changed_files)
        elif self.agent_changed_files and not self.changed_files:
            self.changed_files = list(self.agent_changed_files)

    @property
    def success(self) -> bool:
        return self.state in {
            RunState.GREEN,
            RunState.NEEDS_HUMAN_VALIDATION,
        }

    def __iter__(self):
        yield self.success
        yield self.boundary_breaches
        yield self.response


def _relative_to_repo(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _dedupe_paths(paths: list[str]) -> list[str]:
    return list(dict.fromkeys(path for path in paths if path))


def _print_run_report(
    agent_changed_files: list[str],
    runner_metadata_files: list[str],
    materialized_conflicts: list[str] | None = None,
    refactor_warnings: list[str] | None = None,
) -> None:
    print("[report] agent_changed_files:")
    if agent_changed_files:
        for path in agent_changed_files:
            print(f"  - {path}")
    else:
        print("  None")
    print("[report] runner_metadata_files:")
    if runner_metadata_files:
        for path in runner_metadata_files:
            print(f"  - {path}")
    else:
        print("  None")
    if materialized_conflicts is not None:
        print("[report] materialized_conflicts:")
        if materialized_conflicts:
            for path in materialized_conflicts:
                print(f"  - {path}")
        else:
            print("  None")
    if refactor_warnings is not None:
        print("[report] refactor_warnings:")
        if refactor_warnings:
            for warning in refactor_warnings:
                print(f"  - {warning}")
        else:
            print("  None")


def _planned_failure_metadata_files(task: TaskDefinition, repo_root: Path) -> list[str]:
    paths: list[str] = []
    iter_path = get_iterations_dir(task, repo_root) / f"iter-{task.iteration.current_iteration:02d}.md"
    paths.append(_relative_to_repo(iter_path, repo_root))
    if task.source_path is not None:
        paths.append(_relative_to_repo(task.source_path, repo_root))
    if task.phase_ctx is not None:
        paths.append(_relative_to_repo(task.phase_ctx.work_log_path, repo_root))
    return _dedupe_paths(paths)


def _record_failure_metadata(
    task: TaskDefinition,
    repo_root: Path,
    delta: FailureDelta,
) -> list[str]:
    delta.runner_metadata_files = _dedupe_paths([
        *delta.runner_metadata_files,
        *_planned_failure_metadata_files(task, repo_root),
    ])
    iter_path = record_iteration(task, repo_root, delta)
    task_update_path = update_task_iteration(
        task,
        repo_root,
        delta.error_summary,
        delta.next_delta_instruction,
    )
    work_log_path = record_failure_to_work_log(task, delta.error_summary)
    actual_paths = [
        _relative_to_repo(path, repo_root)
        for path in (iter_path, task_update_path, work_log_path)
        if path is not None
    ]
    delta.runner_metadata_files = _dedupe_paths([
        *delta.runner_metadata_files,
        *actual_paths,
    ])
    return delta.runner_metadata_files


def _reported_agent_changed_files(
    validation_changed_files: list[str],
    patch_result,
) -> list[str]:
    if patch_result is None:
        return _dedupe_paths(validation_changed_files)
    return _dedupe_paths([*patch_result.applied_files, *patch_result.deleted_files])


async def run_task(
    task_md_path: str | Path,
    repo_root: str | Path,
    model: str | None = None,
    max_iterations: int = MAX_ITERATIONS,
    plan_path: str | Path | None = None,
    generate_plan: bool = False,
    thread_id: str | None = None,
    workspace_mode: str | WorkspaceMode = WorkspaceMode.REPO_ROOT,
    strict_ld005: bool = False,
    backend: AgentBackend | None = None,
) -> bool:
    result = await run_task_with_result(
        task_md_path=task_md_path,
        repo_root=repo_root,
        model=model,
        max_iterations=max_iterations,
        plan_path=plan_path,
        generate_plan=generate_plan,
        thread_id=thread_id,
        workspace_mode=workspace_mode,
        strict_ld005=strict_ld005,
        backend=backend,
    )
    return result.ok


async def run_task_with_result(
    task_md_path: str | Path,
    repo_root: str | Path,
    model: str | None = None,
    max_iterations: int = MAX_ITERATIONS,
    plan_path: str | Path | None = None,
    generate_plan: bool = False,
    thread_id: str | None = None,
    workspace_mode: str | WorkspaceMode = WorkspaceMode.REPO_ROOT,
    strict_ld005: bool = False,
    backend: AgentBackend | None = None,
) -> RunResult:
    repo = Path(repo_root).resolve()
    task_path = Path(task_md_path).resolve()
    task = parse_task_md(task_path, repo)
    diagnostics = collect_task_diagnostics(task_path)
    if diagnostics:
        print(format_task_diagnostics(diagnostics))

    plan_content = None
    if plan_path:
        plan_content = Path(plan_path).read_text(encoding="utf-8")

    if generate_plan:
        max_iterations = 1

    preflight = validate_task_boundary(
        task.boundary,
        repo,
        require_write=not generate_plan,
    )
    if not preflight.ok:
        print(format_boundary_preflight(preflight))
        delta = FailureDelta(
            error_summary="Context Boundary preflight failed",
            result_state=RunState.BREACH.value,
            boundary_violations=preflight.errors,
            next_delta_instruction="Fix the task Context Boundary before running the agent.",
        )
        return RunResult(
            state=RunState.BREACH,
            task_id=task.task_id,
            failure_delta=delta,
        )

    boundary_breach_total = 0
    final_state = RunState.RED
    last_failure_delta: FailureDelta | None = None
    agent_changed_files: list[str] = []
    runner_metadata_files: list[str] = []
    materialized_conflicts: list[str] = []
    refactor_warnings: list[str] = []
    for attempt in range(1, max_iterations + 1):
        if attempt > 1:
            task = parse_task_md(task_path, repo)

        attempt_result = await _execute_single_attempt(
            task=task,
            repo_root=repo,
            model=model,
            plan_content=plan_content,
            generate_plan=generate_plan,
            thread_id=thread_id,
            workspace_mode=WorkspaceMode(workspace_mode),
            strict_ld005=strict_ld005,
            backend=backend,
        )
        boundary_breach_total += attempt_result.boundary_breaches
        final_state = attempt_result.state
        last_failure_delta = attempt_result.failure_delta
        agent_changed_files = _dedupe_paths([
            *agent_changed_files,
            *attempt_result.agent_changed_files,
        ])
        runner_metadata_files = _dedupe_paths([
            *runner_metadata_files,
            *attempt_result.runner_metadata_files,
        ])
        materialized_conflicts = _dedupe_paths([
            *materialized_conflicts,
            *attempt_result.materialized_conflicts,
        ])
        refactor_warnings = _dedupe_paths([
            *refactor_warnings,
            *attempt_result.refactor_warnings,
        ])

        if generate_plan:
            if attempt_result.success:
                out_dir = get_iterations_dir(task, repo)
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"plan-{task.task_id}.md"
                out_path.write_text(attempt_result.response, encoding="utf-8")
                runner_metadata_files = _dedupe_paths([
                    *runner_metadata_files,
                    _relative_to_repo(out_path, repo),
                ])
                print(f"[SUCCESS] Plan generated: {out_path}")
                _print_run_report(agent_changed_files, runner_metadata_files, materialized_conflicts, refactor_warnings)
            return RunResult(
                state=attempt_result.state,
                task_id=task.task_id,
                attempts=attempt,
                boundary_breaches=boundary_breach_total,
                response=attempt_result.response,
                agent_changed_files=agent_changed_files,
                runner_metadata_files=runner_metadata_files,
                failure_delta=attempt_result.failure_delta,
                materialized_conflicts=materialized_conflicts,
                refactor_warnings=refactor_warnings,
            )

        if attempt_result.success:
            metadata_paths = mark_task_done(
                task,
                repo,
                attempt,
                boundary_breach_total,
                manual_follow_up=attempt_result.manual_follow_up,
                result_state=attempt_result.state.value,
            )
            runner_metadata_files = _dedupe_paths([
                *runner_metadata_files,
                *(_relative_to_repo(path, repo) for path in metadata_paths),
            ])
            print(f"[SUCCESS] Task {task.task_id} completed.")
            _print_run_report(agent_changed_files, runner_metadata_files, materialized_conflicts, refactor_warnings)
            return RunResult(
                state=attempt_result.state,
                task_id=task.task_id,
                attempts=attempt,
                boundary_breaches=boundary_breach_total,
                response=attempt_result.response,
                manual_follow_up=attempt_result.manual_follow_up,
                agent_changed_files=agent_changed_files,
                runner_metadata_files=runner_metadata_files,
                failure_delta=attempt_result.failure_delta,
                materialized_conflicts=materialized_conflicts,
                refactor_warnings=refactor_warnings,
            )

        _print_run_report(agent_changed_files, runner_metadata_files, materialized_conflicts, refactor_warnings)
        if attempt < max_iterations:
            print("[RETRY] Preparing next iteration.")

    print("[FAILED] Max iterations reached.")
    return RunResult(
        state=final_state,
        task_id=task.task_id,
        attempts=max_iterations,
        boundary_breaches=boundary_breach_total,
        agent_changed_files=agent_changed_files,
        runner_metadata_files=runner_metadata_files,
        failure_delta=last_failure_delta,
        materialized_conflicts=materialized_conflicts,
        refactor_warnings=refactor_warnings,
    )


async def _execute_single_attempt(
    task: TaskDefinition,
    repo_root: Path,
    model: str | None = None,
    plan_content: str | None = None,
    generate_plan: bool = False,
    thread_id: str | None = None,
    workspace_mode: WorkspaceMode = WorkspaceMode.REPO_ROOT,
    strict_ld005: bool = False,
    backend: AgentBackend | None = None,
) -> AttemptResult:
    agent_backend = backend or CodexAgentBackend()
    context_files = collect_context_files(task, repo_root)
    developer_instructions = build_developer_instructions(
        task, context_files, repo_root, generate_plan
    )
    instruction = build_task_instruction(
        task, plan_content, generate_plan, repo_root=repo_root
    )
    sandbox = AgentSandbox.READ_ONLY if generate_plan else AgentSandbox.WORKSPACE_WRITE

    before = snapshot_repo(repo_root)
    agent_cwd = repo_root
    materialized = None
    if workspace_mode == WorkspaceMode.MATERIALIZED and not generate_plan:
        materialized = materialize_agent_workspace(task, repo_root)
        agent_cwd = materialized.agent_root

    result = await agent_backend.run_turn(
        AgentTurnRequest(
            cwd=agent_cwd,
            developer_instructions=developer_instructions,
            instruction=instruction,
            model=model,
            sandbox=sandbox,
            thread_id=thread_id,
        )
    )

    response = result.final_response
    patch_result = None
    if materialized is not None:
        patch_result = apply_materialized_write_patch(task, repo_root, materialized.agent_root)
        if patch_result.conflicted_files:
            conflict_lines = [
                f"{conflict.path}: {conflict.reason}"
                for conflict in patch_result.conflict_details
            ]
            print("Materialized workspace apply conflicts")
            for line in conflict_lines:
                print(f"- {line}")
            agent_changed_files = _dedupe_paths(
                [*patch_result.applied_files, *patch_result.deleted_files]
            )
            delta = FailureDelta(
                error_summary="Materialized workspace apply conflicts require human validation",
                result_state=RunState.NEEDS_HUMAN_VALIDATION.value,
                agent_changed_files=agent_changed_files,
                materialized_conflicts=conflict_lines,
                next_delta_instruction="Review conflicted files and manually reconcile repository changes with the materialized workspace output.",
            )
            return AttemptResult(
                state=RunState.NEEDS_HUMAN_VALIDATION,
                response=response,
                manual_follow_up="Materialized workspace apply conflicts require manual reconciliation.",
                agent_changed_files=agent_changed_files,
                failure_delta=delta,
                materialized_conflicts=conflict_lines,
            )
        if patch_result.ignored_files:
            ignored_lines = [
                f"Ignored materialized workspace change outside Write boundary: {path}"
                for path in patch_result.ignored_files
            ]
            print("Materialized workspace boundary breach")
            for line in ignored_lines:
                print(f"- {line}")
            agent_changed_files = _dedupe_paths(
                [*patch_result.applied_files, *patch_result.deleted_files]
            )
            delta = FailureDelta(
                error_summary="Ignored materialized workspace changes outside Write boundary",
                result_state=RunState.BREACH.value,
                agent_changed_files=agent_changed_files,
                boundary_violations=ignored_lines,
                next_delta_instruction="Remove changes outside the declared Write boundary or update the task boundary before retrying.",
            )
            runner_metadata_files = _record_failure_metadata(task, repo_root, delta)
            return AttemptResult(
                state=RunState.BREACH,
                boundary_breaches=len(patch_result.ignored_files),
                response=response,
                agent_changed_files=agent_changed_files,
                runner_metadata_files=runner_metadata_files,
                failure_delta=delta,
            )

    after = snapshot_repo(repo_root)
    validation = validate_changed_files(before, after, task.boundary, repo_root)
    agent_changed_files = _reported_agent_changed_files(validation.changed_files, patch_result)

    if not validation.ok:
        summary = format_boundary_violations(validation)
        print(summary)
        delta = FailureDelta(
            error_summary="Boundary violations",
            result_state=RunState.BREACH.value,
            agent_changed_files=agent_changed_files,
            boundary_violations=validation.violations,
            next_delta_instruction="Resolve writes outside the declared Context Boundary.",
        )
        runner_metadata_files = _record_failure_metadata(task, repo_root, delta)
        return AttemptResult(
            state=RunState.BREACH,
            boundary_breaches=len(validation.violations),
            response=response,
            agent_changed_files=agent_changed_files,
            runner_metadata_files=runner_metadata_files,
            failure_delta=delta,
        )

    if generate_plan:
        return AttemptResult(
            state=RunState.GREEN,
            response=response,
            agent_changed_files=agent_changed_files,
        )

    interface_result = detect_interface_changes(
        validation.changed_files,
        task.boundary,
        task.strict_constraints,
        repo_root,
        before=before,
        after=after,
    )
    if not interface_result.ok:
        summary = format_interface_changes(interface_result)
        print(summary)
        finding_lines = [
            f"{finding.path}: {finding.reason}"
            for finding in interface_result.findings
        ]
        delta = FailureDelta(
            error_summary="Unauthorized interface changes detected",
            result_state=RunState.BREACH.value,
            agent_changed_files=agent_changed_files,
            interface_findings=finding_lines,
            next_delta_instruction="Revert or explicitly authorize interface changes before completing the task.",
        )
        runner_metadata_files = _record_failure_metadata(task, repo_root, delta)
        return AttemptResult(
            state=RunState.BREACH,
            boundary_breaches=len(interface_result.findings),
            response=response,
            agent_changed_files=agent_changed_files,
            runner_metadata_files=runner_metadata_files,
            failure_delta=delta,
        )

    tbd_result = detect_tbd_markers(validation.changed_files, repo_root)
    if not tbd_result.ok:
        summary = format_tbd_markers(tbd_result)
        print(summary)
        marker_lines = [
            f"{marker.path}:{marker.line}: {marker.text}"
            for marker in tbd_result.markers
        ]
        delta = FailureDelta(
            error_summary="TBD lock-down markers detected",
            result_state=RunState.RED.value,
            agent_changed_files=agent_changed_files,
            tbd_markers=marker_lines,
            next_delta_instruction="Resolve TBD lock-down markers before completing the task.",
        )
        runner_metadata_files = _record_failure_metadata(task, repo_root, delta)
        return AttemptResult(
            state=RunState.RED,
            response=response,
            agent_changed_files=agent_changed_files,
            runner_metadata_files=runner_metadata_files,
            failure_delta=delta,
        )

    dependency_result = detect_dependency_additions(
        before,
        after,
        validation.changed_files,
        task.strict_constraints,
    )
    if not dependency_result.ok:
        summary = format_dependency_additions(dependency_result)
        print(summary)
        finding_lines = [
            f"{finding.path}:{finding.line}: {finding.text}"
            for finding in dependency_result.findings
        ]
        delta = FailureDelta(
            error_summary="Unauthorized dependency additions detected",
            result_state=RunState.RED.value,
            agent_changed_files=agent_changed_files,
            dependency_findings=finding_lines,
            next_delta_instruction="Remove or explicitly authorize dependency additions before completing the task.",
        )
        runner_metadata_files = _record_failure_metadata(task, repo_root, delta)
        return AttemptResult(
            state=RunState.RED,
            response=response,
            agent_changed_files=agent_changed_files,
            runner_metadata_files=runner_metadata_files,
            failure_delta=delta,
        )

    refactor_result = detect_refactor_warnings(agent_changed_files)
    refactor_warning_lines = [
        f"{warning.code} {warning.name}: {warning.message}"
        for warning in refactor_result.warnings
    ]
    if refactor_warning_lines:
        print(format_refactor_warnings(refactor_result))
    strict_refactor_policy = strict_ld005 or refactor_strict_requested(
        task.strict_constraints
    )
    if (
        refactor_warning_lines
        and strict_refactor_policy
        and not refactor_changes_allowed(task.strict_constraints)
    ):
        delta = FailureDelta(
            error_summary="LD-005 strict refactor policy failed",
            result_state=RunState.RED.value,
            agent_changed_files=agent_changed_files,
            next_delta_instruction=(
                "Reduce broad refactor churn, split the task, or explicitly authorize "
                "refactoring in Strict Constraints before retrying."
            ),
        )
        runner_metadata_files = _record_failure_metadata(task, repo_root, delta)
        return AttemptResult(
            state=RunState.RED,
            response=response,
            agent_changed_files=agent_changed_files,
            runner_metadata_files=runner_metadata_files,
            failure_delta=delta,
            refactor_warnings=refactor_warning_lines,
        )

    verification = _verify_done_condition(task, repo_root)
    if verification:
        return AttemptResult(
            state=verification.state,
            response=response,
            manual_follow_up=verification.manual_follow_up,
            agent_changed_files=agent_changed_files,
            refactor_warnings=refactor_warning_lines,
        )

    error = "Done condition was not met"
    delta = FailureDelta(
        error_summary=error,
        result_state=RunState.RED.value,
        command=verification.command,
        stdout_tail=verification.stdout_tail,
        stderr_tail=verification.stderr_tail,
        agent_changed_files=agent_changed_files,
        next_delta_instruction="Fix the failing Done Condition verification.",
    )
    runner_metadata_files = _record_failure_metadata(task, repo_root, delta)
    return AttemptResult(
        state=RunState.RED,
        response=response,
        agent_changed_files=agent_changed_files,
        runner_metadata_files=runner_metadata_files,
        failure_delta=delta,
        refactor_warnings=refactor_warning_lines,
    )


def _verify_done_condition(task: TaskDefinition, repo_root: Path) -> DoneVerification:
    dc = task.done_condition
    if dc.type == "manual":
        print("[DONE] manual verification required; accepting runner completion.")
        return DoneVerification(
            passed=True,
            state=RunState.NEEDS_HUMAN_VALIDATION,
            manual_follow_up=dc.manual_desc or "Manual verification required.",
        )

    if not dc.auto_tests and not dc.commands:
        print("[WARN] No auto tests or commands declared.")
        return DoneVerification(passed=False, state=RunState.RED)

    last_verification = DoneVerification(passed=True, state=RunState.GREEN)
    if dc.auto_tests:
        pytest_command = [sys.executable, "-m", "pytest", *dc.auto_tests]
        last_verification = _run_done_command(pytest_command, " ".join(pytest_command), repo_root)
        if not last_verification:
            return last_verification

    for raw_command in dc.commands:
        command = _coerce_done_command(raw_command)
        parsed_command = _parse_done_command(command.command)
        if isinstance(parsed_command, DoneVerification):
            return parsed_command
        command_cwd = _resolve_done_command_cwd(command.cwd, repo_root, command.command)
        if isinstance(command_cwd, DoneVerification):
            return command_cwd
        last_verification = _run_done_command(
            parsed_command,
            _format_done_command_label(command),
            command_cwd,
            env_overrides=command.env,
        )
        if not last_verification:
            return last_verification

    if dc.type == "hybrid":
        manual = dc.manual_desc or "Manual verification required."
        verification_label = "auto verification" if dc.commands else "auto tests"
        print(f"[DONE] {verification_label} passed; manual verification remains: {manual}")
        return DoneVerification(
            passed=True,
            state=RunState.NEEDS_HUMAN_VALIDATION,
            manual_follow_up=manual,
            command=last_verification.command,
            stdout_tail=last_verification.stdout_tail,
            stderr_tail=last_verification.stderr_tail,
        )

    return DoneVerification(
        passed=True,
        state=RunState.GREEN,
        command=last_verification.command,
        stdout_tail=last_verification.stdout_tail,
        stderr_tail=last_verification.stderr_tail,
    )


def _coerce_done_command(command: DoneCommand | str) -> DoneCommand:
    if isinstance(command, DoneCommand):
        return command
    return DoneCommand(command=str(command))


def _format_done_command_label(command: DoneCommand) -> str:
    if not command.cwd and not command.env:
        return command.command
    extras: list[str] = []
    if command.cwd:
        extras.append(f"cwd={command.cwd}")
    if command.env:
        extras.append("env=" + ",".join(sorted(command.env)))
    return f"{command.command} ({'; '.join(extras)})"


def _resolve_done_command_cwd(
    cwd: str | None,
    repo_root: Path,
    command: str,
) -> Path | DoneVerification:
    if not cwd:
        return repo_root
    candidate = (repo_root / cwd).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        return DoneVerification(
            passed=False,
            state=RunState.RED,
            command=command,
            stderr_tail=f"Done command cwd escapes repository: {cwd}",
        )
    if not candidate.is_dir():
        return DoneVerification(
            passed=False,
            state=RunState.RED,
            command=command,
            stderr_tail=f"Done command cwd is not a directory: {cwd}",
        )
    return candidate


def _parse_done_command(command: str) -> list[str] | DoneVerification:
    try:
        argv = _split_done_command(command)
    except ValueError as exc:
        return DoneVerification(
            passed=False,
            state=RunState.RED,
            command=command,
            stderr_tail=f"Could not parse Done command with shlex: {exc}",
        )
    if not argv:
        return DoneVerification(
            passed=False,
            state=RunState.RED,
            command=command,
            stderr_tail="Done command is empty.",
        )
    unsupported = next(
        (token for token in argv if token in UNSUPPORTED_DONE_SHELL_TOKENS),
        None,
    )
    if unsupported is not None:
        return DoneVerification(
            passed=False,
            state=RunState.RED,
            command=command,
            stderr_tail=(
                f"Unsupported shell token in Done command: {unsupported}. "
                "The command-list DSL runs commands with shell=False; split shell workflows into separate commands."
            ),
        )
    return argv


def _split_done_command(command: str) -> list[str]:
    if os.name == "nt":
        match = re.match(r"^([A-Za-z]:\\\S+)(?:\s+(.*))?$", command)
        if match:
            first = match.group(1)
            rest = match.group(2)
            return [first, *shlex.split(rest)] if rest else [first]
    return shlex.split(command)


def _run_done_command(
    argv: list[str],
    command: str,
    cwd: Path,
    env_overrides: dict[str, str] | None = None,
) -> DoneVerification:
    print(f"[TEST] Running: {command}")
    env = None
    if env_overrides:
        env = os.environ.copy()
        env.update(env_overrides)
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            shell=False,
            capture_output=True,
            text=True,
            timeout=DONE_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        return DoneVerification(
            passed=False,
            state=RunState.RED,
            command=command,
            stderr_tail=str(exc)[-DONE_STDERR_TAIL_CHARS:],
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_subprocess_output(exc.stdout)
        stderr = _coerce_subprocess_output(exc.stderr)
        if stderr:
            stderr = f"{stderr}\n"
        stderr = f"{stderr}Command timed out after {DONE_COMMAND_TIMEOUT_SECONDS} seconds."
        return DoneVerification(
            passed=False,
            state=RunState.RED,
            command=command,
            stdout_tail=stdout[-DONE_STDOUT_TAIL_CHARS:],
            stderr_tail=stderr[-DONE_STDERR_TAIL_CHARS:],
        )

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    stdout_tail = stdout[-DONE_STDOUT_TAIL_CHARS:]
    stderr_tail = stderr[-DONE_STDERR_TAIL_CHARS:]
    if stdout_tail:
        print(stdout_tail)
    if stderr_tail:
        print(stderr_tail)
    passed = result.returncode == 0
    return DoneVerification(
        passed=passed,
        state=RunState.GREEN if passed else RunState.RED,
        command=command,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )


def _coerce_subprocess_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def run_task_sync(**kwargs) -> bool:
    return asyncio.run(run_task(**kwargs))
