# Native chat design

The mobile chat follows the observed ChatGPT Android 1.2026.286 interface.
The reference was inspected in the installed Android app and the native
guest-session comparison captures from 2026-10-23.
Account-only ChatGPT screens were not available in the guest session.

## Presentation

- Borderless header with circular 44-point controls and a two-line menu glyph.
  OpenCuria keeps its workspace switcher, new-chat action and account menu.
- Quiet light surfaces and a black dark theme; blue send arrows, a subdued
  disabled send state and separate prompt/composer colors.
- One-line empty composer that expands for replies, focus, drafts, skills and
  attachments. The same native input stays mounted during layout changes.
  Plus opens the rounded attachment menu; settings occupies the adjacent
  control position and retains engine, mode, model and effort selection.
- Right-aligned prompt bubbles, 24-point corners, 16-point horizontal transcript
  margins and 17/26-point body typography. Assistant answers stay unboxed.
- Copy/fork actions below completed answers. Long-press prompts to copy or
  edit/rerun. Model, time and usage are available in message details.
- Side navigation occupies 80% of a phone's width (maximum 360 points), with
  scrollable workspaces/history, search and a pinned account footer.
- Keyboard-controller layout handles keyboard avoidance on Android and iOS.
  Safe-area padding is removed while the keyboard occupies the bottom edge.

Shared protocol rules, networking, workspace readiness, run controls, gates,
media handling and model-selection rules remain in their existing layers.
No voice or account features were added to imitate unavailable functionality.

## Verification

Use the root workspace commands:

```sh
npm run test -w mobile
npm run type-check -w mobile
npm run export -w mobile
```

Native visual checks use the Android development client with disposable local
API/Socket.IO sample data. Screenshots demonstrate real React Native rendering,
not an external provider run. iOS export verifies the JavaScript bundle;
native iPhone/simulator rendering requires a Mac.

Verified on 2026-10-23: 66 logic tests, 35 native component tests, mobile
TypeScript, and Android/iOS Hermes exports passed. Native Android API 35 checks
covered light/dark chat, side navigation and swipe dismissal, message actions,
attachments, and keyboard avoidance. With the keyboard visible at y=1002 on
the 720×1600 display, the input occupied y=805–881 and the toolbar y=885–973.
