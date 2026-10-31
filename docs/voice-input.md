# Voice input and pointing recordings

Research date: 2026-10-31. This document records the implementation decisions
and browser limitations; it does not imply a live provider was exercised.

## Using voice input

Connect an OpenRouter or OpenAI-compatible provider account in Settings and
configure a small model (or a default Build model) for text cleanup. Open the
microphone's settings in the chat composer and select a speech model. The first
microphone click opens setup; after configuration, the microphone starts the
next dictation directly. Finish inserts editable text into the existing draft.
Nothing is sent to the agent automatically.

Enable **Record pointing too** before starting to include chat images and the
workspace desktop. While this mode is active, desktop clicks indicate a
position; finish/discard controls remain available in the image and desktop
viewers. Each successful pointing recording adds an agent-readable file
reference to the draft. Its directory is
`/workspace/.opencuria/recordings/<recording-id>/`, containing `metadata.json`,
`transcript.json`, `pointer.json`, and optional JPEGs in `frames/`. Plain
dictation does not create these workspace artifacts.

Recordings stop after five minutes or when the tab is hidden or the microphone
ends. Network failures retain the recording for retry. A failed visual capture
requires an explicit choice to use the dictation without visual artifacts.
Each recording is limited to five minutes and 1,500,000 audio bytes. The browser
requests 24 kbit/s audio; a browser-selected codec or bitrate can reach the byte
limit sooner. Recognition and cleanup each have a 65-second total timeout.
Microphone recording requires a secure browser context (HTTPS or localhost).
Audio is sent to the selected speech provider; text cleanup uses the configured
text provider. Desktop screenshots are stored in the workspace.

When complete word timestamps are available, punctuation-defined sentences use
the first word's start and last word's end. Otherwise, provider phrase timing is
retained. A phrase may contain multiple sentences. When neither is usable,
the transcript explicitly records
`timing: "approximate"` and one interval covering the recording. That fallback
does not support precise sentence-to-pointer alignment; select a model with
structured timing when that precision is needed.


### Saved artifact format

All times are milliseconds from microphone recording start. `metadata.json` is
written last as the completion marker. It contains schema version, recording ID,
server `saved_at` UTC, duration, `transcript_path`, `pointer_path`, and actual
`frames: [{t_ms, path}]` observations. `transcript.json` preserves raw and cleaned
phrases without changing their timing anchors, for example:

```json
{
  "schema_version": 1,
  "text": "Change this button.",
  "timing": "provider",
  "segments": [
    {
      "start_ms": 420,
      "end_ms": 1850,
      "raw_text": "Um change this this button.",
      "text": "Change this button."
    }
  ]
}
```

`pointer.json` identifies `coordinate_space: "original-image-pixels"`, the
100 ms sampling goal and `events: [{t_ms, event, x, y, target}]`. A target contains
`kind` (`image` or `desktop`), workspace `path`, `width` and `height`. Desktop paths
reference saved `frames/000000.jpg` files; outside supported surfaces the target
and coordinates are null. Desktop frames target 500 ms intervals, with only one
capture/upload in flight. Slow uploads reduce the actual frame rate.

Frames upload during capture. Cancellation or failure can leave partial files
without completed metadata; no reference is inserted into the draft. Remove such
directories through the workspace files UI if unwanted. All frame uploads must
succeed before saving the completion manifest. A failed cleanup keeps the raw
transcript and shows a warning. API-key dictation needs `harness:run`; persisting
manifests additionally needs the existing `terminal:access` authority.

Automated checks use controlled microphone/provider/runner responses. Public
OpenRouter catalog discovery was verified without authentication. Real microphone
hardware and billable speech/cleanup requests were not exercised; these remain
deployment smoke checks with the configured account and browser.

## Dictation pipeline

Use two stages: speech recognition, then conservative text cleanup. Recognition
owns the original text and timing. Cleanup removes fillers, repeated false
starts and accidental repetition, adds punctuation, and preserves language,
meaning, technical identifiers and explicit corrections. It must never answer
the dictated request or invent content. A failed cleanup should leave the raw
transcript recoverable. Silence must not enter the cleanup stage.

