# 図解カード整理基盤 改修計画

> Status: Proposed  
> 対象リポジトリ: `takikomiudon/transcribe`  
> 対象ブランチ: `main`  
> 計画作成時に確認した先頭コミット: `a44f9b450b6ab419a61d8b4b8565383300b9c7f6`  
> 実装開始時は必ず `git rev-parse HEAD` と既存コードを再確認し、この文書より実装を優先して差分を調整すること。

## 1. 目的

現在の図解カード機能を、次の状態へ段階的に移行する。

```text
音声
  ↓
時刻付き文字起こし
  ↓
根拠を参照できる発話セグメント
  ↓
階層的な話題構造
  ↓
原子的な知識単位
  ↓
重複統合・根拠検証
  ↓
決定論的なカード表示
```

最終的な狙いは、カードを「LLMが直接生成する一次データ」ではなく、**根拠付きの構造化知識を表示するビュー**にすることである。

この改修では、まず停止後に生成する「品質版」を改善する。録音中の「速報版」は既存動作を維持し、品質版の設計が安定した後に同じデータモデルへ寄せる。

## 2. 現状認識

### 2.1 現在の実装

現行コードでは、主に次の構造になっている。

- `cards.py`
  - `TextBuffer` が300文字、20秒の無音、90秒の最大待機時間で入力をフラッシュする。
  - 速報版の判定は `new_card`、`update_last`、`skip` の3種類。
  - `update_last` は常に末尾カードだけを置換する。
  - LLMがタイトルとHTMLを直接生成し、HTMLサニタイザーを通して保存する。
  - 2回失敗すると、通常カードと同じ配列へ「図解生成エラー」を追加する。
  - 品質版は最終文字起こし全文を1回のLLM呼び出しへ渡し、話題分割、要約、重複除去、HTML生成、`source_text`の転記を同時に行わせる。
- `transcribe.py`
  - batch文字起こしで `timestamps_granularity=none` を指定し、文字列だけを保持する。
- `viewer.py`、`webapp/static/app.js`
  - カードを速報版／品質版のフラットな一覧として表示する。
- `test_cards.py`
  - JSON形式、HTMLサニタイズ、バッファ、保存順序などは検証する。
  - 内容の網羅性、重複、根拠整合性、話題構造は検証しない。

### 2.2 実出力で確認された失敗モード

提供された実出力では、速報カード92枚のうち12枚が利用者向けの「図解生成エラー」カードだった。また、次の問題が見られた。

1. 同じ中心論点が近接した複数カードへ分裂する。
2. 同一タイトルが複数回現れる。
3. 前のチャンクで説明された内容を使ったカードに、現在チャンクの短い断片だけが `source_text` として保存される。
4. ASRで崩れた固有名詞や用語が、見た目の整った確定情報として図解される。
5. 主張、補足、事例、注意点が同じ階層に並び、全体構造が見えにくい。
6. 本来は並列関係である内容が `flow` へ押し込まれ、順序や因果があるように見える。

代表的な回帰ケースとして、カード本文には複数の具体的主張があるにもかかわらず、根拠が「にたどり着いていきます。」だけになる状態を再発させてはならない。

## 3. 設計原則

### 3.1 生の文字起こしを不変の根拠として残す

- ASRの生結果は上書きしない。
- 表示・整理用の正規化結果は別フィールドへ保存する。
- LLMに原文をコピーさせて根拠としない。
- カードは `evidence_segment_ids` を返し、サーバー側が該当セグメントから表示用の `source_text` を再構成する。

### 3.2 処理用の区切りと意味上の区切りを分離する

- トークン上限のための固定長ウィンドウは、あくまで処理エンベロープとして使う。
- カードや章の境界として固定文字数を採用しない。
- 発話時刻、句読点、無音、見出し表現、隣接内容の意味変化を使って話題境界を決める。
- ウィンドウにはオーバーラップを設け、境界付近の情報を失わない。

### 3.3 内容抽出と表示生成を分離する

LLMにはHTMLを生成させず、型付きJSONだけを生成させる。HTMLはアプリ側のレンダラーで決定論的に作る。

