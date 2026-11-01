# Pointing capture efficiency review

Historical investigation from 2026-10-31. The findings and proposal below describe
the earlier capture implementation. Revision tracking, snapshot reuse, sparse
pointer events and ten-minute recording support are now implemented; see
[Voice input](voice-input.md) for current behavior, limits and verification.
The original investigation did not run a CPU or bandwidth benchmark.

## Finding

`webapp/src/lib/voicePointing.ts` requests a desktop frame every 500 ms during
an explicit pointing recording. The authenticated bridge in
`backend/apps/runners/voice_viewer_bridge.py` creates a new full-size canvas,
copies the visible framebuffer and encodes JPEG at quality 0.85 for every
request. The parent converts each JPEG to base64 and uploads it through the
existing file transport. There is no content-change check.

This means approximately 120 encodes and uploads per visible desktop minute,
including time spent explaining an unchanged screen. Base64 adds approximately
one third to image payload size before transport metadata. Existing safeguards
already skip hidden surfaces, serialize capture, bound upload concurrency and
limit total bytes; the repeated work is within those safeguards.

Pointer positions are separate from images: they target 100 ms intervals and
reference the latest earlier frame of matching dimensions. The stored manifest
and backend validation permit irregular frame intervals. Sparse screenshots
can therefore retain the current artifact format and agent image-reading flow.

## Recommendation

Keep JPEG artifacts, but skip copying, encoding and uploading when the viewer
has not drawn new desktop content since the last successful capture.

1. Capture an initial frame. While observing, track a content revision on the
   specific framebuffer canvas. Hook only that canvas's rendering context;
   ignore auxiliary cursor and snapshot canvases. The pinned renderer uses
   `drawImage`, `fillRect` and `putImageData`. Cover dimension resets and canvas
   replacement as well. A DOM MutationObserver alone cannot detect pixel draws.
2. Extend the trusted frame request/reply with a revision and an explicit
   unchanged response. The existing 500 ms request can initially remain as a
   cheap revision check, preserving the current upper capture rate. A later
   change notification could replace these requests with a trailing, throttled
   capture after rendering. Do not debounce indefinitely during animation.
3. Freeze both revision and pixels before asynchronous encoding. A draw while
   encoding must leave a newer revision pending. An unchanged reply must not
   allocate a screenshot canvas or pass through upload bookkeeping.
4. Keep observation renewal independent of new frames. Today `syncViewer()`
   sends `voice-observe` from the capture loop; the viewer expires observation
   after three seconds without renewal. Skipping that renewal would lose
   pointing on an idle desktop. Preserve source/origin/session validation,
   stop/cancel cleanup, visibility handling and upload error reporting.
5. Fence revisions by viewer instance and recording. Rebinding or resizing
   requires a fresh frame even if the numeric revision happens to match.
   Retain the previous image reference while unchanged; do not invent later
   capture timestamps for that image. A version attached to pointer messages
   can also identify intervals where the displayed screen has changed but a
   corresponding image is not yet captured. Such intervals should have an
   explicit policy instead of silently appearing perfectly synchronized.

This is an incremental change to the bridge and capture client, with regression
tests in `voicePointing.test.ts` and `test_desktop_proxy.py`. It needs no new
dependency or recording service. Verify static content, drawing during encoding,
animation, same-size viewer replacement, resizing, keepalive expiry, untrusted
messages, failed encoding/uploads and stop/cancel against a real viewer.

For a screen with no redraws over one minute, one initial image instead of about
120 is roughly 99% fewer encodes and uploads. This is an illustrative calculation,
not measured CPU savings. Clocks, caret blinking, redundant redraws and animation
can still trigger captures; continuously changing content may see little saving.

## Repository comparison

The image recipe installs KasmVNC **1.3.3**. Its
[pinned web-client submodule](https://github.com/kasmtech/KasmVNC/tree/v1.3.3/kasmweb)
is `kasmtech/noVNC` at `bce2d6a7048025c6e6c05df9d98b206c23f6dbab`.
The findings below distinguish that implementation from current upstream code.

| Approach | Evidence | Fit for pointing |
| --- | --- | --- |
| Detect rendered desktop changes | The [pinned Kasm display](https://github.com/kasmtech/noVNC/blob/bce2d6a7048025c6e6c05df9d98b206c23f6dbab/core/display.js) maintains frame queues and draws to a 2D target context. Its `flip()` queues work; `onflush` is not an every-frame callback. | Best first step: observe actual target writes, retain screenshots. A generic public framebuffer-change event must not be assumed. |
| Record canvas commands | [rrweb's 2D observer](https://github.com/rrweb-io/rrweb/blob/main/packages/rrweb/src/record/observers/canvas/2d.ts) wraps drawing methods. Its [canvas recipe](https://github.com/rrweb-io/rrweb/blob/main/docs/recipes/canvas.md) also supports sampled images. | Useful precedent for change detection. Importing full DOM/canvas replay would add machinery for a pixel-based remote desktop. |
| Record canvas video | [WebRTC samples](https://github.com/webrtc/samples/blob/gh-pages/src/content/capture/canvas-record/js/main.js) combine `captureStream()` with `MediaRecorder`. | Worth considering for replay video later. Requires video artifact support and timestamped image extraction for the agent; encoder cost and codec support need measurement. It does not require enabling Kasm's disabled WebRTC transport. |
| Record remote-display instructions | [Apache Guacamole SessionRecording](https://github.com/apache/guacamole-client/blob/main/guacamole-common-js/src/main/webapp/modules/SessionRecording.js) replays protocol instructions with indexed frames/keyframes. | Good architecture for session replay, but Guacamole instructions cannot directly replay KasmVNC traffic. A protocol recorder/decoder is too large a change for this optimization. |

Hashing JPEGs after encoding would save duplicate uploads/storage but still pay
the copying and JPEG cost. Repeated full-resolution pixel comparisons also add
readback work. Merely increasing the interval reduces capture responsiveness;
capturing only on clicks misses speech while pointing or waiting for an update.
Change detection before image generation is the more targeted first improvement.

## Baseline verification

Existing tests passed: 14 frontend pointing tests and the backend's executable
viewer bridge test (1 passed, 37 deselected). These verify the current behavior
and trust boundary, not the proposed optimization or its resource savings.
