from __future__ import annotations

import re
from pathlib import Path


SCAFFOLD_DIRS = [
    "interfaces",
    "knowledge",
    "plans/lodd-runner/codex-sdk-migration/tasks",
    "plans/lodd-runner/codex-sdk-migration/iterations",
    ".codex/.agent/lodd",
]


SCAFFOLD_FILES = {
    "AGENTS.md": "# AGENTS.md\n\nFollow the LoDD repository rules in this file.\n",
    "architecture.md": "# Architecture\n\nLoDD Runner orchestrates Codex SDK task runs.\n",
    "plans/lodd-runner/overview.md": "# lodd-runner Overview\n\nCurrent phase: codex-sdk-migration.\n",
    "plans/lodd-runner/codex-sdk-migration/specification.md": "# Specification\n\nMigrate LoDD Runner to Codex SDK.\n",
    "plans/lodd-runner/codex-sdk-migration/decision_log.md": "# Decision Log\n\n",
    "plans/lodd-runner/codex-sdk-migration/work_log.md": "# Work Log\n\n",
    ".codex/config.toml": "[features]\nchild_agents_md = true\n",
    ".codex/.agent/lodd/README.md": "# LoDD Agent Metadata\n\nProject-local LoDD metadata.\n",
}

VALID_PROFILES = {"single", "module"}
VALID_EXPECTED_LIFESPANS = {"single-shot", "short-term", "long-term"}


def init_phase_scaffold(repo_root: Path, *, force: bool = False) -> list[Path]:
    """Create the historical LoDD Runner migration scaffold.

    This command is intentionally kept for backwards compatibility. New user
    workspaces should prefer init_project_scaffold(...).
    """
    created: list[Path] = []
    for rel_dir in SCAFFOLD_DIRS:
        path = repo_root / rel_dir
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)

    for rel_file, content in SCAFFOLD_FILES.items():
        path = repo_root / rel_file
        if force or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            created.append(path)

    keep = repo_root / "plans/lodd-runner/codex-sdk-migration/iterations/.gitkeep"
    if force or not keep.exists():
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("", encoding="utf-8")
        created.append(keep)

    return created


def init_project_scaffold(
    repo_root: Path,
    *,
    profile: str,
    tool_name: str,
    dcc: str,
    input_desc: str,
    output_desc: str,
    expected_lifespan: str,
    modules: list[str] | None = None,
    workstream: str | None = None,
    phase: str | None = None,
    force: bool = False,
) -> list[Path]:
    """Create a draft LoDD workspace scaffold from the reference profiles."""
    profile = profile.strip().lower()
    if profile not in VALID_PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(sorted(VALID_PROFILES))}")
    expected_lifespan = normalize_expected_lifespan(expected_lifespan)
    if profile == "module":
        modules = _parse_modules(modules or [])
        if not modules:
            raise ValueError("--modules is required for --profile module")
        if not workstream:
            raise ValueError("--workstream is required for --profile module")
        if not phase:
            raise ValueError("--phase is required for --profile module")
    else:
        modules = [_slugify(tool_name, default="tool")]

    if profile == "single":
        return _init_single_scaffold(
            repo_root,
            tool_name=tool_name,
            dcc=dcc,
            input_desc=input_desc,
            output_desc=output_desc,
            expected_lifespan=expected_lifespan,
            interface_slug=modules[0],
            force=force,
        )
    return _init_module_scaffold(
        repo_root,
        tool_name=tool_name,
        dcc=dcc,
        input_desc=input_desc,
        output_desc=output_desc,
        expected_lifespan=expected_lifespan,
        modules=modules,
        workstream=workstream or "workstream",
        phase=phase or "phase",
        force=force,
    )


def normalize_expected_lifespan(raw: str) -> str:
    value = re.sub(r"\s+", " ", raw.strip().lower())
    if value == "single-shot":
        return "single-shot"
    if value.startswith("short-term"):
        return "short-term"
    if value == "long-term":
        return "long-term"
    raise ValueError("--expected-lifespan must be single-shot, short-term, or long-term")