```text
LLM: 何が書かれているかを構造化
アプリ: どう見せるかを決定
```

### 3.4 内部の原子性とUIの粒度を分離する

- 内部では、1つの独立した主張・定義・手順・比較・事例を `KnowledgeUnit` として保持する。
- UIでは、関連する複数の `KnowledgeUnit` を1つの話題カードへ統合する。
- 「1知識単位 = 1表示カード」にはしない。

### 3.5 不確実な情報を確定表示しない

- 数値、固有名詞、引用、専門用語は根拠セグメント中の表記と照合する。
- ASR誤りの可能性が高い場合は `needs_review` とし、断定的に補正しない。
- 裏付けられない主張はカードから除外するか、未確認として表示する。

### 3.6 互換性を優先する

- 既存CLIフラグを変更しない。
- CLIのstdlib＋`websockets`という方針を維持する。
- 既存のセッションJSONとHTMLエクスポートを読み込めるようにする。
- 新形式は加算的に導入し、旧形式を一度に削除しない。
- 初期実装ではベクトルDB、外部検索基盤、重量級ML依存を追加しない。

## 4. 非目標

今回の初期改修では、次は行わない。

- 話者分離の新規導入
- ベクトルDBの導入
- リアルタイム中に完全な章構造を確定すること
- 生の文字起こしをLLMで全面的に書き換えること
- 既存UIを最初のPRで全面的に作り直すこと
- 特定モデルだけに依存したプロンプト最適化
- 論文の手法をそのまま再実装すること

参考論文は設計判断の根拠であり、このアプリに対する性能保証ではない。

## 5. 目標データモデル

新しいモデルは別モジュールへ分離する。`dataclass`を基本とし、外部バリデーション依存をCLI側へ持ち込まない。

### 5.1 TranscriptWord

```python
@dataclass(frozen=True)
class TranscriptWord:
    id: str
    text: str
    start_ms: int | None
    end_ms: int | None
    speaker_id: str | None = None
    logprob: float | None = None
    kind: str = "word"
```

### 5.2 TranscriptSegment

```python
@dataclass(frozen=True)
class TranscriptSegment:
    id: str
    raw_text: str
    normalized_text: str
    start_ms: int | None
    end_ms: int | None
    word_ids: list[str]
    speaker_id: str | None = None
    uncertain_spans: list[str] = field(default_factory=list)
```

要件:

- `raw_text`はAPI応答から再現した不変の文字列。
- `normalized_text`は繰り返し抑制、空白・句読点整理などに使う。
- 修正によって意味を補わない。
- 時刻が取得できない場合もIDを付与し、`start_ms`と`end_ms`は`None`を許容する。

### 5.3 Topic

```python
@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    summary: str
    segment_ids: list[str]
    parent_id: str | None = None
    order: int = 0
```

要件:

- 章と節の2階層を初期上限とする。
- セグメントの出現順を保持する。
- 同じ話題へ後から戻った場合、複数の非連続範囲を同じトピックへ関連付けてもよい。

### 5.4 KnowledgeUnit

```python
KnowledgeKind = Literal[
    "claim",
    "definition",
    "process",
    "comparison",
    "taxonomy",
    "timeline",
    "example",
    "caution",
    "question",
]

@dataclass(frozen=True)
class KnowledgeItem:
    label: str
    text: str

@dataclass(frozen=True)
class KnowledgeRelation:
    kind: Literal[
        "supports",
        "contrasts_with",
        "example_of",
        "depends_on",
        "elaborates",
    ]
    target_id: str

@dataclass(frozen=True)
class KnowledgeUnit:
    id: str
    topic_id: str
    kind: KnowledgeKind
    title: str
    summary: str
    items: list[KnowledgeItem]
    evidence_segment_ids: list[str]
    relations: list[KnowledgeRelation] = field(default_factory=list)
    status: Literal["verified", "needs_review"] = "verified"
```

要件:

