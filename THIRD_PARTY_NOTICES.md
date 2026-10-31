# Third-Party Notices

This repository is licensed under the **GNU Affero General Public License
v3.0 only (AGPL-3.0-only)** — see `LICENSE` at the repository root. That
license continues to apply to the repository as a whole, including to the
files listed below. Nothing in this notice replaces or weakens the AGPL-3.0-only
license of the surrounding OpenCuria project.

## Monaco Editor

The webapp bundles **Monaco Editor 0.57.0**, the standalone
editor from VS Code, by Microsoft Corporation under the **MIT License**.
Its copyright and complete license are distributed with the webapp at
`/third-party/monaco-editor-LICENSE.txt` (source:
`webapp/public/third-party/monaco-editor-LICENSE.txt`). Upstream:
[microsoft/monaco-editor](https://github.com/microsoft/monaco-editor).

## Computer Use runtime

The Computer Use runtime in `packages/computer-use` is independently written
OpenCuria code. It does not distribute ChatGPT binaries or source code.
Its bundled MCP SDK and transitive dependencies retain their upstream license
notices in `runner/src/runtime/assets/computer-use-0.2.0.NOTICES.txt`.
Node.js is downloaded from its pinned official release at runtime. Its complete
license notice is retained in `packages/computer-use/third_party/` and included
in the installed runtime's `NOTICES.txt`; the upstream copy is available at
https://github.com/nodejs/node/blob/v22.23.2/LICENSE.

## Claude Agent SDK for Python

The backend depends on the unmodified `claude-agent-sdk` Python distribution,
pinned to **0.2.176** in `backend/requirements.txt`. The SDK is authored and
copyrighted by Anthropic, PBC and is distributed under the **MIT License**.
Its installed distribution contains `claude_agent_sdk-0.2.176.dist-info/licenses/LICENSE`;
the upstream source and license are available at
[anthropics/claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python).
The SDK package bundles the separate Claude Code CLI binary; that binary is
Anthropic software, is not licensed by the SDK's MIT license, and is governed by
Anthropic's applicable terms. The pinned CLI artifact and runtime details are
documented in the [Claude Agent guide](./docs/claude-agent.md).

The OpenCuria Claude transport and harness integration are independently written
project code. No SDK source files are vendored into this repository.

### Upstream SDK license text (MIT)

```text
MIT License

Copyright (c) 2025 Anthropic, PBC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## material-icon-theme (VS Code file/folder icons)

The webapp bundles a small curated subset of colored file/folder SVGs
(~147 icons under `webapp/src/assets/file-icons/`, resolved by the shared
`packages/chat-core/src/fileIcons.ts` and rendered by
`webapp/src/components/files/WorkspaceFileIcon.vue` and the mobile
`WorkspaceFileIcon`) copied from:

- **Project:** Material Icon Theme (`material-icon-theme` on npm —
  Material Design Icons for Visual Studio Code)
- **Authors / copyright holders:** Material Extensions
- **License of the upstream work:** MIT License
  (`Copyright (c) 2025 Material Extensions` — full text in
  `webapp/node_modules/material-icon-theme/LICENSE`, installed as the
  `material-icon-theme` npm dependency pinned in `webapp/package.json`)
- **Upstream URL:** https://github.com/material-extensions/vscode-material-icon-theme
- **Pinned version vendored from:** `5.38.1`

### Nature of the use

- Only individual `icons/*.svg` files are copied verbatim into
  `webapp/src/assets/file-icons/`; the upstream `material-icons.json`
  manifest, theme engine, and VS Code extension code are **not** bundled.
- The copied SVGs keep their upstream fills/paths unmodified; the mapping
  from file names / extensions / folder names to icon files is a small
  hand-written subset in `webapp/src/lib/fileIcons.ts` (not the upstream
  manifest).
- The npm package is a regular runtime dependency so the copy source and
  license travel with the install (`webapp/package.json`,
  `webapp/package-lock.json`, `webapp/node_modules/material-icon-theme/LICENSE`).

### Upstream license text (MIT)

```text
The MIT License (MIT)
Copyright (c) 2025 Material Extensions

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

## Mobile application (Expo / React Native)

The native client is under `mobile/`; the root `package-lock.json` pins the npm workspace dependency tree. It uses the following direct UI/runtime components (versions are pinned in `mobile/package.json` and `package-lock.json`):

- **Expo SDK 57.0.30 and Expo modules**, **React 19.2.3**, **React Native 0.86.3**, `react-native-*` support modules, `@legendapp/list` 3.3.3, `react-native-enriched-markdown` 1.0.1 and `marked` 17.0.3 are MIT-licensed upstream components. Their license files are distributed in their respective npm packages.
- **`remend` 1.3.0** (Vercel, [upstream](https://github.com/vercel/streamdown)): Apache-2.0.
- **`lucide-react-native` 1.58.0** ([upstream](https://github.com/lucide-icons/lucide)): ISC. Its upstream license also identifies the Feather-derived icons and their MIT copyright notice; see the installed package's `LICENSE`.
- **`socket.io-client` 4.8.3**: MIT.
- `@opencuria/client` and `@opencuria/chat-core` are in-repository OpenCuria packages licensed AGPL-3.0-only, like the surrounding project.

The native Markdown package has additional bundled components:

- **MD4C** (MIT, Martin Mitáš; [upstream](https://github.com/mity/md4c)) and **Tree-sitter** (MIT, Max Brunsfeld; [upstream](https://github.com/tree-sitter/tree-sitter)). The curated Tree-sitter grammar sources included by `react-native-enriched-markdown` have individual MIT license files in `cpp/highlight/vendor/grammars/*/LICENSE` inside that package. Unicode/ICU headers used by Tree-sitter are under the Unicode/ICU notices in `cpp/highlight/vendor/tree-sitter/src/unicode/LICENSE` (Unicode, Inc. / ICU).
- **RaTeX**, native LaTeX math dependency ([upstream](https://github.com/erweixin/RaTeX)): MIT. The Android build resolves `io.github.erweixin:ratex-android` 0.1.10; the package's iOS vendor source and prebuilt XCFramework are pinned to RaTeX v0.1.14 in `react-native-enriched-markdown`.
- **KaTeX mathematical fonts** (`KaTeX_*.ttf`, including in the RaTeX native artifacts): SIL Open Font License 1.1, **not** MIT. They originate from [KaTeX](https://github.com/KaTeX/KaTeX), are distributed by RaTeX without modification, and retain their separate font license. Copyright/source details are in [RaTeX's font notice](https://github.com/erweixin/RaTeX/blob/v0.1.14/crates/ratex-katex-fonts/fonts/FONT_NOTICE.txt); the complete OFL text is [`SIL-OFL-1.1.txt`](https://github.com/erweixin/RaTeX/blob/v0.1.14/licenses/SIL-OFL-1.1.txt). RaTeX's [third-party notice](https://github.com/erweixin/RaTeX/blob/v0.1.14/THIRD_PARTY_NOTICES.txt) specifically asks redistributors of binaries/SDKs that contain these fonts to retain the notice and OFL attribution.

The full license texts for npm components are in their installed package `LICENSE` files (the lockfile identifies the exact package versions); transitive native component notices are in the upstream package/source locations linked above. When redistributing a mobile binary or SDK, include the applicable license texts and notices with the distribution, including the separate OFL font notice/license. No native store/release binary or app-level license bundle is produced by this documentation change.

### KaTeX font notice and SIL Open Font License 1.1

The following TrueType fonts are from [KaTeX](https://github.com/KaTeX/KaTeX):
`KaTeX_*.ttf`. They are licensed under the SIL Open Font License, Version 1.1;
KaTeX's MIT software license does not apply to these font binaries. The fonts
are distributed unmodified by RaTeX. This notice and the complete font license
are included here because native Markdown/LaTeX renderers may bundle them into
mobile application artifacts.

```text
KaTeX font files (KaTeX_*.ttf)
================================

These TrueType fonts are taken from the KaTeX project:
  https://github.com/KaTeX/KaTeX

These unmodified font binaries are not licensed under the MIT License in the
KaTeX/RaTeX software repositories. They are licensed under the SIL Open Font
License, Version 1.1.

Copyright (c) 2009-2010, Design Science, Inc. (<www.mathjax.org>)
Copyright (c) 2014-2018 Khan Academy (<www.khanacademy.org>),
with Reserved Font Name KaTeX_Main. Each font face declares its own Reserved
Font Name (for example, KaTeX_Math, KaTeX_AMS and KaTeX_Caligraphic).

This Font Software is licensed under the SIL Open Font License, Version 1.1.
Full license text:

SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```

## Mobile workspace tools (t3code, Ghostty, Pierre Highlights)

The mobile Git/Terminal/Files sheet adapts code from **t3code**
([pingdotgg/t3code](https://github.com/pingdotgg/t3code), MIT License,
Copyright (c) 2026 T3 Tools Inc.): the native terminal surface
(`mobile/modules/opencuria-terminal/`), the native syntax highlighter
(`mobile/modules/opencuria-highlights/`) and parts of the terminal and file
browser UI under `mobile/src/tools/`. Each module ships the t3code MIT
license as `LICENSE`; adapted source files carry an attribution header.

Vendored components (details and pinned revisions in each module's
`THIRD_PARTY_NOTICES.md` / `NOTICE.md`):

- **Ghostty / libghostty** (`GhosttyKit.xcframework`, iOS) and
  **libghostty-vt** (Android shared libraries and C headers),
  [ghostty-org/ghostty](https://github.com/ghostty-org/ghostty): MIT.
- **MesloLGS NF** terminal font (Android): Apache-2.0.
- **@pierre/highlights** 0.2.0 lexers compiled to C with wasm2c: Apache-2.0.
- **wasm2c runtime** ([WebAssembly/wabt](https://github.com/WebAssembly/wabt)):
  Apache-2.0 (text in `mobile/modules/opencuria-highlights/Vendor/wasm2c/LICENSE`).
- **SIMDe** ([simd-everywhere/simde](https://github.com/simd-everywhere/simde)): MIT.
- **jsdiff** (`diff` 9.0.0, [kpdecker/jsdiff](https://github.com/kpdecker/jsdiff)):
  BSD-3-Clause, used for Git diffs on mobile.

The mobile file icons are the material-icon-theme SVGs listed above,
inlined by `mobile/scripts/generate-file-icons.mjs` into
`mobile/src/tools/files/fileIconSvgs.generated.ts`.

## OpenAI Codex

OpenCuria integrates the unmodified official **Codex 0.166.0** standalone
app-server package, released by **OpenAI**, **Copyright 2025 OpenAI**, under the
**Apache License, Version 2.0**. Source and release:
https://github.com/openai/codex/releases/tag/rust-v0.166.0.

- Full upstream license: [codex-0.166.0-LICENSE.txt](docs/licenses/codex-0.166.0-LICENSE.txt).
- Upstream attribution notice: [codex-0.166.0-NOTICE.txt](docs/licenses/codex-0.166.0-NOTICE.txt).
- The notice identifies code derived from **Ratatui** (MIT): Copyright (c)
  2016–2022 Florian Dehau; Copyright (c) 2023–2025 The Ratatui Developers.

`backend/apps/harness/engines/codex/models.json` records capability metadata
extracted from the pinned runtime's unauthenticated `model/list` response. This
metadata is attributed to OpenAI Codex under the same upstream license; it is not
a live subscription-entitlement catalog. The integration/transport code is
independently written OpenCuria code, not vendored Codex source. Runtime release
bytes are downloaded on demand, not bundled in the repository.

Redistributors of the downloaded package or images containing it must retain the
applicable license texts and notices, including upstream NOTICE, Ratatui's MIT
notice/license and all separate helper/dependency licensing. The package includes
`codex-app-server`, `codex-code-mode-host`, `codex-exec-server`,
`codex-path/rg` (ripgrep) and
`codex-resources/bwrap` (bubblewrap); Codex's Apache license alone is not a license
for every dependency/helper. Consult and preserve the release and corresponding
source dependency notices/licenses (including ripgrep's MIT/Unlicense and
bubblewrap's LGPL-2.0-or-later obligations, with corresponding source as required).
The two upstream Codex files copied here are not a complete third-party license
bundle for a redistributed runtime image. See the [Codex guide](docs/codex.md)
for exact pins and runtime boundaries. OpenCuria's AGPL-3.0-only license is unchanged.

## pypdf

The backend requires the unmodified **pypdf 6.21.1** distribution for isolated,
bounded PDF text extraction. Upstream: https://github.com/py-pdf/pypdf. It uses
the **BSD 3-Clause License**, with copyright notices for Mathieu Fenniak, Ashish
Kulkarni and Steve Witham. The full license, copied from the installed pinned
distribution's `pypdf-6.21.1.dist-info/licenses/LICENSE`, is retained in
[pypdf-6.21.1-LICENSE.txt](docs/licenses/pypdf-6.21.1-LICENSE.txt). Retain it when
redistributing source or binaries as required by that license. No pypdf source
is vendored; the dependency does not change OpenCuria's project license.