def _init_single_scaffold(
    repo_root: Path,
    *,
    tool_name: str,
    dcc: str,
    input_desc: str,
    output_desc: str,
    expected_lifespan: str,
    interface_slug: str,
    force: bool,
) -> list[Path]:
    tool_slug = _slugify(tool_name, default="tool")
    dcc_slug = _slugify(dcc, default="dcc")
    task_rel = "tasks/task-001.md"
    interface_rel = f"interfaces/{interface_slug}.md"
    knowledge_rel = f"knowledge/dcc_{dcc_slug}.md"
    src_rel = f"src/{tool_slug}.py"
    created: list[Path] = []

    for rel_dir in ["interfaces", "knowledge", "tasks", "iterations/task-001"]:
        _ensure_dir(repo_root / rel_dir, created)

    files = [
        ("AGENTS.md", _agents_template(tool_name, dcc)),
        ("architecture.md", _architecture_template(tool_name, input_desc, output_desc, expected_lifespan, profile="single")),
        (knowledge_rel, _dcc_template(dcc)),
        (interface_rel, _interface_template(interface_slug, tool_name, input_desc, output_desc)),
        (task_rel, _task_template(
            task_number=1,
            title=f"Implement {tool_name} draft task",
            tool_name=tool_name,
            dcc=dcc,
            input_desc=input_desc,
            output_desc=output_desc,
            reads=[
                "AGENTS.md # why: LoDD repository rules and DCC constraints",
                "architecture.md # why: purpose, data flow, and Tool Lifecycle metadata",
                f"{interface_rel} # why: contract draft for the tool boundary",
                f"{knowledge_rel} # why: DCC-specific constraints draft",
            ],
            writes=[f"{src_rel} # why: draft implementation target placeholder"],
        )),
        ("iterations/task-001/.gitkeep", ""),
    ]
    for rel_file, content in files:
        _write_file(repo_root / rel_file, content, created, force=force)
    return created


def _init_module_scaffold(
    repo_root: Path,
    *,
    tool_name: str,
    dcc: str,
    input_desc: str,
    output_desc: str,
    expected_lifespan: str,
    modules: list[str],
    workstream: str,
    phase: str,
    force: bool,
) -> list[Path]:
    dcc_slug = _slugify(dcc, default="dcc")
    workstream_slug = _slugify(workstream, default="workstream")
    phase_slug = _slugify(phase, default="phase")
    phase_prefix = f"plans/{workstream_slug}/{phase_slug}"
    knowledge_rel = f"knowledge/dcc_{dcc_slug}.md"
    task_rel = f"{phase_prefix}/tasks/task-001.md"
    interface_rels = [f"interfaces/{module}.md" for module in _dedupe_slugs(modules)]
    created: list[Path] = []

    for rel_dir in [
        "interfaces",
        "knowledge",
        f"plans/{workstream_slug}",
        f"{phase_prefix}/tasks",
        f"{phase_prefix}/iterations/task-001",
    ]:
        _ensure_dir(repo_root / rel_dir, created)

    files: list[tuple[str, str]] = [
        ("AGENTS.md", _agents_template(tool_name, dcc)),
        ("architecture.md", _architecture_template(tool_name, input_desc, output_desc, expected_lifespan, profile="module")),
        (knowledge_rel, _dcc_template(dcc)),
        (f"plans/{workstream_slug}/overview.md", _overview_template(tool_name, workstream_slug, phase_slug)),
        (f"{phase_prefix}/specification.md", _specification_template(tool_name, phase_slug, input_desc, output_desc, interface_rels)),
        (f"{phase_prefix}/decision_log.md", "# Decision Log\n\nDraft: review-required before relying on these decisions as durable LoDD history.\n"),
        (f"{phase_prefix}/work_log.md", "# Work Log\n\nDraft: append only human-reviewed Phase history here.\n"),
    ]
    for module, interface_rel in zip(_dedupe_slugs(modules), interface_rels):
        files.append((interface_rel, _interface_template(module, tool_name, input_desc, output_desc)))
    files.extend([
        (task_rel, _task_template(
            task_number=1,
            title=f"Define {tool_name} module boundary draft",
            tool_name=tool_name,
            dcc=dcc,
            input_desc=input_desc,
            output_desc=output_desc,
            reads=[
                "AGENTS.md # why: LoDD repository rules and DCC constraints",
                "architecture.md # why: purpose, data flow, and Tool Lifecycle metadata",
                f"plans/{workstream_slug}/overview.md # why: workstream context",
                f"{phase_prefix}/specification.md # why: phase scope and task split draft",
                f"{knowledge_rel} # why: DCC-specific constraints draft",
                *(f"{interface_rel} # why: module contract draft" for interface_rel in interface_rels),
            ],
            writes=[f"src/{module}.py # why: draft module implementation target placeholder" for module in _dedupe_slugs(modules)],
        )),
        (f"{phase_prefix}/iterations/task-001/.gitkeep", ""),
    ])
    for rel_file, content in files:
        _write_file(repo_root / rel_file, content, created, force=force)
    return created


def _agents_template(tool_name: str, dcc: str) -> str:
    return f"""# AGENTS.md

Draft LoDD workspace rules for {tool_name}.

- Review-required draft: fill in project-specific invariants before live task execution.
- Treat `architecture.md` as the local architecture contract after human review.
- Keep task Context Boundary Read/Write entries narrow and explain why each entry is present.
- Do not edit files outside the task's declared Write boundary.
- Target DCC: {dcc}. Check matching `knowledge/dcc_*` notes before tasks that depend on DCC behavior.
"""