- `title`はカード一覧だけでも意味が分かる主張型タイトルにする。
- `evidence_segment_ids`は必須。
- LLMは`source_text`を返さない。
- 1つの`KnowledgeUnit`へ独立した主張を詰め込みすぎない。
- 事例は原則として中心主張の`example_of`として関連付ける。

### 5.5 RenderedCard

既存の`Card`との互換を保つため、既存フィールドを維持しながら任意フィールドを追加する。

```python
@dataclass
class Card:
    id: str
    title: str
    html: str
    source_text: str
    created_at: float
    status: str
    topic_id: str | None = None
    unit_ids: list[str] = field(default_factory=list)
    evidence_segment_ids: list[str] = field(default_factory=list)
    component: str | None = None
```

要件:

- 旧JSONはデフォルト値で読み込める。
- 新しい`source_text`は保存時にサーバー側で再構成した派生値とする。
- 根拠の正本は`evidence_segment_ids`。
- HTMLは決定論的レンダラーだけが生成する。

## 6. 保存形式

既存ファイルを壊さず、次を追加する。

```text
transcripts/<session>-final.md
transcripts/<session>-segments.json
cards_output/<session>.json
cards_output/<session>-final.json
cards_output/<session>-outline.json
cards_output/<session>-knowledge.json
cards_output/<session>.html
```

役割:

- `*-segments.json`: word timestampから構成した根拠セグメント
- `*-outline.json`: 章・節構造
- `*-knowledge.json`: 原子的な知識単位と関係
- `*-final.json`: 既存UIで表示できるレンダリング済みカード
- `*.html`: 既存のオフラインエクスポート

各新規JSONにはトップレベルに`schema_version`を持たせる。

```json
{
  "schema_version": 2,
  "session_id": "20260805-231150",
  "items": []
}
```

既存のカード配列JSONは当面そのまま維持してよい。ラッパー形式への変更は、全読込箇所に移行ロジックが入った後の別PRとする。

## 7. 品質版コンパイラ

新しい品質版生成を`card_compiler.py`へ実装する。

### 7.1 ステージA: 時刻付き文字起こしの保存

現在の`batch_transcribe()`は文字列のみを返す。次を導入する。

```python
@dataclass(frozen=True)
class BatchTranscript:
    text: str
    words: list[TranscriptWord]
```

作業:

1. ElevenLabs batch APIの`timestamps_granularity`を`word`へ変更する。
2. `words`を解析する。
3. 既存利用箇所向けに、必要なら文字列だけを返す互換ラッパーを残す。
4. API応答に`words`がない場合のフォールバックを実装する。
5. `write_batch_transcript`によるMarkdown保存は維持する。
6. セグメントJSONは原子的な一時ファイル置換で保存する。

### 7.2 ステージB: 発話セグメント化

最初のセグメントは決定論的に作る。

境界候補:

- `。！？`などの文末
- 一定以上の無音
- 話者変更
- 見出しらしい短文
- 極端に長い文の安全分割

ルール:

- 数文字だけの断片は、前後のセグメントへ意味を補わず連結する。
- 同一文の異常な連続反復は`normalized_text`側で圧縮候補にする。
- `raw_text`は保持する。
- セグメントIDはセッション内で安定させる。
- 単純な文字数境界をトピック境界として扱わない。

### 7.3 ステージC: 階層アウトライン生成

長文を安全に扱うため、処理ウィンドウと意味境界を分ける。

1. セグメントをトークン予算内のオーバーラップ付きウィンドウにする。
2. 各ウィンドウについて、LLMへセグメントID付きで境界候補と話題名を返させる。
3. 重複ウィンドウの境界候補を統合する。
4. コンパクトな話題一覧だけを使い、章・節の2階層へ整理する。
5. 全セグメントが少なくとも1つの話題へ所属することを検証する。

LLM出力例:

```json
{
  "boundaries": [
    {
      "start_segment_id": "seg-0012",
      "end_segment_id": "seg-0038",
      "title": "行動から始める理由",
      "summary": "行動が情報と思考の質を高める循環を説明する"
    }
  ]
}
```

