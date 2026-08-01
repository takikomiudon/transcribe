# Minimal Realtime Transcription CLI

macOSの日本語・英語・韓国語音声を `gpt-live-transcribe` でリアルタイム文字起こしし、確定した文章をMarkdownへ保存する最小CLIです。

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
uv run transcribe.py --device 0
```

途中結果はターミナルへ表示され、確定結果は `transcripts/YYYYMMDD-HHMMSS.md` へ逐次保存されます。終了は `Ctrl-C` です。

英語・韓国語の確定結果を日本語でも保存する場合は、`--translate-ja` を追加します。原文の文字起こしには `gpt-live-transcribe`、日本語訳には `gpt-5.6-luna` を使用するため、翻訳分のAPI料金が別途かかります。

```sh
uv run transcribe.py --device 0 --translate-ja
```

無音待機中の音声はAPIへ送らず、発話開始時に直前500msの音声を付けて送信します。800msの無音または30秒の連続音声で発話を確定します。周囲の雑音で区切られない場合は、`--silence-threshold 800` のように既定値500より大きくしてください。

初回起動時にmacOSから求められたら、ターミナルのマイク利用を許可してください。拒否した場合は「システム設定 → プライバシーとセキュリティ → マイク」から変更できます。

## AudibleなどのMac音声

システム音声を入力として扱うには、BlackHoleを別途インストールし、「Audio MIDI設定」で通常の出力先とBlackHoleを含む複数出力装置を作成します。その後、`--list-devices` に表示されるBlackHoleの番号を指定します。

この最小版は入力を1つだけ扱います。マイクとの同時ミックス、話者分離、音声保存、自動再接続は行いません。
