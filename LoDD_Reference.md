# LoDD (Locality-Oriented / Lock-down Driven) リファレンス

[![License: CC BY 4.0](https://img.shields.io/badge/License-CC%20BY%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by/4.0/)

- **Author:** Shouichi Kanbara (pikovolt)
- **Status:** Release v1.0 (2026-03-10)
- **Type:** Framework Specification

---

## 設計思想

> **「LLMを信頼しないことで、LLMを最大限活用する」**

LLMは確率的モデルであり、コンテキストが広がるほど精度という密度が薄まる。
LoDDは、AIを特定の「局所（Locality）」に閉じ込め、関係ないファイルへのアクセスや
勝手な変更を構造的に遮断（Lock-down）することで、LLMを「仕様のトランスパイラ」として稼働させる。

「疎結合でいろ」と命令するのではなく、**「疎結合でしかいられない」**状態を構造で作り出す。

> **核心原理: 不確定コストの確定コストへの変換**
>
> AIが壊すかもしれないコスト（不確定・青天井）を、
> 人間が事前に書くコスト（確定・有限）に変換する。

### 4つの設計原則

| 原則 | 内容 |
|------|------|
| **局所性（Locality）** | AIが「今、ここ」の責務だけに集中できる環境を作る |
| **封鎖（Lock-down）** | Context Boundaryによって関係ないファイルへのアクセスを構造的に遮断する |
| **構造による制御** | プロンプトでの指示ではなく、物理的な情報の分離で制御する |
| **手戻りのなさ** | 一撃の速さを捨て、開発期間全体のスループットを最大化する |

### 想定スケール

- チーム規模: 1〜3人
- コード規模: 200〜300行の単一ファイルから、500行程度のモジュール十数個まで
- サイクル: 数日〜10日
- 対象領域: 3DCG環境向けツール・パイプライン処理

### 6つの防壁

このフレームワークの全要素は、以下の防壁として機能する。

| 防壁 | 対処する問題 | 実現手段 |
|------|------------|---------|
| **仕様の迷子防止** | AIが「このシステムは何か」を見失う | `architecture.md` + `AGENTS.md` |
| **契約違反の阻止** | AIが勝手にインターフェースを変更する | `interfaces/` + Context Boundary |
| **コンテキスト汚染の防止** | 過去の失敗ログが現在の実装を汚染する | `iterations/` の隔離とパージ + New Chat再投入 |
| **過剰修正の防止** | エラー修正時に無関係なコードまで変更する | Iteration Controlの差分指示 |
| **境界突破の検知** | AIがスコープ外に手を伸ばす | Lock-down Rules |
| **AI負債の可視化** | 負債が潜伏したまま蓄積する | Debt Markers + 返済スプリント |

### 損益分岐点

| 条件 | LoDDが割に合う | LoDDが過剰 |
|------|--------------|-----------|
| コードの寿命 | short-term以上（数週間〜） | single-shot（使い捨て） |
| モジュール数 | 2以上 | 単一ファイルで完結 |
| DCC APIの関与 | あり（地雷がある） | なし（純粋なデータ処理） |
| サイクルの反復 | 同種の作業が繰り返される | 一度きりの作業 |
| knowledge/ の蓄積 | 既にいくつかある（再利用効果が出る） | ゼロからのスタート |

最初のサイクルが一番重く、回すほど軽くなる。初回の重さで諦めるのが最大のリスクである。

---

## 構造選択

```text
・単機能 / 直線的 / 1〜3タスク → Flat構造
・複数モジュール協調 / 構造的分解 / 4タスク以上 → Phase構造
```

判定基準は行数ではなく、依存関係の密度と分解の必要性。

---

## Flat構造

```text
repo/
 ├ AGENTS.md
 ├ architecture.md
 ├ interfaces/
 │  └ <module_name>.md
 ├ knowledge/
 │  └ <prefix>_<topic>.md
 ├ tasks/
 │  └ task-001.md
 ├ iterations/
 │  └ task-001/
 │      └ iter-01.md
 ├ src/
 └ tests/
```

## Phase構造

```text
repo/
 ├ AGENTS.md
 ├ architecture.md
 ├ interfaces/
 │  └ <module_name>.md
 ├ knowledge/
 │  └ <prefix>_<topic>.md
 ├ plans/
 │  └ <workstream>/
 │      ├ overview.md
 │      └ <phase>/
 │          ├ specification.md
 │          ├ decision_log.md
 │          ├ work_log.md
 │          ├ tasks/
 │          │  └ task-001.md
 │          └ iterations/
 │              └ task-001/
 │                  └ iter-01.md
 ├ src/
 └ tests/
```

---

## 各要素の説明

### `AGENTS.md`

AIへの全体ルールブック。リポジトリ内で最初に読ませるファイル。

**記載内容:**
- SSOTの優先順位（`interfaces/` > `architecture.md` > `tasks/`）
- 使用言語・DCC環境の制約

---

### `architecture.md`

システム全体の目的・設計方針を定義するSSOT。

**記載内容:**
- ツール/システムの目的（WHY）
- 全体アーキテクチャの概要（モジュール構成、データフロー）
- 技術的な設計判断とその理由
- 対象DCC環境の制約
- Tool Lifecycle（後述）

Flat構造では単一ファイル。Phase構造でも分割が必要になるまでは単一ファイルのまま運用する。

---

### `interfaces/`

モジュール間の契約を定義する**最重要ディレクトリ**。

**各ファイルの記載内容:**
- 関数/クラスのシグネチャ（引数の型、戻り値の型）
- 前提条件（Precondition）と保証状態（Postcondition）
- 副作用の有無と内容
- エラー時の挙動

**存在意義:** LLMが勝手にシグネチャを変更することを構造的に阻止する。200行のツールでもこのディレクトリの価値は変わらない。

**記載例:**

```markdown
## MeshData
- vertices: List[float]
  - 格納順序: [x0, y0, z0, x1, y1, z1, ...]
  - 座標系: Y-up, 右手系
  - 単位: メートル
  - 空間: ワールド座標（kWorld相当）
- normals: List[float]
  - 種別: per-vertex normal（face normalではない）
  - 格納順序: vertices と同一インデックス対応
  - 正規化: 済み（長さ1.0を保証）
```

解釈の余地を排除するため、座標系・単位系・空間・格納順序・種別の厳密な定義・不変条件を明記する。

---

### `knowledge/`

DCC固有の「正しい叩き方」および外部依存の使い方をプールするディレクトリ。

**入れるもの:** 実際のタスクで問題が発覚し、解決策がDCC上で動作確認済みのもの。社内ライブラリのAPI・使い方。フォーマット仕様。

**入れないもの:** 「いつか使うかもしれない」一般的なTips。

#### `interfaces/` との違い

| ディレクトリ | 定義するもの | 性質 |
|---|---|---|
| `interfaces/` | 入出力の契約（WHAT） | プロジェクト固有 |
| `knowledge/` | 外部依存の正しい使い方（HOW） | プロジェクト横断で再利用可能 |

#### 命名規則とプレフィクス分類

ファイル名にプレフィクスを付与し、知識の出自と変更権限を区別する。

| プレフィクス | 内容 | 変更権限 | 例 |
|-------------|------|---------|-----|
| `dcc_` | DCC APIの地雷情報 | 追記可能（DCC上での検証後） | `dcc_maya_mesh_api.md` |
| `lib_` | 社内ライブラリのAPI・使い方 | 読み取り専用（ライブラリ側の更新に追従） | `lib_studio_mesh_utils.md` |
| `fmt_` | データフォーマット仕様 | 読み取り専用 | `fmt_obj_specification.md` |

#### `dcc_` ファイルの記載例

```markdown
# Maya 2024: MFnMesh.getPoints の座標系
# 発見: geometry_pipeline Phase2, 2026-03-02
# 前提: Maya 2024.2。将来バージョンで修正の可能性あり

## 問題
getPoints() のデフォルトは kObject（ローカル座標）。
ワールド座標が必要な場合に無指定で呼ぶと座標が狂う。

## 正解
points = fn_mesh.getPoints(om2.MSpace.kWorld)
```

#### `lib_` ファイルの記載例

```markdown
# studio.mesh — スタジオ共通メッシュユーティリティ

## 概要
スタジオ共通のメッシュユーティリティライブラリ。Maya / Houdini 両対応。

## インポート
from studio.mesh import MeshConverter, validate_topology

## 主要API

### MeshConverter.to_triangles(mesh_data) -> TriMeshData
- 入力: MeshData（studio.mesh.MeshData 型）
- 出力: TriMeshData（全面が三角形に分割済み）
- 注意: n-gon は fan 方式で分割される。凹多角形では破綻する可能性あり

### validate_topology(mesh_data) -> List[TopologyError]
- 非多様体エッジ、孤立頂点を検出
- 空リストが返れば正常

## 既知の注意点
- MeshConverter はスレッドセーフではない（Maya のメインスレッドで使用すること）
- v2.3 以前は getPoints が kObject 固定。v2.4 で修正済み
```

#### 運用ルール

- `dcc_` の転記は人間がDCC上の挙動と突き合わせてレビューした上で行う
- `lib_` はライブラリの更新に合わせて人間が更新する（AIが勝手に変更しない）
- プロジェクトを跨いで持ち運べる資産として設計する

---

### `tasks/task-xxx.md`

AIワーカーへの指示書。1タスク = 1ファイル。
- タスク定義作成時のチェック：タスクを書く前に `knowledge/` のファイル一覧を確認し、関連するものを Context Boundary の Read に含めること

```markdown
# Task-001: <タスク名>

## Status
Not Started | In Progress | Done

## Input in the prompt
- 禁止事項（未指示のリファクタリング、シグネチャの無断変更など）

## Context Boundary
- Read:
  - interfaces/mesh_export.md          # MeshData の入出力契約を遵守するため
  - knowledge/dcc_maya_mesh_api.md     # getPoints の座標系トラップを回避するため
  - knowledge/lib_studio_mesh_utils.md # 社内ライブラリの契約
- Write:
  - src/exporters/obj_exporter.py      # 本タスクの実装対象
  - tests/test_obj_exporter.py         # Done条件(auto)の実体

## Functional Contract
- 入力: MeshData オブジェクト
- 出力: .obj ファイル（指定パスに書き出し）
- Done条件:
  - type: auto | manual | hybrid
  - auto: tests/test_obj_exporter.py が全パス
  - manual: Maya上で目視確認する内容（該当する場合）

## Strict Constraints
- MeshData のインターフェースを変更しない
- 標準ライブラリ以外の外部パッケージを使用しない

## Iteration Control
- current_iteration: 1
- history: []
- current_delta_instruction: ""

## Retrospective（タスク完了時に記入）
- iterations: 0
- boundary_breaches: 0
- knowledge_added: 0

## Debt Markers（タスク完了時に記入）
- unreviewed_functions: []
- untested_edges: []
- implicit_assumptions: []
```

#### Context Boundary

AIが参照・編集できるファイルの許可範囲。AIが余計なファイルを読みに行く権限を構造的に剥奪する。

**各エントリに Why コメントを付与する。** AIが「なぜこのファイルを読むのか」を明示的に理解できるようにし、理解のブレを抑える。

**Write欄にないファイルは全て暗黙的にDeny（変更禁止）** とする。

#### Context Boundary の切り方 — 3層フィルタ

タスク定義作成時に、以下の3つの問いを順に適用する。

```text
Q1: このタスクは何を「変更」するか？
    → Write 欄が決まる

Q2: 変更対象の「契約」は何か？
    → interfaces/ から該当ファイルを特定
    → Read 欄の第一群

Q3: 変更対象が「依存する外部知識」は何か？
    → knowledge/ から該当ファイルを特定
    → Read 欄の第二群
```

#### Done条件の `type`

| type | 判定方法 | 適用場面 |
|---|---|---|
| `auto` | `tests/` のテストで判定 | データ変換、ファイルI/O |
| `manual` | 人間がDCC上で目視確認 | UI、シーン操作、描画 |
| `hybrid` | 自動テスト＋人間の最終確認 | 自動化可能な部分だけテスト |

#### Iteration Control

エラー発生時にこのセクションだけを更新し、New Chatで再投入することで、過去の失敗コンテキストを引きずらせない。

#### Retrospective

タスク完了時に3つの数値を記録する。LoDDが機能しているかを判断する軽量な運用メトリクス。

- **iterations が慢性的に多い** → タスクの粒度が粗すぎる、または interfaces/ の品質が低い
- **boundary_breaches が多い** → 依存関係の理解が不足している、またはモジュール分割が不適切
- **knowledge_added が多い** → DCC APIの地雷原を歩いている（想定内か想定外かで判断が変わる）

#### Debt Markers

タスク完了時に負債を可視化する。返済スプリントで何をすべきかがここから導出される。

- **unreviewed_functions:** 意図を1行で説明できない関数名のリスト
- **untested_edges:** 検証していないエッジケースのリスト
- **implicit_assumptions:** interfaces/ に書かれていない暗黙の前提

---

### `iterations/`

AIワーカーの試行錯誤ログを隔離するサンドボックス。

**各ファイル（`iter-xx.md`）の記載内容:**
- 試行内容の要約
- 発生したエラーとその原因分析
- 次の試行に向けた差分指示

**運用ルール:**
- タスク完了後、DCC固有の地雷情報があれば `knowledge/` に転記
- 設計判断があれば `decision_log.md`（Phase構造時）または `architecture.md` に反映
- 転記が済んだら `iterations/` 配下のファイルは**削除**

---

### `src/`

プロダクションコード本体。`tasks/` の Context Boundary の Write 欄に列挙されたファイルのみが編集対象。

### `tests/`

Done条件の実体。`tasks/` の Functional Contract に記載されたDone条件（`type: auto` または `hybrid`）と対応するテストファイルを配置する。

---

### Phase構造で追加される要素

#### `plans/<workstream>/overview.md`

Workstream単位のSSOT。目的、スコープ、Phaseの一覧と進捗を記載する。

**設計意図:** AIに特定のPhaseを実装させる際、「このPhaseは全体のどこに位置するか」を理解させるためのコンテキストアンカー。

#### `<phase>/specification.md`

Phase単位の詳細仕様。

**記載内容:**
- Phaseの目的とスコープ
- 入力状態（このPhaseの開始時に何が存在しているか）
- 出力状態（このPhase完了時に何が達成されているか）
- このPhase内のタスク一覧と依存関係

**探索フェーズ:** DCC APIの挙動調査等の探索的作業は独立したPhaseとして定義し、specification.md に探索の目的と成果物（knowledge/ への記録）を明記する。

#### `<phase>/decision_log.md`

設計判断の永久記録（WHY）。

**記載例:**
```markdown
## 2026-03-02: MeshDataの頂点格納形式

- 決定: flat array (x,y,z,x,y,z,...) を採用
- 却下: list of tuples [(x,y,z), ...] — Maya API との変換コストが高いため
- 影響: interfaces/mesh_export.md, src/core/mesh.py
```

Phase間で「なぜ前のPhaseでこの判断をしたか」を参照するために独立ファイルとして維持する。

#### `<phase>/work_log.md`

作業履歴の永久記録（WHAT）。作業日時、実施内容、結果、次のアクションを時系列で蓄積する。

`iterations/` がタスク単位の試行錯誤ログ（完了後パージ）であるのに対し、`work_log.md` はPhase単位の作業履歴として永続化する。パージ後も「何が起きたか」の全体像を追跡可能にする。

返済スプリントの実施記録もここに残す。

---

## Lock-down Rules（封鎖ルール）

AIが境界を越えようとした時に止めるための検知ルール。

### 禁止行動一覧

| ID | 違反行動 | 検知トリガー | 対処 |
|----|---------|------------|------|
| LD-001 | Context Boundary 外のファイルを読む | Read欄にないファイルのimport/参照 | 作業停止。TBD(LD-001)を記入 |
| LD-002 | Context Boundary 外のファイルを編集する | Write欄にないファイルへの変更 | 作業停止。TBD(LD-002)を記入 |
| LD-003 | interfaces/ のシグネチャを変更する | 型・引数・戻り値の変更 | 即時停止。TBD(LD-003)を記入 |
| LD-004 | 未指示の依存パッケージを追加する | import文に新規外部パッケージ | 作業停止。TBD(LD-004)を記入 |
| LD-005 | 未指示のリファクタリングを行う | 指示外のコード構造変更 | 即時停止。TBD(LD-005)を記入 |

---

## Context Boundary チェック（タスク定義作成時）

タスク定義の品質を担保するためのチェックリスト。

1. Write欄の各ファイルについて:
   - そのファイルがimport/参照するモジュールの interfaces/ はRead欄に含まれているか？
2. Read欄の interfaces/ について:
   - そこに記載された型・構造体が依存する別の interfaces/ はRead欄に含まれているか？（推移的依存の確認）
3. knowledge/ のファイル一覧を確認:
   - 対象DCCに関連する `dcc_` ファイルは全て確認したか？
   - 使用する社内ライブラリの `lib_` ファイルは含まれているか？
4. Readファイル数が5を超える場合:
   - タスクの分割を検討する（局所性が損なわれている可能性）
5. 全てのRead/WriteエントリにWhyコメントが付与されているか？

---

## AI負債管理

### AI負債の5類型

| 負債の種類 | 内容 | 潜伏期間 |
|-----------|------|---------|
| **理解負債** | AI生成コードを人間が十分に理解していない | 変更が必要になるまで |
| **契約負債** | interfaces/に書かれていない暗黙の契約がコードに埋まっている | 別モジュールとの結合時 |
| **環境負債** | 特定DCCバージョンでしか動かない前提がハードコードされている | DCC更新時 |
| **テスト負債** | Done条件は満たすがエッジケースが未検証 | 本番データ投入時 |
| **知識負債** | knowledge/に転記すべき知見がiterations/に埋もれたまま消えている | 次プロジェクトで同じ地雷を踏む |

### Tool Lifecycle

architecture.md に記載する。ツールの想定寿命が返済ポリシーの判断基準となる。

```markdown
## Tool Lifecycle
- expected_lifespan: single-shot | short-term (<3months) | long-term
- debt_policy:
  - single-shot: 返済不要。動作すれば完了
  - short-term: 理解負債・契約負債のみ返済
  - long-term: 全負債を返済。knowledge/ への転記を必須化
```

### タスク完了時の軽量返済（5分以内）

- Retrospective の3値を記入する
- Debt Markers を記入する
- iterations/ に knowledge/ に転記すべき内容がないか確認する
- AI生成コードの各関数の意図を1行で説明できるか確認する
  → 説明できない関数があれば、Debt Markers の unreviewed_functions に追記する

### 返済スプリント

サイクルに1日を構造として組み込む。「余裕があればやる」ではなく、サイクルの構成要素として確保する。

```text
Day 1      : 設計・タスク分解
Day 2〜N-2 : 実装（タスク実行）
Day N-1    : 返済スプリント
Day N      : 納品・振り返り
```

### 返済アクション一覧

| ID | 返済アクション | 対象負債 | 所要時間目安 |
|----|--------------|---------|-------------|
| R-001 | AI生成コードの通読・コメント追記 | 理解負債 | 30分〜1時間 |
| R-002 | interfaces/ と実装の突き合わせ | 契約負債 | 15分/モジュール |
| R-003 | エッジケーステストの追加 | テスト負債 | 30分〜1時間 |
| R-004 | knowledge/ への転記（未実施分） | 知識負債 | 15分/件 |
| R-005 | DCCバージョン依存箇所の洗い出し | 環境負債 | 30分 |
| R-006 | Retrospective の集計・傾向分析 | メタ判断 | 15分 |

### 返済の優先順位

全てを1日で返済する必要はない。優先順位は「次のサイクルで踏む確率」で決める。

```text
高: 次のサイクルで確実に触るモジュールの理解負債・契約負債
中: 本番データ投入前に検証すべきテスト負債
低: 当面触らないモジュールの環境負債
```

### 返済記録

Phase構造では `work_log.md` に返済の実施記録を残す。

```markdown
## 2026-03-08: 返済スプリント

### 実施した返済
- R-001: mesh.py の三角形分割ロジックを通読。コメント追記済み
- R-002: interfaces/mesh_data.md と mesh.py を突き合わせ。
         法線の正規化が保証されていない箇所を発見 → テスト追加
- R-004: dcc_maya_skincluster.md を knowledge/ に転記

### 残存負債（次サイクルに繰り越し）
- R-003: material処理のエッジケーステスト（優先度:中）
- R-005: Houdini 20.5 での動作未確認（優先度:低）
```

---

## ワークフロー

### タスク実行

```text
1. task-xxx.md を読む
2. Context Boundary の Read 欄に記載されたファイルを収集
3. 連結してAIに渡す（静的注入）
4. AIが src/ と tests/ を生成・編集
5. Done条件で検証

   ├─ Pass → 軽量返済（5分）
   │         Retrospective 記入
   │         Debt Markers 記入
   │         iterations/ パージ
   │         knowledge/ への転記を検討
   │         タスク完了
   │
   └─ Fail → iterations/ にログ記録
             task-xxx.md の Iteration Control を更新
             New Chat で再投入
```

### エラーリカバリ（Delta Recovery）

```text
1. iterations/task-xxx/iter-XX.md にエラー内容を記録
2. task-xxx.md の Iteration Control を更新:
   - current_iteration を +1
   - history に前回の失敗要約を追加
   - current_delta_instruction にピンポイントの差分修正指示を記述
     （「他のロジックは一切変更しないこと」を明記）
3. New Chat で再投入（コンテキストリセット）
```

### iterations/ のパージ

```text
タスク完了後:
1. iterations/task-xxx/ 内のログを確認
2. DCC固有の地雷情報があれば → knowledge/ に転記（人間レビュー済みのもののみ）
3. 設計判断があれば → decision_log.md または architecture.md に反映
4. iterations/task-xxx/ を削除
```

### サイクル全体

```text
サイクル開始
  │
  ├─ architecture.md に Tool Lifecycle を明記
  │  → 返済ポリシーが決まる
  │
  ├─ 設計・タスク分解（Day 1）
  │    ├─ 3層フィルタで Context Boundary を決定
  │    └─ Context Boundary チェックを実施
  │
  ├─ 実装フェーズ（Day 2〜N-2）
  │    各タスク完了時:
  │    ├─ 軽量返済（5分）
  │    ├─ Retrospective 記入
  │    ├─ Debt Markers 記入
  │    └─ iterations/ パージ + knowledge/ 転記検討
  │
  ├─ 返済スプリント（Day N-1）
  │    ├─ Debt Markers から返済対象を抽出
  │    ├─ Tool Lifecycle に応じて優先順位付け
  │    ├─ 返済アクション実施
  │    └─ 実施記録を work_log.md に残す
  │
  └─ 納品・振り返り（Day N）
       └─ Retrospective 集計 → 次サイクルの改善
```

---

## ひな型（Scaffold）

初動コストを軽減するために、プロジェクト開始時にひな型からディレクトリ構造と定型文を生成する。

### ひな型の種類

| ひな型 | 想定規模 | 構造 | 生成されるもの |
|--------|---------|------|---------------|
| `single` | 単一ファイル 200〜300行、1〜3タスク | Flat | AGENTS.md, architecture.md, interfaces/1ファイル, tasks/1ファイル |
| `module` | 複数モジュール協調、4タスク以上 | Phase | 上記 + plans/の骨格, interfaces/をモジュール数分 |

### ひな型生成時に人間が答える問い（5つ）

```text
1. ツール名 / 処理名は何か
2. 対象DCCは何か（Maya / Houdini / Blender ...）
3. 入力は何か（ファイル / シーンデータ / 選択オブジェクト ...）
4. 出力は何か（ファイル / シーン変更 / UI表示 ...）
5. モジュール名の列挙（module ひな型の場合）
```

この5つの回答から、以下を**下書き状態で**自動生成する。

- `architecture.md` の目的・入出力セクション
- `interfaces/` の各ファイルの骨格（シグネチャは空欄、構造だけ）
- `tasks/` の Context Boundary の Read欄（interfaces/ への参照）
- `AGENTS.md` のDCC制約セクション

人間は生成された下書きを確認・修正するだけで初動が完了する。

---

## エージェント並列化への拡張パス（参考）

現行LoDDは「静的注入 + 人間ゲートキーパー」の直列実行を前提としている。想定スケールでは直列で十分であり、以下は必要になった時点で着手する。

### セマンティックドリフト問題

並列化すると、各Agentが自分のContext Boundary内では「正しく」動きながら、interfaces/の同じ定義を微妙に違う解釈で実装するリスクが生じる。

| ドリフト類型 | 内容 |
|-------------|------|
| 型のドリフト | 同じ型名を異なる内部表現で扱う |
| 意味のドリフト | 同じ用語を異なる意味で解釈する |
| 前提のドリフト | 暗黙の前提条件が食い違う |

### 拡張時に必要な追加要素

| 要素 | 目的 |
|------|------|
| **Contract Test** (`tests/contracts/`) | interfaces/の記述を実行可能なテストに変換し、各Agentの成果物を統合前に検証 |
| **Shared Constants** (`constants/`) | 座標系・単位系等の共有定数を物理的に分離し、全AgentのReadに含める |
| **Merge Gate** | 並列タスクの統合ポイントを明示的なタスクとして定義。人間がゲートキーパーを担当 |

### 並列化が正当化される条件

- モジュール間の依存がinterfaces/で完全に分断できる
- 各タスクの実行時間が長く、直列では待ち時間が支配的になる
- Contract Testを整備するコストを吸収できる程度にプロジェクトが反復される

---

## ツール構成

| 用途 | 推奨ツール | 理由 |
|---|---|---|
| **設計・タスク分解** | Claude Code | 長文ドキュメント・仕様の理解に強い |
| **実装生成・コード編集** | Cursor + GitHub Copilot | Python実装の安定性とコード編集UIの使いやすさ |

2ツール構成で開始し、明確なボトルネックが発生した場合にのみツールを追加する。

---

## 構造間の対応関係

```text
                    Flat構造          Phase構造
                    ─────────         ──────────
全体ルール           AGENTS.md         AGENTS.md
全体設計             architecture.md   architecture.md
契約定義             interfaces/       interfaces/
外部依存知識          knowledge/        knowledge/
作業管理             tasks/            plans/<ws>/<phase>/tasks/
Workstream SSOT     (なし)            plans/<ws>/overview.md
Phase仕様           (なし)            plans/<ws>/<phase>/specification.md
設計判断記録         (なし)*           plans/<ws>/<phase>/decision_log.md
作業履歴記録         (なし)*           plans/<ws>/<phase>/work_log.md
試行錯誤ログ         iterations/       plans/<ws>/<phase>/iterations/
実装コード           src/              src/
テスト              tests/            tests/

* Flat構造では必要に応じて architecture.md に直接追記する。
```

---

## ライセンス・著作権
© 2026 Shouichi Kanbara (pikovolt)
このリファレンスは **クリエイティブ・コモンズ 表示 4.0 国際 ライセンス (CC BY 4.0)** の下で提供されています。