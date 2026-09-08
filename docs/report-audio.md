# Report audio

Audio uses the local Kokoro-82M model through `kokoro-onnx` in the setup below.
It is optional and off by default. With it enabled, each new report gets a
playable voice recording below its text in both General and the agent's topic.
It reads every report section and reference title; URLs remain clickable in the
text rather than being spelled aloud. Speech is generated locally, then the audio
is uploaded to the same Telegram group as the text. No paid speech service is used.

## Reuse a local speech service

If a Kokoro-compatible service is already running, Backbone can reuse its loaded
model. The endpoint receives JSON `{"text":"...","voice":"af_heart","speed":1.0}`
and returns PCM WAV. Only HTTP loopback URLs are accepted, and redirects are refused.
Alfred's Kokoro Speak Selection service uses this interface on port 8765.

Install FFmpeg if it is not already available (`brew install ffmpeg` on macOS,
or your Linux package manager). It converts WAV to Telegram's Ogg/Opus voice format.
Then configure Backbone:

```bash
backbone config set telegram.tts_url http://127.0.0.1:8765/speak
backbone config set telegram.tts_voice af_heart
backbone config set telegram.report_audio true
```

These settings take effect without restarting agents. The default voice matches
the example service's English voice. Choose a voice the local service supports.

## Set up a standalone service

In a repository checkout, the optional adapter in
[examples/report_speech_server.py](../examples/report_speech_server.py) uses
[kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx). It keeps model dependencies
outside Backbone's installation. Download the model and voices once:

```bash
mkdir -p ~/.local/share/backbone-speech
curl -fL https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx -o ~/.local/share/backbone-speech/kokoro-v1.0.onnx
curl -fL https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin -o ~/.local/share/backbone-speech/voices-v1.0.bin
uv run --no-project --with kokoro-onnx --with soundfile --with fastapi --with uvicorn python examples/report_speech_server.py --model ~/.local/share/backbone-speech/kokoro-v1.0.onnx --voices ~/.local/share/backbone-speech/voices-v1.0.bin
```

Keep this process running, or manage it with your OS's service manager. Do not start
a second service on an occupied port; reuse the existing endpoint instead.
Install FFmpeg and apply the three Backbone settings above, then publish a new
report. Its text and full recording will appear automatically in Telegram.
Backbone never downloads a model or changes another application's speech setup.
The adapter's maximum input is 400 characters; Backbone splits full reports into
smaller chunks, requires every chunk to succeed, joins them, and encodes one recording.

## Delivery and troubleshooting

Text and audio have independent delivery queues. Failed speech generation or voice
uploads retry without resending the text. After the first successful upload, Telegram's file ID is reused for the other
destination and for partial-upload retries. An upload that fails before a file ID
is saved may require generating the recording again.
A voice message replies to its corresponding text report so they remain associated.

Read `backbone updates show ID --json`: `telegram_delivery` tracks both text copies,
and `telegram_audio_delivery` tracks the optional audio. `not_requested` means audio
was not selected. Enable audio before new reports are delivered; old reports are
not automatically narrated. To pause audio, set `telegram.report_audio false`;
pending audio waits until it is re-enabled, within the normal report retention.

A missing agent topic delays that copy while General remains available. Ordinary
agents receive topics automatically; swarm members intentionally have no topics
and receive their reports in General only. Renamed or forgotten agents may need
routing attention if their delivery was already partly sent. Destinations already
selected for a report are not silently moved to another group.

If audio stays pending, check the local `/health` endpoint, voice name, FFmpeg on
the service's PATH, and Backbone's log for the report ID and error type. Temporary
audio files are deleted after generation/upload attempts. As with text, a crash
between Telegram accepting an upload and saving its receipt can produce a duplicate.

Telegram's supported voice format is documented in
[sendVoice](https://core.telegram.org/bots/api#sendvoice).