固定長ウィンドウはAPI上限回避のためだけに使い、最終話題境界はセグメントIDで確定する。

### 7.4 ステージD: トピック単位の知識抽出

各トピックについて、LLMへ次だけを渡す。

- トピック名・概要
- 対象セグメントのIDと`normalized_text`
- 許可する`KnowledgeKind`
- 既存のJSON Schema

LLMには次を禁止する。

- HTMLの生成
- 入力にない固有名詞の推測
- 根拠IDのない主張
- 複数の独立主張を無理に1単位へ結合すること
- `source_text`のコピー

### 7.5 ステージE: 全体横断の統合

トピック抽出後、コンパクトな`KnowledgeUnit`一覧を使って次を行う。

1. 正規化タイトルが同一の単位を統合候補にする。
2. 同じ中心主張の言い換えを統合する。
3. 主張と事例を`example_of`で結ぶ。
4. 補足を`elaborates`で結ぶ。
5. 似ているが反対の内容は`contrasts_with`で保持し、誤って統合しない。
6. 非連続の証拠セグメントを1単位へ集約できるようにする。
7. すべての統合履歴をデバッグログへ残す。

初期実装ではベクトルDBを使わない。次の組み合わせで十分とする。

- 正規化文字列一致
- 文字n-gramまたはトークン集合の重複率
- LLMによる、候補を絞った最終統合判定

### 7.6 ステージF: 根拠検証

レンダリング前に必ず検証する。

必須検証:

- すべての`evidence_segment_ids`が存在する。
- 根拠を再構成すると空にならない。
- 説明量に対して根拠が極端に短くない。
- カードに含む数値が根拠中に存在する。
- 引用符付き表現が根拠中に存在する。
- LLMが返したセグメントIDが対象トピック外でも、実在するなら非連続証拠として明示的に許可する。
- 裏付け不能な単位は破棄するか`needs_review`にする。
- `needs_review`の固有名詞や専門用語を、確定した定義として表示しない。

回帰条件:

- 「にたどり着いていきます。」のような短い断片だけを根拠に、複数の具体的主張を持つカードを生成してはならない。
- 根拠が短すぎる場合は、隣接セグメントを自動で足すのではなく、抽出結果を再生成または破棄する。根拠の水増しは禁止する。

### 7.7 ステージG: 決定論的レンダリング

`card_renderer.py`へ実装する。

基本マッピング:

| `KnowledgeKind` | 主コンポーネント |
|---|---|
| `process` | `flow` |
| `comparison` | `compare`、共通属性が複数なら`table` |
| `taxonomy` | `tree` |
| `timeline` | `timeline` |
| `definition` | `keyvalue` |
| `claim` | `callout`または通常文章 |
| `caution` | `callout` |
| `example` | 親カード内の補足 |
| `question` | `callout`または未解決事項一覧 |

ルール:

- 1カードの主コンポーネントは1つ。
- `flow`は順序を入れ替えると意味が変わる場合だけ使う。
- 1トピックにつき主要カードを1〜5枚程度にまとめる。
- 事例や補足は可能な限り親カードへ収納する。
- HTMLは既存の許可タグ・クラスだけで生成する。
- 既存サニタイザーは防御層として残す。
- 同じカード内でタイトルを二重表示しない。
- レンダラー単体はAPI呼び出しなしでテストできるようにする。

## 8. 速報版の扱い

初期PRでは速報版のアルゴリズムを全面変更しない。ただし、明らかな問題だけを先に直してよい。

### 8.1 初期PRに含める小変更

- 2回生成に失敗しても、利用者向けカード配列へエラーカードを追加しない。
- エラーはログとセッション処理状態へ保存する。
- 極端に短い新規チャンクから`new_card`を作らない。
- 既存の速報カード保存形式と表示は維持する。

### 8.2 後続フェーズ

品質版が安定した後、速報版の判定を次へ拡張する。

```text
create
update(card_id)
merge(card_ids)
link(card_id)
skip
```

候補カード選定:

- 全カードをLLMへ渡さない。
- `id`、`title`、`summary`、主要語だけの軽量インデックスを作る。
- 新規発話との文字列重複や主要語一致で上位候補を選ぶ。
- 候補だけをLLMへ渡す。
- 末尾以外のカード更新を許可する。
- 速報カードには`provisional`を付け、品質版との`keep/update/merge/split/drop`対応を保存する。

## 9. UI計画

UI変更は品質版の構造化データが安定した後に行う。

目標タブ:

```text
概要 | 目次 | カード | 全文
```

### 9.1 概要

- セッション全体の重要ポイント
- 未確認の用語数
- 処理警告
- 主要トピック数

### 9.2 目次

- 章・節の階層表示
- クリックで該当カードへ移動
- 時刻があれば音声位置へ移動

### 9.3 カード

- トピック単位でグルーピング
- 補足・具体例を折りたたみ
- `needs_review`を明示
- 速報版／品質版は主タブではなくバッジまたは表示切替として残してよい

### 9.4 全文

- 根拠セグメントを選ぶと強調表示
- 時刻があれば`audio.currentTime`を更新
- 生テキストと正規化テキストの区別を表示可能にする

## 10. ファイル別の変更方針

### 新規ファイル

#### `transcript_segments.py`

- `TranscriptWord`
- `TranscriptSegment`
- batch API応答の解析
- 句読点・無音を使うセグメント化
- JSON保存・読込
- 根拠再構成

#### `card_models.py`

- `Topic`
- `KnowledgeUnit`
- `KnowledgeItem`
- `KnowledgeRelation`
- schema version
- JSON変換と検証

#### `card_compiler.py`

- アウトライン生成
- トピック別抽出
- 全体統合
- 根拠検証
- 品質版カードの組み立て

#### `card_renderer.py`

- 型付き知識からHTMLを生成
- 既存CSSコンポーネントへのマッピング
- `source_text`のサーバー側再構成

#### `test_card_compiler.py`

- 多段階処理の単体・統合テスト
- 不正な根拠ID
- 短すぎる根拠
- 非連続証拠
- 重複統合
- 不確実語

#### `test_card_renderer.py`

- 種別ごとのHTML
- `flow`誤用防止
- エスケープ
- 安定した出力

### 既存ファイル

#### `transcribe.py`

- `BatchTranscript`を扱う新関数を追加
- word timestampを取得
- 既存の文字列利用箇所には互換ラッパーを提供
- CLIの既存挙動を維持

#### `cards.py`

- 既存速報版を当面保持
- `Card`へ任意メタデータを追加
- エラーを通常カードへ保存しない
- 品質版の一発生成を`card_compiler`呼び出しへ置換
- 旧`generate_final_cards`は互換ラッパーとして残すか、移行後に削除

#### `webapp/runner.py`

- 停止後処理を次へ変更する。

```text
batch transcription
→ segments保存
→ compile_final_cards
→ outline/knowledge/final cards保存
→ WebSocket通知
```

- 一部ステージ失敗時のフォールバックを定義する。
- 品質版失敗時も速報版・文字起こし・音声を保持する。

#### `webapp/app.py`

- セッション詳細へ`outline`と処理警告を追加
- 旧セッションでは空配列を返す
- エクスポートで新旧カードを読めるようにする

#### `webapp/static/app.js`

- 初期PRでは既存フラット表示を維持可能
- 後続で話題グループ、目次、根拠ジャンプを追加
- `status === "error"`のカード表示を前提にしない

#### `viewer.py`

- 新カードの任意メタデータに対応
- 既存HTMLエクスポート互換を維持
- 後続で目次を埋め込む

#### `test_cards.py`

- 既存テストを維持
- エラー時に通常カードが追加されないことへ期待値を変更
- 旧JSONの読込互換テストを追加

#### `test_webapp.py`

- 旧セッションの読込
- 新しいoutline/knowledgeファイルがない場合
- 品質版生成成功／部分失敗
- エクスポート互換

## 11. 実装フェーズ

## Phase 0: ベースラインと評価器

目的: 改善したか判断できる状態を先に作る。

作業:

- [ ] 現行テストを実行し、結果を記録する。
- [ ] `tests/fixtures/card_quality/`へ短い合成文字起こしを追加する。
- [ ] 次の失敗を再現する合成ケースを作る。
  - 同じ中心主張が別チャンクへ分割される。
  - 短い末尾断片だけが根拠になる。
  - 同一タイトルが重複する。
  - ASRで崩れた固有名詞が含まれる。
  - 並列内容が`flow`へ変換される。
- [ ] 実際の長い文字起こしや書籍由来の音声内容は、権利を確認せずリポジトリへコミットしない。
- [ ] ローカル評価用スクリプトを作り、カード数、重複タイトル、根拠ID妥当性、数値支持率を出力する。

完了条件:

- 現行方式が少なくとも2つの合成回帰ケースで失敗することを確認できる。
- APIを呼ばない単体評価がある。

## Phase 1: 根拠セグメント

目的: カードと音声・原文を確実に結び付ける。

作業:

- [ ] word timestampを取得する。
- [ ] `BatchTranscript`、`TranscriptWord`、`TranscriptSegment`を導入する。
- [ ] `*-segments.json`を保存する。
- [ ] 旧文字列APIを壊さない。
- [ ] セグメントIDから`source_text`を再構成する関数を作る。
- [ ] フォールバック時は時刻なしセグメントを作る。

完了条件:

- 全セグメントIDが一意。
- 時刻がある場合、開始時刻が非減少。
- 既存CLIとWeb録音が動く。
- `uv run pytest`が通る。

## Phase 2: 構造化品質版コンパイラ

目的: 全文一発のHTML生成を置き換える。

作業:

- [ ] アウトライン生成
- [ ] トピック別`KnowledgeUnit`抽出
- [ ] 全体統合
- [ ] 根拠検証
- [ ] outline/knowledge JSON保存
- [ ] 品質版生成のフォールバック
- [ ] LLM応答を厳格なJSON Schemaにする

完了条件:

- LLMはHTMLと`source_text`を返さない。
- 全`KnowledgeUnit`に実在する根拠IDがある。
- 正規化タイトルの完全重複がない。
- 短い根拠断片から詳細カードを作る回帰ケースが通る。
- 既存速報版に影響しない。

## Phase 3: 決定論的レンダラーと互換出力

目的: 内容と見た目を分離する。

作業:

- [ ] `card_renderer.py`を実装
- [ ] 既存コンポーネントCSSを再利用
- [ ] `source_text`をサーバー側で再構成
- [ ] 新しい任意メタデータを保存
- [ ] 旧カードJSONを読み込めるようにする
- [ ] エラーカードを廃止

完了条件:

- 同一入力から常に同一HTMLが生成される。
- rendererテストはネットワーク不要。
- 旧セッションをWeb UIとHTMLエクスポートで表示できる。
- 利用者向けカード一覧に「図解生成エラー」が混ざらない。

## Phase 4: 話題構造UI

目的: フラットなカード列を探索可能な知識構造へ変える。

作業:

- [ ] 概要・目次・カード・全文の表示
- [ ] トピック別グルーピング
- [ ] 根拠ハイライト
- [ ] 音声時刻ジャンプ
- [ ] `needs_review`表示
- [ ] オフラインHTMLにも目次を含める

完了条件:

- 目次からカードへ移動できる。
- カードから原文と音声位置へ移動できる。
- キーボード操作を維持する。
- モバイル幅で崩れない。

## Phase 5: 速報版の非隣接更新

目的: 録音中の重複を減らす。

作業:

- [ ] 軽量カードインデックス
- [ ] 上位候補抽出
- [ ] `update(card_id)`と`merge(card_ids)`
- [ ] stable ID
- [ ] 速報版と品質版の対応履歴

完了条件:

- 話題へ戻った際に末尾以外のカードを更新できる。
- 全カード本文を毎回LLMへ送らない。
- 速報版が失敗しても録音と最終処理を阻害しない。

## 12. 品質評価

### 12.1 自動検査

最低限、次を機械的に測る。

