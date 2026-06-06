from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.command == "init":
        from .scaffold import init_project_scaffold

        repo = Path(args.repo).resolve()
        try:
            created = init_project_scaffold(
                repo,
                profile=args.profile,
                tool_name=args.tool_name,
                dcc=args.dcc,
                input_desc=args.input,
                output_desc=args.output_desc,
                expected_lifespan=args.expected_lifespan,
                modules=args.modules,
                workstream=args.workstream,
                phase=args.phase,
                force=args.force,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)
        if created:
            print("Created or refreshed:")
            for path in created:
                print(f"  {_display_path(path, repo)}")
        else:
            print("LoDD scaffold already exists.")
        return

    if args.command == "init-phase":
        from .scaffold import init_phase_scaffold

        repo = Path(args.repo).resolve()
        created = init_phase_scaffold(repo, force=args.force)
        if created:
            print("Created or refreshed:")
            for path in created:
                print(f"  {path}")
        else:
            print("LoDD Phase scaffold already exists.")
        return

    if args.command in {"retrospective-summary", "summary"}:
        from .summary import format_retrospective_summary, summarize_retrospectives

        summary = summarize_retrospectives(args.path)
        print(format_retrospective_summary(summary))
        return

    if args.command == "repayment-plan":
        from .summary import (
            find_architecture_file,
            format_repayment_plan,
            read_tool_lifecycle,
            summarize_retrospectives,
            write_repayment_plan,
            write_repayment_work_log_template,
        )

        root = Path(args.path).resolve()
        architecture = Path(args.architecture).resolve() if args.architecture else find_architecture_file(root)
        expected_lifespan = None
        lifecycle_note = None
        if architecture is not None:
            try:
                architecture.relative_to(root)
            except ValueError:
                print(f"Error: Architecture file must be under repayment root: {architecture}", file=sys.stderr)
                sys.exit(1)
            expected_lifespan, lifecycle_note = read_tool_lifecycle(architecture)
        summary = summarize_retrospectives(root)
        plan = format_repayment_plan(summary, expected_lifespan, lifecycle_note)
        print(plan, end="")
        if args.output:
            output_path = write_repayment_plan(summary, args.output, expected_lifespan, lifecycle_note)
            print(f"[repayment-plan] markdown: {output_path}")
        if args.work_log_template:
            template_path = write_repayment_work_log_template(
                summary,
                args.work_log_template,
                expected_lifespan,
                lifecycle_note,
            )
            print(f"[repayment-plan] work-log-template: {template_path}")
        return

    if args.command == "cycle-review":
        from .summary import (
            find_architecture_file,
            format_cycle_review,
            read_tool_lifecycle,
            summarize_retrospectives,
            write_cycle_review,
        )

        root = Path(args.path).resolve()
        architecture = Path(args.architecture).resolve() if args.architecture else find_architecture_file(root)
        expected_lifespan = None
        lifecycle_note = None
        if architecture is not None:
            try:
                architecture.relative_to(root)
            except ValueError:
                print(f"Error: Architecture file must be under cycle-review root: {architecture}", file=sys.stderr)
                sys.exit(1)
            expected_lifespan, lifecycle_note = read_tool_lifecycle(architecture)
        summary = summarize_retrospectives(root)
        review = format_cycle_review(summary, expected_lifespan, lifecycle_note)
        print(review, end="")
        if args.output:
            output_path = write_cycle_review(summary, args.output, expected_lifespan, lifecycle_note)
            print(f"[cycle-review] markdown: {output_path}")
        return

    if args.command == "closeout-checklist":
        from .iteration import build_closeout_checklist, write_closeout_checklist

        task_path, repo_root = _resolve_paths(args.task, args.repo)
        checklist = build_closeout_checklist(task_path, repo_root)
        print(checklist, end="")
        if args.output:
            output_path = write_closeout_checklist(task_path, repo_root, args.output)
            print(f"[closeout-checklist] markdown: {output_path}")
        return

    if args.command == "handoff":
        from .handoff import build_new_chat_handoff, write_new_chat_handoff

        task_path, repo_root = _resolve_paths(args.task, args.repo)
        packet = build_new_chat_handoff(task_path, repo_root)
        print(packet, end="")
        if args.output:
            output_path = write_new_chat_handoff(task_path, repo_root, args.output)
            print(f"[handoff] markdown: {output_path}")
        return

    if args.command == "pr-readiness":
        from .reports import classify_pr_readiness, format_pr_readiness, read_pr_readiness_report

        report = read_pr_readiness_report(args.report_json)
        print(format_pr_readiness(classify_pr_readiness(report)), end="")
        return

    if args.command == "lint-task":
        from .linting import format_task_lint_result, lint_task

        task_path, repo_root = _resolve_paths(args.task, args.repo)
        if not task_path.exists():
            print(f"Error: Task file not found: {task_path}", file=sys.stderr)
            sys.exit(1)
        result = lint_task(
            task_path,
            repo_root,
            strict_task_format=args.strict_task_format,
            boundary_review=args.boundary_review,
        )
        print(format_task_lint_result(result))
        sys.exit(0 if result.ok else 1)

    task_path, repo_root = _resolve_paths(args.task, args.repo)
    plan_path = Path(args.plan).resolve() if args.plan else None

    if not task_path.exists():
        print(f"Error: Task file not found: {task_path}", file=sys.stderr)
        sys.exit(1)
    if plan_path and not plan_path.exists():
        print(f"Error: Plan file not found: {plan_path}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        _dry_run(task_path, repo_root, plan_path, args.generate_plan)
        return

    from .runner import RunState, run_task_with_result

    result = asyncio.run(
        run_task_with_result(
            task_md_path=task_path,
            repo_root=repo_root,
            model=args.model,
            max_iterations=args.max_iterations,
            plan_path=plan_path,
            generate_plan=args.generate_plan,
            thread_id=args.thread_id,
            workspace_mode=args.sandbox,
            strict_ld005=args.strict_ld005,
        )
    )
    handoff_artifact = None
    if args.handoff_md:
        from .handoff import write_new_chat_handoff

        handoff_artifact = write_new_chat_handoff(task_path, repo_root, args.handoff_md)
        print(f"[handoff] markdown: {handoff_artifact}")
    if args.report_json:
        from .reports import write_run_report

        report_path = write_run_report(result, args.report_json, handoff_artifact=handoff_artifact)
        print(f"[report] json: {report_path}")
    if args.strict_ci and result.state == RunState.NEEDS_HUMAN_VALIDATION:
        sys.exit(1)
    sys.exit(0 if result.ok else 1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run LoDD tasks with the Codex Python SDK.")
    subparsers = parser.add_subparsers(dest="command")

    init_project = subparsers.add_parser("init", help="Create a draft LoDD workspace scaffold.")
    init_project.add_argument("--repo", default=".", help="Repository root for generated files.")
    init_project.add_argument("--profile", choices=["single", "module"], required=True, help="Scaffold profile.")
    init_project.add_argument("--tool-name", required=True, help="Tool or process name.")
    init_project.add_argument("--dcc", required=True, help="Target DCC or host process.")
    init_project.add_argument("--input", required=True, help="Draft input description.")
    init_project.add_argument("--output", dest="output_desc", required=True, help="Draft output description.")
    init_project.add_argument("--expected-lifespan", required=True, help="single-shot, short-term, or long-term.")
    init_project.add_argument("--modules", nargs="*", default=None, help="Module names for module profile; accepts repeated or comma-separated values.")
    init_project.add_argument("--workstream", default=None, help="Workstream name for module profile.")
    init_project.add_argument("--phase", default=None, help="Phase name for module profile.")
    init_project.add_argument("--force", action="store_true", help="Overwrite scaffold files.")

    init = subparsers.add_parser("init-phase", help="Create this repo's Phase LoDD scaffold.")
    init.add_argument("--repo", default=".", help="Repository root.")
    init.add_argument("--force", action="store_true", help="Overwrite scaffold files.")

    summary = subparsers.add_parser(
        "retrospective-summary",
        aliases=["summary"],
        help="Summarize completed task retrospectives under a phase or flat task root.",
    )
    summary.add_argument("path", help="Phase directory or flat root containing tasks/.")

    repayment = subparsers.add_parser(
        "repayment-plan",
        help="Generate a local Markdown repayment plan from completed-task retrospectives.",
    )
    repayment.add_argument("path", help="Phase directory or flat root containing tasks/.")
    repayment.add_argument("--architecture", default=None, help="Optional architecture.md with Tool Lifecycle metadata.")
    repayment.add_argument("--output", default=None, help="Optional Markdown output path.")
    repayment.add_argument(
        "--work-log-template",
        default=None,
        help="Optional bounded repayment sprint work_log.md entry draft path.",
    )

    cycle = subparsers.add_parser(
        "cycle-review",
        help="Generate bounded R-006 retrospective trend review Markdown.",
    )
    cycle.add_argument("path", help="Phase directory or flat root containing tasks/.")
    cycle.add_argument("--architecture", default=None, help="Optional architecture.md with Tool Lifecycle metadata.")
    cycle.add_argument("--output", default=None, help="Optional Markdown output path.")

    closeout = subparsers.add_parser(
        "closeout-checklist",
        help="Print a non-mutating iteration closeout checklist for one task.",
    )
    closeout.add_argument("--task", required=True, help="Path to task-xxx.md.")
    closeout.add_argument("--repo", default=None, help="Repository root. Defaults to inferred root.")
    closeout.add_argument("--output", default=None, help="Optional Markdown output path.")

    handoff = subparsers.add_parser(
        "handoff",
        help="Print a compact New Chat handoff packet for a task.",
    )
    handoff.add_argument("--task", required=True, help="Path to task-xxx.md.")
    handoff.add_argument("--repo", default=None, help="Repository root. Defaults to inferred root.")
    handoff.add_argument("--output", default=None, help="Optional Markdown output path.")

    pr_readiness = subparsers.add_parser(
        "pr-readiness",
        help="Classify a run report for future PR publishing readiness without network access.",
    )
    pr_readiness.add_argument("--report-json", required=True, help="Path to a bounded run report JSON file.")

    lint = subparsers.add_parser(
        "lint-task",
        help="Lint a LoDD task file without calling Codex.",
    )
    lint.add_argument("--task", required=True, help="Path to task-xxx.md.")
    lint.add_argument("--repo", default=None, help="Repository root. Defaults to inferred root.")
    lint.add_argument(
        "--strict-task-format",
        action="store_true",
        help="Exit non-zero for strict task authoring diagnostics and preflight/config failures.",
    )
    lint.add_argument(
        "--boundary-review",
        action="store_true",
        help="Print advisory Context Boundary dependency review prompts.",
    )

    parser.add_argument("--task", help="Path to task-xxx.md.")
    parser.add_argument("--repo", default=None, help="Repository root. Defaults to inferred root.")
    parser.add_argument("--model", default=None, help="Codex model override.")
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--plan", default=None, help="Optional implementation plan file.")
    parser.add_argument("--generate-plan", action="store_true")
    parser.add_argument("--thread-id", default=None, help="Resume an existing Codex thread.")
    parser.add_argument(
        "--sandbox",
        choices=["repo-root", "materialized"],
        default="repo-root",
        help="Runner workspace mode. Default: repo-root.",
    )
    parser.add_argument(
        "--strict-ci",
        action="store_true",
        help="Exit non-zero for needs-human-validation in normal task execution.",
    )
    parser.add_argument(
        "--strict-ld005",
        action="store_true",
        help="Convert LD-005 broad-refactor warnings into red run failures unless refactoring is explicitly allowed.",
    )
    parser.add_argument(
        "--report-json",
        default=None,
        help="Write a machine-readable run report JSON file while preserving human-readable stdout.",
    )
    parser.add_argument(
        "--handoff-md",
        default=None,
        help="Write a compact New Chat handoff Markdown artifact after task execution.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser



def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)

def _resolve_paths(task_arg: str | None, repo_arg: str | None) -> tuple[Path, Path]:
    if not task_arg:
        raise SystemExit("Error: --task is required unless using a scaffold or report subcommand.")

    from .task_parser import infer_repo_root

    task_raw = Path(task_arg)
    if repo_arg:
        repo_root = Path(repo_arg).resolve()
        task_path = task_raw if task_raw.is_absolute() else repo_root / task_raw
        return task_path.resolve(), repo_root

    task_path = task_raw.resolve() if task_raw.is_absolute() else (Path.cwd() / task_raw).resolve()
    return task_path, infer_repo_root(task_path)


def _dry_run(task_path: Path, repo_root: Path, plan_path: Path | None, generate_plan: bool) -> None:
    from .context_builder import (
        build_developer_instructions,
        build_task_instruction,
        collect_context_files,
        collect_phase_context,
    )
    from .boundary import format_boundary_preflight, validate_task_boundary
    from .task_parser import (
        StructureType,
        collect_task_diagnostics,
        format_task_diagnostics,
        get_iterations_dir,
        parse_task_md,
    )

    task = parse_task_md(task_path, repo_root)
    diagnostics = collect_task_diagnostics(task_path)
    preflight = validate_task_boundary(
        task.boundary,
        repo_root,
        require_write=not generate_plan,
    )
    context_files = collect_context_files(task, repo_root)
    phase_files = collect_phase_context(task)
    plan_content = plan_path.read_text(encoding="utf-8") if plan_path else None
    developer_instructions = build_developer_instructions(
        task, context_files, repo_root, generate_plan
    )
    instruction = build_task_instruction(
        task, plan_content, generate_plan, repo_root=repo_root
    )

    print("=" * 60)
    print("DRY RUN - Codex SDK is not called")
    print("=" * 60)
    print(f"Task:      {task.task_id}")
    print(f"Title:     {task.title}")
    print(f"Status:    {task.status}")
    print(f"Structure: {task.structure.value}")
    if task.structure == StructureType.PHASE and task.phase_ctx:
        print(f"Workstream: {task.phase_ctx.workstream}")
        print(f"Phase:      {task.phase_ctx.phase}")
    print(f"Iterations: {get_iterations_dir(task, repo_root)}")
    print(f"Read:       {task.boundary.read}")
    print(f"Write:      {task.boundary.write}")
    print(format_boundary_preflight(preflight))
    print(format_task_diagnostics(diagnostics))
    print(f"Phase files: {list(phase_files)}")
    print(f"Context files: {list(context_files)}")
    print(f"Developer instructions: {len(developer_instructions)} chars")
    print(f"Turn input: {len(instruction)} chars")
    print("=" * 60)
    print("Effective task packet (Codex turn input)")
    print("=" * 60)
    print(instruction)


if __name__ == "__main__":
    main()
