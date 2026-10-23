# Harness prompt ownership

All three engines use the product context in
[`prompts/opencuria.py`](../backend/apps/harness/prompts/opencuria.py).
It describes the isolated workspace, the boundary with external services,
routine local setup subject to the current task/mode/permissions, and chat
file/media rendering. It does not define a coding persona, planning policy,
tool usage, delegation, or project conventions.

| Engine | Coding instructions | OpenCuria product context | Project context and skills |
| --- | --- | --- | --- |
| OpenCuria | `agents/definitions.py` and `prompts/composer.py` | Included once by the composer | Composer loads project files and renders selected/plugin skill bodies |
| Codex | Native Codex base instructions | `developerInstructions` on start/resume | Native discovery; selected skills are materialized as native files |
| Claude | Native `claude_code` preset | `--append-system-prompt-file` on start/resume | CLI context loading; selected skills use the managed native plugin |

Explore receives workspace/chat facts without local setup guidance. Hidden
title, summary, and compaction helpers receive no product context. The product
context does not grant tools or bypass permissions. Role prompts, tool schemas,
skills, and project files must not be copied into the external engines' extension.

## Chat files and media

Images and videos use Markdown image syntax, including for video recordings:

```markdown
![Screenshot](/workspace/screenshots/result.png)
![Recording](/workspace/recordings/demo.mp4)
[Report](/workspace/reports/report.pdf)
```

Relative chat paths resolve from `/workspace`, not the shell's current directory.
Absolute workspace paths avoid ambiguity when work happens in a subdirectory.
Only supported images/videos are promised inline rendering; other files use links.

## Separate runtime integration boundaries

Sharing product context does not change native mode, configuration, or permission
contracts. These existing boundaries need separate changes and contract tests:

- Claude currently disables user/project setting sources. A native preset alone
  does not enable project `CLAUDE.md` loading.
- Claude's scoped SDK child definitions use explicit `AgentDefinition.prompt`
  values. They receive product context, but this is not an additive extension of
  upstream subagent role prompts. Restoring upstream roles must preserve child
  permission hooks, tool scopes, and event identity.
- Codex currently maps Plan restrictions through sandbox/tool configuration;
  this does not select a native collaboration-mode preset. Native mode integration
  belongs in the engine adapter, not the shared prompt.

Tests capture requests from the pinned CLIs against local scripted providers to
check native root prompt preservation, a single product extension on fresh and
resumed turns, and scoped Claude child context. No live provider is required.