| 指標 | 定義 |
|---|---|
| Evidence validity | 全根拠IDのうち実在する割合 |
| Empty evidence | 再構成後に空または極端に短い根拠の件数 |
| Numeric support | カード中の数値が根拠にも存在する割合 |
| Exact title duplication | 正規化後に同じタイトルを持つカード数 |
| Card error leakage | 利用者向けカードに処理エラーが混ざった件数 |
| Topic coverage | 話題へ所属するセグメントの割合 |
| Orphan units | 話題または根拠を持たない知識単位数 |
| Cost | セッション1分あたりの入力・出力トークン |
| Latency | 停止から品質版完成までの処理時間 |

### 12.2 人手評価

代表セッションをブラインド比較し、各項目を1〜5で評価する。

- 重要内容の網羅性
- 重複の少なさ
- 1カードの論点のまとまり
- 章・節の自然さ
- 根拠との一致
- ASR誤りを確定情報にしていないか
- 必要情報へ到達する速さ
- 図解形式が意味関係に合っているか

### 12.3 実出力サンプルに対する目安

提供された92枚の速報カードはCIの固定正解にはしないが、ローカル比較の参考にする。

期待値:

- 利用者向けエラーカード: 12枚 → 0枚
- 完全に同じタイトル: 0件
- 根拠が短い断片だけの詳細カード: 0件
- 主要な表示カード: 内容を落とさず、おおむね20〜35枚程度へ整理
- 数値・固有名詞: 根拠で確認できないものを確定表示しない
- 「主張」「根拠」「具体例」「補足」が同じ階層に無秩序に並ばない

20〜35枚はハードコードする閾値ではない。内容量に応じて変わるため、人手評価の目安として扱う。

## 13. 受け入れ条件

全体のDefinition of Done:

- [ ] `uv run pytest`が通る。
- [ ] 既存CLIの主要コマンドが変わらない。
- [ ] 既存Webセッションを読み込める。
- [ ] 旧カードJSONを表示・エクスポートできる。
- [ ] 品質版のLLM応答にHTMLがない。
- [ ] 品質版のLLM応答に`source_text`がない。
- [ ] すべての品質版カードに根拠セグメントIDがある。
- [ ] サーバー側だけで表示用根拠を再構成できる。
- [ ] 詳細なカードが極端に短い根拠だけを持たない。
- [ ] 正規化タイトルの完全重複がない。
- [ ] 利用者向けカード配列へ処理エラーを追加しない。
- [ ] `flow`は真の手順・時系列・因果だけに使われる。
- [ ] raw transcriptを上書きしない。
- [ ] API失敗時も音声、文字起こし、速報版を失わない。
- [ ] 新しい依存を追加した場合、その必要性と代替案をPR本文へ記載する。
- [ ] `README.md`の設計説明と実装が一致する。

## 14. フォールバックとロールバック

品質版の各ステージは独立して失敗できるようにする。

```text
segments失敗
  → 既存のplain transcriptから時刻なしsegmentsを作る

outline失敗
  → 単一の「全体」トピックへフォールバック

topic extraction失敗
  → そのトピックだけ警告として記録し、他を継続

global consolidation失敗
  → トピック内の重複除去だけで出力

renderer失敗
  → 知識JSONを保持し、旧品質版または速報版を表示

全体失敗
  → 速報版、最終文字起こし、WAVを保持
```

フィーチャーフラグ例:

```text
TRANSCRIBE_STRUCTURED_FINAL_CARDS=1
```

初期導入では新方式をフラグで切り替えられるようにしてもよい。安定後に既定値を新方式へ変更し、旧方式の削除は別PRとする。

## 15. Codexへの実行ルール