OpenRouter provides `/api/v1/audio/transcriptions`, accepting base64 audio with
an explicit format or an OpenAI-compatible multipart upload. Request
`verbose_json` and segment/word timestamps for pointing mode. Timing support
depends on the selected model/provider; some reject this format. Discover STT
models through the models API's transcription output modality. Keep credentials
in the existing provider-account vault and make provider requests from the
backend. [OpenRouter STT documentation](https://openrouter.ai/docs/guides/overview/multimodal/stt)

Do not assume an audio-capable chat model produces reliable acoustic timestamps.
For ordinary dictation a plain transcript is sufficient. For pointing, require
timing-capable transcription, or explicitly label any fallback interval as
approximate. Never distribute sentence times evenly across the recording and
present them as measured speech times. An STT segment can contain multiple
sentences; word timings allow sentence boundaries to be reconstructed without
changing their acoustic alignment. Preserve the raw timing alongside edited
text. The implementation requests both granularities and prefers validated word
timing. It uses sentence punctuation from the full transcript, or word
punctuation if the full text has none. It never splits a combined word token
into invented subintervals. Malformed or partial word metadata falls back to
provider phrases, then the complete transcript with approximate bounds.
Abbreviations and missing punctuation can affect sentence grouping; forced
alignment or language-aware sentence detection remains a possible extension.

WhisperX demonstrates why text recognition and precise alignment are separate
problems: its pipeline adds voice activity detection and forced alignment to
recognition. A local alignment service would add GPU/model/runtime requirements
and is unnecessary for an initial implementation with timing-capable hosted STT.
[WhisperX repository and pipeline](https://github.com/m-bain/whisperX)

OpenWhispr and Handy are useful UX references for explicit recording state and
simple dictation controls. Their native desktop integration does not transfer
directly to a browser application. No source code was copied or dependencies
installed from either project. [OpenWhispr](https://github.com/OpenWhispr/openwhispr),
[Handy](https://github.com/cjpais/Handy)

## Recording and one clock

Request microphone access only after a user action, negotiate a supported MIME
type with `MediaRecorder.isTypeSupported`, and collect all data including the
last `dataavailable` event before processing. Always release audio tracks on
completion, failure, cancellation and component disposal. A pending microphone
permission request can finish after cancellation; its resulting stream still
needs immediate disposal.

Use one `performance.now()` origin for recording duration, pointer events,
frame observations and transcript offsets. Store a UTC wall-clock label separately for human identification; the
implementation records server save time, not an inferred microphone start time. The monotonic clock avoids system-clock adjustment.
[MDN: performance.now](https://developer.mozilla.org/en-US/docs/Web/API/Performance/now)

Sampling goals are 100 ms for the pointer and 500 ms for desktop frames. These
are best-effort intervals, with actual observation times persisted. Browser
timers and media events can be delayed, particularly in background tabs or on
locked devices; chunk count multiplied by `timeslice` is not a clock. Do not
overlap frame requests or produce an unbounded upload queue. Bound duration,
audio bytes, frame bytes, pointer count and total retained data.
[MDN: MediaRecorder dataavailable timing](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/dataavailable_event)

## Pointing targets

Ordinary dictation is the default. Pointing must be separately selected before
recording, with an obvious active indicator. It observes workspace content, so
its result should be inserted into the draft for review before sending.

For a chat image, save its workspace source path and coordinates in the original
image's pixels, together with source width and height. Derive coordinates from
the rendered image bounds so zoom and pan remain correct. Letterboxing and
positions outside the image are not image hits. Remote HTTP URLs and temporary
authenticated browser URLs are not workspace paths; either save a controlled
copy or reject that target with a clear indication.

The workspace desktop is an embedded KasmVNC viewer, sometimes cross-origin.
Parent-document pointer listeners cannot see inside that iframe. There are two
sound options:

1. A dedicated pointing overlay above the viewer captures positions relative to
   the actual scaled framebuffer and blocks mouse interaction with the desktop
   while pointing. This is simple and works across origins.
2. A viewer bridge sends pointer/frame observations with `postMessage`, validating
   origin, source window, shape and active recording identity. This supports
   interaction but requires a maintained bridge inside the viewer.

The existing iframe connection-recovery code already treats cross-origin
viewers specially. Extending that controlled integration is preferable to
unguarded `contentDocument` access. [MDN: postMessage and origin checks](https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage)

Capture workspace desktop frames through the existing workspace desktop/runtime
boundary or a controlled viewer bridge; do not make browser-wide screen sharing
a requirement for pointing inside OpenCuria. A frame path must only be assigned
after the frame was captured and saved. Persist the actual capture observation
time, and associate a pointer event with the most recent available frame.
Record gaps and failures instead of silently claiming fresh frames.

Capturing the user's physical desktop is a different feature. The Screen Capture
API asks the user to choose a display/window and returns a video stream. Its
cursor option controls cursor inclusion in pixels; it does not grant a global
stream of operating-system pointer coordinates. Exact physical-desktop pointer
tracking outside the page requires a native companion or a suitable browser
extension. [W3C Screen Capture specification](https://www.w3.org/TR/screen-capture/)

## Workspace artifacts and validation

Use a unique recording directory below `/workspace/.opencuria/recordings/`,
with a manifest, transcript JSON, pointer JSON and frame assets. Each timestamp
is milliseconds relative to the same recording start. Preserve raw and cleaned
text and an explicit timing quality. Each pointer observation includes its
event kind, target type, source path, source dimensions and source coordinates.
The completion manifest includes schema version, duration, actual frame
observations and links to the transcript and pointer log. An agent-facing draft
reference should name the manifest/transcript paths and explain the timeline
convention.

Validate ownership and organization on every request, resolve workspace and
provider access through existing services, generate artifact paths server-side,
and reject path traversal, nonfinite or out-of-range coordinates, oversized
audio/log/frame payloads and inconsistent durations. Complete a manifest only
after all referenced assets have been persisted. Cancellation must not insert
text or attach artifacts to a different chat after navigation.

Verification should cover microphone denial, late permission resolution after
cancel, silent input, final recorder chunk, unsupported codecs, interrupted
tracks, transcription/cleanup timeouts, unsupported timing responses, cleanup
failure, double stop, overlapping frame requests, zoomed image coordinates,
desktop scaling, origin validation, recording limits, cross-workspace access,
malformed logs and partial persistence failures. Live provider and physical
microphone checks require available credentials and hardware and must be
reported separately from mocked integration tests.
