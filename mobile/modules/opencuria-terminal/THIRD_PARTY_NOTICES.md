# Third-Party Notices

## t3code

The native module (Swift, Kotlin, C++ JNI bridge, build scripts) and the JavaScript terminal
helpers in `mobile/src/tools/terminal/` are adapted from t3code's mobile terminal.

- Upstream project: https://github.com/pingdotgg/t3code
- Source: `apps/mobile/modules/t3-terminal`, `apps/mobile/src/features/terminal`
- License: MIT, Copyright (c) 2026 T3 Tools Inc. (full text in `LICENSE`)

## Ghostty / libghostty

The iOS terminal renderer vendors `GhosttyKit.xcframework`, a libghostty build produced from
VVTerm's custom-I/O and live-padding Ghostty branch.

- Upstream project: https://github.com/ghostty-org/ghostty
- Vendored source branch: https://github.com/Yash-Singh1/ghostty/tree/t3code/custom-io-ordered-feed
- Vendored revision: `cf8edc23f3a6a87a96e41a90013e89e987d34980`
- Based on: https://github.com/wiedymi/ghostty/tree/vvterm/custom-io-padding
- Reference integration: https://github.com/vivy-company/vvterm
- License: MIT, Copyright (c) 2024 Mitchell Hashimoto, Ghostty contributors
  (full text in `Vendor/libghostty-vt/LICENSE`)

Keep this notice in sync when updating `Vendor/libghostty`.

## Ghostty / libghostty-vt

The Android terminal renderer vendors upstream `libghostty-vt` shared libraries
(`android/src/main/jniLibs/`) and C headers (`Vendor/libghostty-vt/include`).

- Upstream project: https://github.com/ghostty-org/ghostty
- Vendored revision: `9f62873bf195e4d8a762d768a1405a5f2f7b1697` (`Vendor/libghostty-vt/VERSION`)
- Build patches: `scripts/libghostty-android-patches/`
- License: MIT (full text in `Vendor/libghostty-vt/LICENSE`)

Keep this notice, the headers and the libraries in sync when updating `Vendor/libghostty-vt`.

## MesloLGS NF (Android terminal font)

- Files: `android/src/main/assets/fonts/MesloLGS-NF-{Regular,Bold}.ttf`
- Source: https://github.com/romkatv/powerlevel10k-media (Meslo LG patched with Nerd Fonts glyphs)
- Upstream: Meslo LG by André Berg (customization of Apple's Menlo), Nerd Fonts patcher
- License: Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0)