1. 最初に`README.md`、`PLAN.md`、`cards.py`、`transcribe.py`、`webapp/runner.py`、`webapp/app.py`、`viewer.py`、関連テストを全文読む。
2. 実装前に現在のmainとの差分とテスト結果を確認する。
3. Phase 0から順に進め、複数フェーズを一度の巨大差分にしない。
4. 各フェーズで、変更前に対象ファイルと契約を短く列挙する。
5. 公開関数や保存形式を変える場合は、先に互換テストを書く。
6. LLM呼び出しなしで検証できるロジックを分離する。
7. 新規依存は原則追加しない。追加する場合は、標準ライブラリで困難な理由を記録する。
8. 既存プロンプトを単に長くするだけで解決しない。
9. HTML生成を新しいLLMプロンプトへ移し替えない。
10. raw transcriptを修正済み文字列で置換しない。
11. サンプル実出力の長文を無断でテストfixtureとしてコミットしない。
12. 各フェーズ終了時に次を実行する。

```sh
uv run pytest
python -m compileall .
```

13. 失敗したテストを無効化して進めない。
14. 変更後に、受け入れ条件を満たした項目と未完了項目を報告する。
15. 未完了フェーズを「実装済み」とREADMEへ書かない。

## 16. 推奨PR分割

### PR 1: Evidence foundation

- word timestamp
- segment model
- segment JSON
- server-side source reconstruction
- old transcript API compatibility

### PR 2: Structured final compiler

- outline
- knowledge units
- consolidation
- evidence validation
- structured artifacts

### PR 3: Deterministic rendering and compatibility

- renderer
- extended Card
- old session migration
- error-card removal
- export compatibility

### PR 4: Topic-oriented UI

- overview
- outline
- grouped cards
- transcript/audio navigation

### PR 5: Live-card retrieval and non-last updates

- candidate index
- stable IDs
- non-adjacent update
- live/final reconciliation

## 17. 技術的根拠

この計画は次の研究から設計上の示唆を得ている。ただし、各研究の評価対象は本アプリそのものではなく、適用は工学的推論である。

- [Summ^N: A Multi-Stage Summarization Framework for Long Input Dialogues and Documents](https://aclanthology.org/2022.acl-long.112/)  
  長い入力を分割し、粗い要約から最終要約へ段階的に集約する構成を支持する。
- [Dynamic Chunking and Selection for Reading Comprehension of Ultra-Long Context in Large Language Models](https://aclanthology.org/2025.acl-long.1538/)  
  固定長切断が意味的に関連する内容を分離し得ることと、可変長の意味境界を使う意義を示す。
- [M3Seg: A Maximum-Minimum Mutual Information Paradigm for Unsupervised Topic Segmentation in ASR Transcripts](https://aclanthology.org/2023.emnlp-main.492/)  
  ASR transcriptを主題境界で分割することを独立した問題として扱う根拠になる。
- [Aspect-based Meeting Transcript Summarization: A Two-Stage Approach with Weak Supervision on Sentence Classification](https://arxiv.org/abs/2311.04292)  
  同じ観点の文が長い文字起こし中に散在し得るため、関連文の選択と要約を分ける設計を支持する。
- [ThreadSumm: Summarization of Nested Discourse Threads Using Tree of Thoughts](https://aclanthology.org/2026.acl-long.1486/)  
  最終文面の前に、aspectとAtomic Content Unitを抽出するcontent planningの考え方を支持する。
- [Measuring Attribution in Natural Language Generation Models](https://aclanthology.org/2023.cl-4.2/)  
  生成内容を識別可能な情報源に照らして検証するという根拠管理の方針を支持する。
- [FENICE: Factuality Evaluation of summarization based on Natural language Inference and Claim Extraction](https://aclanthology.org/2024.findings-acl.841/)  
  要約を原子的なclaimへ分解し、原文と対応付けて検証する評価設計の参考になる。
- [Japanese ASR-Robust Pre-trained Language Model with Pseudo-Error Sentences Generated by Grapheme-Phoneme Conversion](https://www.isca-archive.org/interspeech_2022/ohsugi22_interspeech.html)  
  日本語ASR誤りが後段の意味理解・要約へ影響することを前提にすべき根拠になる。
- [ElevenLabs Speech to Text documentation](https://elevenlabs.io/docs/overview/capabilities/speech-to-text)  
  Scribe v2のword-level timestampを根拠セグメントと音声ナビゲーションに利用できる。
