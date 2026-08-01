# Minimal Realtime Transcription CLI

macOSの音声入力を `gpt-live-transcribe` でリアルタイム文字起こしし、確定した文章をMarkdownへ保存する最小CLIです。

## 必要なもの

- macOS
- `uv`
- `ffmpeg`
- `OPENAI_API_KEY` を設定した `.env.local`

`.env.local` はGit管理外です。APIキーの値をソースコードやコミットへ入れないでください。

## 使い方

最初に音声入力番号を確認します。

```sh
uv run transcribe.py --list-devices
```

表示された番号を指定して開始します。

```sh
uv run --env-file .env.local transcribe.py --device 0
```

途中結果はターミナルへ表示され、確定結果は `transcripts/YYYYMMDD-HHMMSS.md` へ逐次保存されます。終了は `Ctrl-C` です。

初回起動時にmacOSから求められたら、ターミナルのマイク利用を許可してください。拒否した場合は「システム設定 → プライバシーとセキュリティ → マイク」から変更できます。

## AudibleなどのMac音声

システム音声を入力として扱うには、BlackHoleを別途インストールし、「Audio MIDI設定」で通常の出力先とBlackHoleを含む複数出力装置を作成します。その後、`--list-devices` に表示されるBlackHoleの番号を指定します。

この最小版は入力を1つだけ扱います。マイクとの同時ミックス、翻訳、話者分離、音声保存、自動再接続は行いません。
