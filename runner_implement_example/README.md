# runner_implement_example

LoDD runner 層の参考実装です。

このディレクトリは、`LoDD_Reference.md` を実装で理解するための最小サンプルです。LoDD 仕様そのものではありません。仕様上の source of truth は LoDD 本体の Reference document です。

## 含めているもの

- `lodd_runner/`: Python CLI 実装
- `pyproject.toml`: install / console script 定義
- `examples/minimal-task/`: `--dry-run` で parse と Context Boundary preflight を試すための最小 fixture

## 含めていないもの

- 開発用 task archive
- runner 設計ドキュメント一式
- テスト一式
- LoDD Reference 本文

## 位置づけ

`lodd_runner` は、LoDD の runner layer がどのように振る舞えるかを示す reference implementation です。

主に次を実装しています。

- Task Markdown parse
- Context Boundary preflight
- repo-root / materialized workspace execution
- LD-002 Write Boundary 検証
- LD-003 interface 変更検知の一部
- LD-004 Python dependency manifest 変更検知
- LD-005 broad refactor warning / strict mode
- auto / manual / hybrid Done condition
- green / red / breach / needs-human-validation 分類
- Iteration delta record
- Retrospective / Debt Markers summary
- New Chat handoff packet

## 注意点

この実装は syscall-level read audit を提供しません。

- `repo-root` mode では LD-001 は prompt-driven です。
- `materialized` mode では、agent workspace に見えるファイル集合を Context Boundary に沿って制限します。

また、LD-003 / LD-004 / LD-005 は完全な意味解析ではなく、実用上の heuristic を含みます。

## 使い方

Python 3.10+ が必要です。

```bash
python -m pip install -e .
```

Codex を呼ばずに task packet と preflight を確認するには、次を実行します。

```bash
python -m lodd_runner \
  --task tasks/task-001.md \
  --repo examples/minimal-task \
  --dry-run
```

console script を使う場合は次です。

```bash
lodd-runner \
  --task tasks/task-001.md \
  --repo examples/minimal-task \
  --dry-run
```

help は次で確認できます。

```bash
python -m lodd_runner --help
```

## LoDD repo へ置く場合の推奨パス

```text
runners/runner_implement_example/
```

LoDD 本体 README からリンクする場合は、次のように説明するのが安全です。

```text
A minimal reference implementation of the LoDD runner layer. The LoDD Reference remains the source of truth.
```
