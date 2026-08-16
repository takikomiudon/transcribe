# Minimal Realtime Transcription CLI

macOSの日本語・英語・韓国語音声をElevenLabs Scribe Realtimeでリアルタイム文字起こしし、確定結果をOpenAIで補正する最小CLIです。録音中はElevenLabs Scribe v2で用語集用の末尾音声を転写し、録音量の成長に合わせて全文も更新します。日本語を主言語、英語・韓国語を副言語として認識します。

## 必要なもの

- macOS
- `uv`
- `ffmpeg`
- `curl`（macOS標準）
- `ELEVENLABS_API_KEY` を設定した `.env.local`
- 補正・翻訳・図解に使う `OPENAI_API_KEY`
- WebアプリでDeepSeekを使う場合は `DEEPSEEK_API_KEY`

`.env.local` はGit管理外です。APIキーの値をソースコードやコミットへ入れないでください。

### ElevenLabs APIキーの取得

1. [ElevenLabs](https://elevenlabs.io/)でアカウントを作成してログインします。
2. 左メニューの「Developers」から「API Keys」を開きます。
3. 「Create API Key」を押し、`transcribe-local` など判別できる名前を付けます。
4. Restricted Keyのまま「Speech to Text」だけを有効にし、必要ならクレジット上限を設定します。
5. 作成直後に一度だけ表示されるキーをコピーします。
6. Scribeの利用規約が表示された場合は、ダッシュボードで承諾します。

詳細はElevenLabs公式の[APIキー作成手順](https://elevenlabs.io/docs/help-center/technical/how-do-i-authorize-myself-using-an-api-key)と[キーの制限・管理](https://elevenlabs.io/docs/overview/administration/workspaces/api-keys)を参照してください。

Raycastから起動する場合は、使用する音声入力番号も設定できます。未設定時は `0` を使用します。

```sh
ELEVENLABS_API_KEY='your-elevenlabs-api-key'
OPENAI_API_KEY='your-openai-api-key'
DEEPSEEK_API_KEY='your-deepseek-api-key'
TRANSCRIBE_DEVICE=0
```

## 使い方

最初に音声入力番号を確認します。

```sh
uv run transcribe.py --list-devices
```

表示された番号を指定して開始します。

```sh
uv run transcribe.py --device 0
```

Realtimeの生テキストは表示せず、直前500文字の文脈とbatch版から収穫した用語集を使ってOpenAIで補正した確定結果を、ターミナルへ表示しながら `transcripts/YYYYMMDD-HHMMSS.md` へ受信順に保存します。ブラウザにはScribe v2による精度重視の全文が表示され、最初の30秒と、録音量が前回の全文更新時の1.5倍に達した時点で更新されます。終了は `Ctrl-C` です。

処理中は開始から終了までの全音声を `recordings/YYYYMMDD-HHMMSS.wav` へ保存します。最初の30秒と、録音フレーム数が前回の全文転写時の1.5倍に達した時点で蓄積WAVをElevenLabs Scribe v2へ送り、精度重視の結果で `transcripts/YYYYMMDD-HHMMSS-final.md` を書き直します。それ以外の30秒tickでは、補正用語集の更新に必要な末尾60秒だけを転写し、final本文には使いません。停止時には全WAVで最終更新し、保存に成功するとWAVは自動で削除されます。API通信や保存に失敗した場合は、直前のfinalと再試行用WAVが残ります。

判定間隔は `--batch-refresh-seconds 60` のように変更できます。用語集用の末尾ウィンドウは60秒または判定間隔の2倍の長い方です。既定値では、用語集用転写が録音時間の約2倍、成長比率による全文転写が合計約3倍、停止時の最終更新が1倍となり、batch処理量は最大約6倍を目安に線形に増えます。全文更新tickでは末尾転写を省くため、実際の処理量はこれより少なくなります。Realtime、各batch転写、停止時の最終更新にはそれぞれElevenLabsの利用料金が発生します。

英語・韓国語の確定結果を日本語でも保存する場合は、`--translate-ja` を追加します。原文の文字起こしにはElevenLabs Scribe Realtime、日本語訳にはOpenAI `gpt-5.6-luna` を使用するため、翻訳分のAPI料金が別途かかります。

```sh
uv run transcribe.py --device 0 --translate-ja
```

確定した発話からOpenAIで話題ごとの図解カードを生成する場合は、`--cards` を追加します。録音中は補正済みのRealtime確定結果から速報カードがローカルビューアへ追加されます。停止後はScribe v2のfinal transcript全文を見て意味単位を分割し直した品質版へ自動で切り替わり、タブから速報版にも戻れます。速報版は `cards_output/YYYYMMDD-HHMMSS.json`、品質版は `cards_output/YYYYMMDD-HHMMSS-final.json` に分けて保存し、同名のHTMLには両方を収録します。品質版の生成に失敗した場合は速報版が残ります。

```sh
uv run transcribe.py --device 0 --cards
```

生成は既定で300文字、20秒間の新規発話なし、または前回生成から90秒のいずれかで始まります。必要なら `--cards-character-threshold`、`--cards-idle-seconds`、`--cards-max-seconds` で調整し、ビューアのポート競合時は `--cards-port` を変更できます。`--translate-ja` と同時指定した場合も、図解には補正済み・翻訳前のテキストを使います。

音声は無音を含め、16kHz PCMを100ms単位で常時送信します。発話の確定はElevenLabs側のVADに任せ、800msの無音で区切ります。終了時は100ms未満の残りも送信して確定します。

初回起動時にmacOSから求められたら、ターミナルのマイク利用を許可してください。拒否した場合は「システム設定 → プライバシーとセキュリティ → マイク」から変更できます。

## Webアプリ

初回は `uv sync` でWebアプリ用の依存を導入し、次のコマンドで起動します。

```sh
uv sync
uv run -m webapp
```

ブラウザで <http://127.0.0.1:8770> を開いてください。ブラウザの `getUserMedia` でマイク音声を取得し、16kHz PCMへ変換してWebSocketでサーバーへストリーミングします。利用にはブラウザのマイク許可が必要です。localhostはsecure contextとして扱われるため、ローカル利用ではHTTPSは不要です。

Webアプリではセッション履歴を残し、録音の開始・停止・同じセッションへの再開ができます。録音したWAVは最終処理後も常に保持し、削除する場合はセッションメニューから明示的に操作します。

録音開始前はヘッダーのAIモデル選択から `GPT-5.6 Luna` と `DeepSeek V4 Flash` をセッション単位で切り替えられます。新規セッションの既定値はOpenAI `gpt-5.6-luna`です。`DEEPSEEK_API_KEY` を設定した場合だけ、公式APIの `deepseek-v4-flash` が選択肢に表示されます。録音開始後はモデルを変更できず、停止後に再開しても録音開始時のモデルを使います。

CLIはWebアプリから独立しており、従来どおり `uv run transcribe.py` で利用できます。CLIはstdlibとwebsocketsだけを使う方針を維持します。WebアプリはFastAPIとuvicornを使用し、依存は `pyproject.toml` で管理します。

録音中セッションの管理は単一プロセス内で行います。`uvicorn --workers` の指定や、同じデータディレクトリを使う複数インスタンスの同時起動には対応していません。

## AudibleなどのMac音声

システム音声を入力として扱うには、BlackHoleを別途インストールし、「Audio MIDI設定」で通常の出力先とBlackHoleを含む複数出力装置を作成します。その後、`--list-devices` に表示されるBlackHoleの番号を指定します。

この最小版は入力を1つだけ扱います。マイクとの同時ミックス、話者分離、自動再接続は行いません。

## Raycastから起動

Raycastの「Settings → Extensions」で `+` を押し、「Add Script Directory」からこのリポジトリの `raycast` ディレクトリを追加します。次の2コマンドが使えるようになります。

- `transcribe`
- `transcribe-ja`

初回起動時にmacOSから求められたら、RaycastによるTerminalの操作を許可してください。各コマンドにはRaycastの設定から任意のホットキーを割り当てられます。

起動には `Live Transcribe` という専用Terminalタブを1つだけ使用します。文字起こし中にどちらかのコマンドを再実行した場合は、新しく起動せず、そのタブを前面へ表示します。終了するときは専用タブで `Ctrl-C` を押してください。終了後は、どちらかのコマンドを実行すると同じタブで再開します。

## 図解カード整理の設計方針と技術的根拠

図解カード機能は、録音中の速報性と、停止後に全体を見直せる品質版の二段構成です。品質版は、最終文字起こしを一度のLLM呼び出しで「話題分割・要約・重複除去・根拠転記・HTML生成」まで行う旧方式から、根拠付きの構造化データを段階的に作る方式へ移行しました。設計と受け入れ条件は[`PLAN.md`](PLAN.md)を参照してください。

### 旧方式の制約

速報版を生成する300文字、20秒の無音、90秒の最大待機時間は、LLMへ送るタイミングを決めるには有効ですが、意味上の話題境界ではありません。現在は軽量カードインデックスから上位候補を選び、後から戻ってきた話題でも末尾以外のカードを更新・統合できます。

旧品質版では、最終文字起こし全文からLLMがカードのタイトル、HTML、`source_text`を同時に生成していました。この方式では、次の処理が1回の応答へ密結合します。

```text
話題境界の推定
→ 重要内容の抽出
→ 重複の統合
→ 図解形式の選択
→ HTML生成
→ 根拠箇所の転記
```

見た目の整ったカードを生成できても、カード群全体の章構造、非隣接箇所に散らばる同一論点の統合、根拠位置の正確さ、ASR誤りの扱いを個別に検証しにくいことが問題です。

### 実装アーキテクチャ

停止後の品質版は、次のパイプラインで処理します。

```text
Scribe v2の時刻付き単語列
  ↓
TranscriptSegment
  - raw_text
  - normalized_text
  - start_ms / end_ms
  - stable segment ID
  ↓
階層アウトライン
  - 章
  - 節
  - 所属segment ID
  ↓
KnowledgeUnit
  - 主張
  - 定義
  - 手順
  - 比較
  - 分類
  - 事例
  - 注意点
  - evidence_segment_ids
  ↓
全体横断の重複統合・関係付け
  ↓
根拠検証
  ↓
決定論的レンダラー
  ↓
概要・目次・カード・全文
```

内部では、独立した主張や事例を原子的な`KnowledgeUnit`として保持します。ただし、UIで1単位ずつカード化すると細かくなりすぎるため、表示時には同じ話題の複数単位を統合します。原子性は保存、検証、再編成のために使い、表示粒度とは分離します。

### 論文から得られる設計上の示唆

| 研究 | 研究が示す内容 | 本リポジトリへの適用 |
|---|---|---|
| [Summ^N (ACL 2022)](https://aclanthology.org/2022.acl-long.112/) | 長い文書や対話を分割し、粗い要約を複数段階で作ってから最終要約へ集約する。AMI、ICSI、QMSumなどの長文要約で既存手法を上回った。 | 最終文字起こしから一度にカード列を作らず、アウトライン、トピック別抽出、全体統合、表示生成を分ける。 |
| [Dynamic Chunking and Selection (ACL 2025)](https://aclanthology.org/2025.acl-long.1538/) | 固定長切断は意味的に関連する内容を分離する危険があり、隣接文の意味類似度に基づく可変長チャンクを提案している。 | 300文字などの閾値を話題境界として使わない。固定長は処理上のウィンドウだけにし、最終境界は発話・無音・意味変化から決める。 |
| [M3Seg (EMNLP 2023)](https://aclanthology.org/2023.emnlp-main.492/) | ASR transcriptを、テーマ上の意味で区切られたセグメントへ分割する問題を扱い、2つの公開データセットで従来法を上回った。 | 文字起こしの区切りをカード生成プロンプトの副作用にせず、独立したtopic segmentation段階として実装する。 |
| [Aspect-based Meeting Transcript Summarization (IEEE Big Data 2023)](https://arxiv.org/abs/2311.04292) | 同じ観点に関係する文は長い会議文字起こし中に散在し得るため、関連文を選択してから観点別に要約する二段階方式を採る。 | 直前カードだけを見るのではなく、同じ話題の非連続segmentを集めて1つの知識単位やカードへ統合できるようにする。 |
| [ThreadSumm (ACL 2026)](https://aclanthology.org/2026.acl-long.1486/) | 最終要約を書く前に、discourse aspectとAtomic Content Unitを抽出するcontent planningを行い、構造、観点保持、意見網羅を改善する。 | 最終HTMLより先に、話題と原子的な知識単位を型付きJSONで作る。内部単位と表示カードを分離する。 |
| [Measuring Attribution / AIS (Computational Linguistics 2023)](https://aclanthology.org/2023.cl-4.2/) | 外部世界について述べる生成結果は、識別可能な提供済み情報源に照らして検証されるべきだとする評価枠組み。 | LLMに`source_text`をコピーさせず、`evidence_segment_ids`を返させ、サーバー側で原文と時刻を再構成する。 |
| [FENICE (Findings of ACL 2024)](https://aclanthology.org/2024.findings-acl.841/) | 要約から原子的なclaimを抽出し、NLIで原文と対応付ける、解釈しやすい事実整合性評価を提案する。 | カード単位ではなく、内部の原子的主張ごとに根拠を検証する。数値・引用・固有名詞の支持確認もこの段階で行う。 |
| [Japanese ASR-Robust PLM (Interspeech 2022)](https://www.isca-archive.org/interspeech_2022/ohsugi22_interspeech.html) | 日本語ASRの誤りを含む入力に頑健な事前学習を行い、音声対話要約で改善を報告している。ASR誤りが後段処理へ影響することを前提としている。 | ASR出力を確定事実として扱わない。raw transcriptを保持し、正規化結果を分離し、不確かな固有名詞や専門用語を`needs_review`として扱う。 |

これらの論文は、固定長分割より意味境界が常に優れることや、このアプリで特定のカード枚数が最適であることを直接証明するものではありません。また、ThreadSummはネストした議論スレッド、Dynamic Chunkingは長文読解、FENICEは要約評価を主対象にしており、本リポジトリへの適用は設計上の類推です。したがって、方式の採否は論文名だけで決めず、実際の録音データに対する網羅性、重複、根拠整合性、処理時間、APIコストを比較して判断します。

### 根拠をsegment IDで持つ理由

カード生成モデルに原文をコピーさせると、見た目上は引用に見えても、実際の文字起こし中の位置や音声時刻を保証できません。新方式では、LLMが次のような参照だけを返します。

```json
{
  "evidence_segment_ids": ["seg-0104", "seg-0105"]
}
```

表示用の原文はサーバー側で該当IDから再構成します。これにより、存在しないIDの拒否、数値や固有名詞の照合、全文中の強調、音声位置へのジャンプが可能になります。

ElevenLabs Scribe v2のbatchリクエストでは`timestamps_granularity=word`を指定し、単語の`start`と`end`から安定した発話セグメントを作ります。詳細は[ElevenLabs Speech to Text documentation](https://elevenlabs.io/docs/overview/capabilities/speech-to-text)を参照してください。

### HTMLをLLMに生成させない理由

現在のHTMLサニタイザーは安全策として有効なので残しますが、新方式では通常のHTMLをアプリ側のレンダラーが生成します。`KnowledgeUnit.kind`と構造化された項目から、次のように表示を決めます。

| 意味関係 | 表示 |
|---|---|
| 手順・真の因果系列 | `flow` |
| 選択肢・対比 | `compare` |
| 共通属性による複数対象比較 | `table` |
| 分類・要因分解 | `tree` |
| 日付・時代に沿う出来事 | `timeline` |
| 定義・名前付き属性 | `keyvalue` |
| 単一の主張・注意 | `callout`または通常文章 |
| 具体例 | 親カード内の補足 |

これにより、内容抽出の品質と表示ロジックを別々にテストできます。また、単なる並列項目を`flow`へ押し込み、存在しない順序や因果を暗示することを防ぎやすくなります。

### 評価方針

新方式は、カードがきれいに見えるかだけでは評価しません。少なくとも次を記録します。

- 重要な主張・数値・固有名詞の網羅率
- 各知識単位が原文で支持される割合
- 重複カードの割合
- 1単位に複数の独立論点が混ざっていないか
- 章・節への所属が自然か
- ASRで不確かな語を確定表示していないか
- セッション1分あたりの入力・出力トークン
- 停止から品質版完成までの時間
- カードから原文・音声位置へ到達するまでの操作量

既存方式と新方式を同じ最終文字起こしへ適用し、カードだけを見た比較と、原文を照合する根拠評価を分けて行います。実装のフェーズ、互換性方針、回帰条件は[`PLAN.md`](PLAN.md)に記載しています。
