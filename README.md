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
