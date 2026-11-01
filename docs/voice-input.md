# Voice input and pointing recordings

## Setup and use

Under **Settings → Provider & Models → Voice input**, choose:

- **Speech recognition** (`speech_model`): a transcription model from the
  selected OpenRouter or OpenAI-compatible account. **Automatic** uses
  `openrouter/elevenlabs/scribe-v2` when OpenRouter is connected, otherwise
  `openai-compatible/whisper-1`. Custom model IDs are supported.
- **Text cleanup** (`voice_cleanup_model`): an optional dictation editor.
  When unset, it inherits **Small Model**, then the default **Build** agent
  model. The current chat model does not override this choice.

Both choices are saved in the organization's existing provider configuration.
Provider credentials remain server-side. Audio is sent to the selected speech
provider; the transcript is sent to the cleanup provider.

In chat, click the microphone to start. While recording, a bar replaces the
composer toolbar: **Discard** (X) drops the audio without transcription, a live
waveform shows the signal, **Stop and edit** (■) inserts the transcript into the
existing draft for review, and **Stop and send** sends the draft as soon as the
transcript is ready. The same bar is available in image and desktop viewers.
Models are configured only in Settings.

Pointing needs no opt-in. Every dictation observes workspace images and the
desktop passively; a pointing recording is only created once the speaker points
at a linked image or the desktop. Desktop frames are captured from the first
pointer on the desktop onward. A pointer icon in the bar confirms that pointing
is recorded. Plain dictation creates no workspace recording files.

## Audio and text processing

The browser requests mono audio with echo cancellation, noise suppression and
automatic gain control, and records using a supported codec at a requested
64 kbit/s. The final MediaRecorder chunk is collected before processing. A signal
meter helps identify an inactive microphone. Conservatively detected near-digital
silence skips transcription; this is not a speech classifier. When a meter is
unavailable or suspended, silence gating is disabled instead of discarding audio.

Recognition requests temperature zero and structured word/segment timestamps.
The backend selects the speech model from saved Settings; client-supplied model
IDs cannot override it. Cleanup edits the whole dictation so an explicit
correction can replace words from an earlier phrase. It removes fillers and
false starts while preserving language, intent, identifiers and intentional
repeated requests. It must not answer the dictated request.

Cleaned `text` is separate from timestamped source `segments`. Cleanup never
rewrites their recognition text or timing anchors; `segments_kind: "source"`
makes that distinction explicit. Complete word timestamps produce sentence
intervals where punctuation aligns with measured word boundaries. Otherwise,
provider phrase intervals are retained. If neither is usable, the complete
transcript has one interval spanning the recording and `timing: "approximate"`.
That fallback cannot provide precise sentence-to-pointer alignment. No timing
is interpolated from text length. The unfiltered ASR output remains in
`raw_text`; only segments confidently marked non-speech by provider diagnostics
can be excluded from source phrases. Failed cleanup keeps recognized speech
and shows a warning.

## Desktop and image capture

Pointing records workspace content, not the user's physical desktop outside
OpenCuria. Desktop interaction remains normal during capture: clicks, drags and
keyboard input reach the VM. There is no input-blocking pointing overlay.

A passive bridge in the authenticated KasmVNC viewer observes pointer events
and captures the actual framebuffer canvas under `#noVNC_container`. Auxiliary
cursor canvases are excluded. Coordinates and dimensions come from that live
framebuffer, even when its resolution differs from workspace settings (for
example 1280×720 instead of a configured 1920×1080). Snapshot pixels are frozen
before asynchronous JPEG encoding. Parent/viewer messages validate source,
origin, recording identity and bounds; authenticated viewer URLs never enter
recording logs.

Images use their canonical workspace path and original-pixel coordinates.
Rendered bounds account for zoom and pan; letterboxing and unsupported external
images produce no image target. Desktop pointer positions are associated with
the latest captured frame of the same dimensions. Outside a supported surface,
coordinates and target are null.

During pointing capture, a red laser cursor marks supported image pixels and
desktop surfaces with a matching captured frame. Controls, letterboxing and
unlinked images keep their normal cursor. Stopping, discarding or a capture
failure restores the normal cursor immediately.

