# Minimal Realtime Transcription CLI

macOSの日本語・英語・韓国語音声をElevenLabs Scribe Realtimeでリアルタイム文字起こしし、録音中はセッション全体をElevenLabs Scribe v2で30秒ごとに再文字起こしする最小CLIです。日本語を主言語、英語・韓国語を副言語として認識します。

## 必要なもの

- macOS
- `uv`
- `ffmpeg`
- `curl`（macOS標準）
- `ELEVENLABS_API_KEY` を設定した `.env.local`
- 翻訳または図解を使う場合のみ `OPENAI_API_KEY`

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
OPENAI_API_KEY='your-openai-api-key' # --translate-ja / --cards 使用時のみ
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

途中結果はターミナルへ表示され、Realtimeの確定結果は速報ログとして `transcripts/YYYYMMDD-HHMMSS.md` へ逐次保存されます。ブラウザにはScribe v2による精度重視の全文が表示され、30秒ごとに更新されます。終了は `Ctrl-C` です。

処理中は開始から終了までの全音声を `recordings/YYYYMMDD-HHMMSS.wav` へ保存します。この蓄積WAVを30秒ごとにElevenLabs Scribe v2へ送り、精度重視の結果で `transcripts/YYYYMMDD-HHMMSS-final.md` を書き直します。停止時にも全WAVで最終更新し、保存に成功するとWAVは自動で削除されます。API通信や保存に失敗した場合は、直前のfinalと再試行用WAVが残ります。

更新間隔は `--batch-refresh-seconds 60` のように変更できます。毎回セッション全体を送り直すため処理量は録音時間に対して累積し、60分の録音を30秒間隔で更新すると周期更新だけで約60.5時間分の音声処理になります。Realtime、各周期更新、停止時の最終更新にはそれぞれElevenLabsの利用料金が発生します。

英語・韓国語の確定結果を日本語でも保存する場合は、`--translate-ja` を追加します。原文の文字起こしにはElevenLabs Scribe Realtime、日本語訳にはOpenAI `gpt-5.6-luna` を使用するため、翻訳分のAPI料金が別途かかります。

```sh
uv run transcribe.py --device 0 --translate-ja
```

確定した発話からOpenAIで話題ごとの図解カードを生成する場合は、`--cards` を追加します。録音中はRealtimeの確定結果から速報カードがローカルビューアへ追加されます。停止後はScribe v2のfinal transcript全文を見て意味単位を分割し直した品質版で、同じ `cards_output/YYYYMMDD-HHMMSS.html` とJSONを差し替えます。品質版の生成に失敗した場合は速報版が残ります。

```sh
uv run transcribe.py --device 0 --cards
```

生成は既定で300文字、20秒間の新規発話なし、または前回生成から90秒のいずれかで始まります。必要なら `--cards-character-threshold`、`--cards-idle-seconds`、`--cards-max-seconds` で調整し、ビューアのポート競合時は `--cards-port` を変更できます。`--translate-ja` と同時指定した場合も、図解には翻訳前の原文を使います。

音声は無音を含め、16kHz PCMを100ms単位で常時送信します。発話の確定はElevenLabs側のVADに任せ、800msの無音で区切ります。終了時は100ms未満の残りも送信して確定します。

初回起動時にmacOSから求められたら、ターミナルのマイク利用を許可してください。拒否した場合は「システム設定 → プライバシーとセキュリティ → マイク」から変更できます。

## AudibleなどのMac音声

システム音声を入力として扱うには、BlackHoleを別途インストールし、「Audio MIDI設定」で通常の出力先とBlackHoleを含む複数出力装置を作成します。その後、`--list-devices` に表示されるBlackHoleの番号を指定します。

この最小版は入力を1つだけ扱います。マイクとの同時ミックス、話者分離、自動再接続は行いません。

## Raycastから起動

Raycastの「Settings → Extensions」で `+` を押し、「Add Script Directory」からこのリポジトリの `raycast` ディレクトリを追加します。次の2コマンドが使えるようになります。

- `transcribe`
- `transcribe-ja`

初回起動時にmacOSから求められたら、RaycastによるTerminalの操作を許可してください。各コマンドにはRaycastの設定から任意のホットキーを割り当てられます。

起動には `Live Transcribe` という専用Terminalタブを1つだけ使用します。文字起こし中にどちらかのコマンドを再実行した場合は、新しく起動せず、そのタブを前面へ表示します。終了するときは専用タブで `Ctrl-C` を押してください。終了後は、どちらかのコマンドを実行すると同じタブで再開します。
