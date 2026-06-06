from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath, Path
from typing import Any

CONFIG_FILE_NAME = "lodd-runner.json"
GLOB_CHARS = set("*?[]")


@dataclass(frozen=True)
class RunnerConfigIssue:
    code: str
    message: str


@dataclass
class RunnerConfig:
    snapshot_ignore_prefixes: list[str] = field(default_factory=list)
    snapshot_ignore_names: list[str] = field(default_factory=list)
    issues: list[RunnerConfigIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def load_runner_config(repo_root: str | Path) -> RunnerConfig:
    root = Path(repo_root).resolve()
    path = root / CONFIG_FILE_NAME
    if not path.is_file():
        return RunnerConfig()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return RunnerConfig(
            issues=[
                RunnerConfigIssue(
                    code="CONFIG-INVALID-JSON",
                    message=f"{CONFIG_FILE_NAME} is not valid JSON: {exc}",
                )
            ]
        )

    if not isinstance(raw, dict):
        return RunnerConfig(
            issues=[
                RunnerConfigIssue(
                    code="CONFIG-INVALID-SHAPE",
                    message=f"{CONFIG_FILE_NAME} must contain a JSON object.",
                )
            ]
        )

    config = RunnerConfig()
    config.snapshot_ignore_prefixes = _validated_string_list(
        raw.get("snapshot_ignore_prefixes", []),
        "snapshot_ignore_prefixes",
        allow_slash=True,
        issues=config.issues,
    )
    config.snapshot_ignore_names = _validated_string_list(
        raw.get("snapshot_ignore_names", []),
        "snapshot_ignore_names",
        allow_slash=False,
        issues=config.issues,
    )
    return config


def format_runner_config_issues(config: RunnerConfig) -> str:
    lines = ["Runner config warnings:"]
    if not config.issues:
        lines.append("  None")
        return "\n".join(lines)
    for issue in config.issues:
        lines.append(f"  [warning] {issue.code}: {issue.message}")
    return "\n".join(lines)


def _validated_string_list(
    value: Any,
    field_name: str,
    *,
    allow_slash: bool,
    issues: list[RunnerConfigIssue],
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        issues.append(
            RunnerConfigIssue(
                code="CONFIG-INVALID-FIELD",
                message=f"{field_name} must be a list of strings.",
            )
        )
        return []

    validated: list[str] = []
    for item in value:
        if not isinstance(item, str):
            issues.append(
                RunnerConfigIssue(
                    code="CONFIG-INVALID-ENTRY",
                    message=f"{field_name} entries must be strings.",
                )
            )
            continue
        raw = item.replace("\\", "/").strip()
        error = _config_entry_error(raw, allow_slash=allow_slash)
        if error:
            issues.append(
                RunnerConfigIssue(
                    code="CONFIG-INVALID-ENTRY",
                    message=f"Invalid {field_name} entry {item!r}: {error}",
                )
            )
            continue
        validated.append(_normalize_config_path(raw))
    return list(dict.fromkeys(validated))


def _normalize_config_path(value: str) -> str:
    return value.replace("\\", "/").strip().strip("/")


def _config_entry_error(value: str, *, allow_slash: bool) -> str:
    normalized = _normalize_config_path(value)
    if not normalized:
        return "empty entries are not allowed"
    if any(char in value for char in GLOB_CHARS):
        return "glob characters are not supported"
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        return "absolute paths are not allowed"
    parts = PurePosixPath(normalized).parts
    if any(part == ".." for part in parts):
        return "parent directory references are not allowed"
    if not allow_slash and "/" in normalized:
        return "names must not contain path separators"
    if any(part in {"", "."} for part in parts):
        return "dot or empty path components are not allowed"
    return ""