Pointer sampling targets 100 ms, with separate down/up/leave events. Desktop
snapshots target 500 ms once the desktop was pointed at. Actual observation times use the recording's monotonic
clock; browser delays are not disguised as exact sampling intervals. Capture
and uploads run independently with one capture and at most eight uploads in
flight. A full queue or failed upload surfaces an error rather than producing a
successful partial recording.

## Stored recordings and limits

Successful pointing captures are saved under:

```text
/workspace/.opencuria/recordings/<recording-uuid>/
  metadata.json
  transcript.json
  pointer.json
  frames/000000.jpg
```

All `t_ms`, `start_ms` and `end_ms` values are milliseconds relative to microphone
recording start. `transcript.json` contains cleaned `text`, source `segments`,
`segments_kind`, `raw_text`, timing quality, model IDs and cleanup warnings.
`pointer.json` contains `coordinate_space: "original-image-pixels"` and
`events: [{t_ms, event, x, y, target}]`; targets identify kind, path and dimensions.
`metadata.json` links both files and `frames: [{t_ms, path}]`, with duration and
UTC server save time. It is written last, after all referenced uploads succeed.
The draft links this completion manifest for the agent.

Recordings are limited to five minutes and 1,500,000 audio bytes. At the requested
64 kbit/s, the byte limit can finish recording before five minutes; codec/bitrate
choices vary by browser. Desktop frames are bounded individually and in total.
Recognition and cleanup each have a 65-second total timeout. Hiding the tab or
ending the microphone track finishes recording. Navigation, disabling the
composer and unmounting discard it and fence late asynchronous results.

Network errors retain audio for Retry. Failed visual capture requires an
explicit **Use dictation only** choice to keep the text without visual artifacts;
failures of an observer that never linked anything cannot affect plain dictation.
Retry and **Use dictation only** keep the chosen stop or send action.
Discard or failure can leave uploaded frames without completed metadata; these
partial directories can be removed through workspace files. Recording requests
validate organization/workspace access and bounded canonical paths, timelines,
frame references and finite pixel coordinates. API keys need `harness:run`;
manifest persistence additionally needs the existing `terminal:access` authority.

## HTTP deployment

OpenCuria has no HTTPS gate. HTTP on `localhost` or `127.0.0.1` works with the
normal browser microphone permission. Browsers block microphone APIs on ordinary
HTTP LAN origins by default; web code and response headers cannot grant access.
Chromium can explicitly trust the exact origin, including its port, through
`chrome://flags/#unsafely-treat-insecure-origin-as-secure`, or the startup argument
`--unsafely-treat-insecure-origin-as-secure=http://host:port`. Relaunch and grant
microphone permission. This origin exception does not encrypt HTTP traffic.

For the default local `compose.yml`, a remote server can be reached through:

```bash
ssh -N -L 8080:127.0.0.1:8080 -L 8000:127.0.0.1:8000 user@server
```

Open `http://127.0.0.1:8080`. Both forwards are needed because that configuration
uses `127.0.0.1:8000` for API/WebSocket traffic. Other deployments also need their
runtime API/WebSocket URLs and allowed origins configured for the local address.
A tunnel does not rewrite those values. Secure UUID generation falls back to
`crypto.getRandomValues` when HTTP omits `crypto.randomUUID`.

See [MDN microphone access](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia),
[Chromium's origin exception](https://new.chromium.org/Home/chromium-security/deprecating-powerful-features-on-insecure-origins/)
and [OpenRouter transcription](https://openrouter.ai/docs/guides/overview/multimodal/stt).

## Verification scope

Validation includes the real local OpenCuria browser, a nested workspace VM and
its KasmVNC framebuffer, desktop interaction during capture, saved recording
artifacts, and real OpenRouter transcription/cleanup calls using controlled
spoken audio. Unit tests cover lifecycle, permissions, provider failures,
alignment and malformed/partial recordings. Physical microphone hardware has
not been exercised; browser-generated/supplied audio is distinct from that check.
