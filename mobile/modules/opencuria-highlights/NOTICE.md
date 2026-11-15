# Notices — opencuria-highlights

Native syntax highlighter of the mobile file viewer, adapted from the
`t3-highlights` module of [t3code](https://github.com/pingdotgg/t3code)
(MIT, Copyright (c) 2026 T3 Tools Inc., see `LICENSE`).

| Component | Files | License |
| --- | --- | --- |
| [@pierre/highlights](https://www.npmjs.com/package/@pierre/highlights) 0.2.0 lexers, compiled from WebAssembly to C with wasm2c (`highlights.wasm` sha256 `cf5fc4d83e7c1ddab19d163de096f787f87efe61504909aa469434f61ef51e2e`) | `Vendor/highlights/` | Apache-2.0 |
| [wasm2c runtime](https://github.com/WebAssembly/wabt) (WABT) | `Vendor/wasm2c/` | Apache-2.0 (`Vendor/wasm2c/LICENSE`) |
| [SIMDe](https://github.com/simd-everywhere/simde) | `Vendor/simde/` | MIT (`Vendor/simde/LICENSE`) |
| t3code wrapper and bridges | `cpp/`, `ios/`, `android/` | MIT (`LICENSE`) |

The generated lexer sources are unmodified; the Apache License 2.0 text is in
`Vendor/wasm2c/LICENSE` and applies to `Vendor/highlights/` as well.