def _architecture_template(
    tool_name: str,
    input_desc: str,
    output_desc: str,
    expected_lifespan: str,
    *,
    profile: str,
) -> str:
    return f"""# Architecture

Draft architecture for {tool_name}. Human review is required before treating this file as the durable contract.

## Purpose

Describe the stable purpose of {tool_name} here.

## Input

{input_desc}

## Output

{output_desc}

## Scaffold Profile

- profile: {profile}

## Tool Lifecycle

- expected_lifespan: {expected_lifespan}
- repayment_policy_note: Draft value from LoDD scaffold; review before relying on repayment priority decisions.

## Data Flow

Draft the main data flow here.

## Responsibility Boundaries

Draft module and DCC responsibility boundaries here.

## Interface

See `interfaces/` for draft contracts.
"""


def _dcc_template(dcc: str) -> str:
    return f"""# DCC Constraints: {dcc}

Draft DCC knowledge file. Human review is required before relying on this as durable project knowledge.

## Known Constraints

- Record {dcc}-specific API, scene, file-format, or process constraints here.

## Open Questions

- Confirm which constraints must be included in task Context Boundary Read entries.
"""


def _interface_template(slug: str, tool_name: str, input_desc: str, output_desc: str) -> str:
    return f"""# Interface: {slug}

Draft interface contract for {tool_name}. Human review is required before implementation tasks rely on this file.

## Data Flow

- Input draft: {input_desc}
- Output draft: {output_desc}

## Responsibility Boundaries

- Define what this boundary owns.
- Define what callers and neighboring modules own.

## Interface

- Signatures / commands / file contracts: TODO after human review.
- Invariants: TODO after human review.
"""


def _overview_template(tool_name: str, workstream: str, phase: str) -> str:
    return f"""# {workstream} Overview

Draft workstream overview for {tool_name}. Human review is required.

- current_phase: {phase}
- goal: TODO
- non_goals: TODO
"""


def _specification_template(
    tool_name: str,
    phase: str,
    input_desc: str,
    output_desc: str,
    interface_rels: list[str],
) -> str:
    interface_lines = "\n".join(f"- {path}" for path in interface_rels)
    return f"""# Specification: {phase}

Draft phase specification for {tool_name}. Human review is required.

## Purpose

TODO: define exact phase purpose.

## Input

{input_desc}

## Output

{output_desc}

## Interface Drafts

{interface_lines}

## Task Boundary Notes

Split implementation into narrow tasks after reviewing signatures, invariants, and Context Boundary dependencies.
"""


def _task_template(
    *,
    task_number: int,
    title: str,
    tool_name: str,
    dcc: str,
    input_desc: str,
    output_desc: str,
    reads: list[str],
    writes: list[str],
) -> str:
    read_lines = "\n".join(f"  - {entry}" for entry in reads)
    write_lines = "\n".join(f"  - {entry}" for entry in writes)
    return f"""# Task-{task_number:03d}: {title}

## Status

Not Started

## Input in the prompt

- Review-required scaffold draft for {tool_name}.
- Target DCC: {dcc}.
- Input: {input_desc}
- Output: {output_desc}
- Before execution, replace placeholder signatures, invariants, task boundaries, and Done commands with reviewed project-specific details.

## Context Boundary

- Read:
{read_lines}
- Write:
{write_lines}

## Functional Contract

- Done条件:
  - type: manual
  - manual: Human reviews and replaces scaffold placeholders before implementation.

## Strict Constraints

- Do not treat scaffold placeholders as reviewed production contracts.
- Do not broaden Context Boundary entries without adding why-comments.

## Iteration Control

- current_iteration: 1
- history: []
- current_delta_instruction: ""

## Retrospective

- result_state: green | red | breach | needs-human-validation
- iterations: 0
- boundary_breaches: 0
- knowledge_added: 0
- manual_follow_up: None

## Debt Markers

- unreviewed_functions: None
- boundary_issues: None
- knowledge_gaps: None
"""


def _ensure_dir(path: Path, created: list[Path]) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        created.append(path)


def _write_file(path: Path, content: str, created: list[Path], *, force: bool) -> None:
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    created.append(path)


def _parse_modules(raw_modules: list[str]) -> list[str]:
    names: list[str] = []
    for raw in raw_modules:
        names.extend(part.strip() for part in raw.split(",") if part.strip())
    return _dedupe_slugs([_slugify(name, default="module") for name in names])


def _dedupe_slugs(slugs: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    result: list[str] = []
    for slug in slugs:
        base = slug or "module"
        counts[base] = counts.get(base, 0) + 1
        result.append(base if counts[base] == 1 else f"{base}-{counts[base]}")
    return result


def _slugify(value: str, *, default: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or default
