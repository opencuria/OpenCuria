# Third-Party Notices

This repository is licensed under the **GNU Affero General Public License
v3.0 only (AGPL-3.0-only)** — see `LICENSE` at the repository root. That
license continues to apply to the repository as a whole, including to the
files listed below. Nothing in this notice replaces or weakens the AGPL-3.0-only
license of the surrounding OpenCuria project.

## Agent-S (S3 computer-use core)

The isolated computer-use core under `backend/apps/harness/agent_s/` (and its
tests under `backend/apps/harness/tests/test_agent_s_*.py` plus the shared
fakes in `backend/apps/harness/tests/agent_s_conftest.py`) is adapted from:

- **Project:** Agent-S (GUI computer-use agents, `gui_agents/s3` — S3 layer)
- **Authors / copyright holders:** Simular AI and Agent-S contributors
- **License of the upstream work:** Apache License, Version 2.0
  (`http://www.apache.org/licenses/LICENSE-2.0`)
- **Upstream URL:** https://github.com/simular-ai/Agent-S
- **Pinned commit reviewed and ported:**
  `3aa272d23d2994c7bbde1acbbe0ef8e8d06b8693`
  (merge of PR #215; the working tree matches this commit exactly)

### Affected package paths (this repository)

- `backend/apps/harness/agent_s/__init__.py`
- `backend/apps/harness/agent_s/actions.py`
- `backend/apps/harness/agent_s/actions_parser.py`
- `backend/apps/harness/agent_s/adapters.py`
- `backend/apps/harness/agent_s/code_agent.py`
- `backend/apps/harness/agent_s/config.py`
- `backend/apps/harness/agent_s/formatting.py`
- `backend/apps/harness/agent_s/grounding.py`
- `backend/apps/harness/agent_s/harness.py`
- `backend/apps/harness/agent_s/llm.py`
- `backend/apps/harness/agent_s/materializer.py`
- `backend/apps/harness/agent_s/messages.py`
- `backend/apps/harness/agent_s/parsing.py`
- `backend/apps/harness/agent_s/ports.py`
- `backend/apps/harness/agent_s/prompts.py`
- `backend/apps/harness/agent_s/session.py`
- `backend/apps/harness/agent_s/worker.py`
- `backend/apps/harness/agent_s/worker_prompt.py`
- `backend/apps/harness/tests/agent_s_conftest.py`
- `backend/apps/harness/tests/test_agent_s_adapters.py`
- `backend/apps/harness/tests/test_agent_s_config.py`
- `backend/apps/harness/tests/test_agent_s_harness.py`
- `backend/apps/harness/tests/test_agent_s_llm_grounding_code.py`
- `backend/apps/harness/tests/test_agent_s_parsing.py`
- `backend/apps/harness/tests/test_agent_s_prompts.py`
- `backend/apps/harness/tests/test_agent_s_runtime.py`
- `backend/apps/harness/tests/test_agent_s_worker.py`

### Nature of the modifications

- **Async port:** the synchronous Agent-S call chain
  (`call_llm_safe`/`call_llm_formatted`, grounding, OCR, code execution,
  action materialization, worker steps) is expressed through small injected
  async ports (`CompletionPort`, `OcrPort`, `CodeExecutionPort`,
  `ActionMaterializer`) so the core runs without network, OCR binaries,
  Django/ORM, Socket.IO, or local subprocesses.
- **No `eval`/`exec`:** Agent-S materializes the planner's
  `agent.method(...)` line with `eval(code)`; this port parses the same line
  with a strict `ast`-based parser that accepts only literal arguments for
  the 15 known action methods.
- **Verbatim AI-visible texts preserved:** the worker prompt head/tail, all
  15 action signatures/docstrings, the `UBUNTU_APP_SETUP` and
  `SET_CELL_VALUES_CMD` snippets, the code-agent prompts, the formatting
  feedback template, and the reflection prompt are byte-identical to the
  pinned upstream commit (verified by golden hash tests), including
  upstream typos/quirks (e.g. "alxl the text", "your believe the task").
- **Executed quirks preserved:** double materialization during the
  `CODE_VALID` format check, `steps_executed` excluding the terminal
  DONE/FAIL turn, the grounding default system prompt
  (`"You are a helpful assistant."`), `time.sleep(1.0)` after every failed
  LLM/format attempt (including the final one; never after success), the
  non-long-context flush popping index 1 twice, and the OCR cleaning /
  table / coordinate rules are all pinned by tests.
- Each adapted file carries a header comment naming the upstream project,
  URL, pinned commit, and the Apache-2.0 license, and points back to this
  notice.

### Upstream license text (Apache-2.0)

The full text of the Apache License, Version 2.0 — under which Simular AI
and the Agent-S contributors published the upstream work — is available at
`http://www.apache.org/licenses/LICENSE-2.0`. A copy may also be found in
the upstream repository (https://github.com/simular-ai/Agent-S).
