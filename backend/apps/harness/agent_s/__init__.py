"""Isolated async Agent-S3 core (first layer, no harness wiring).

This package ports the AI-visible Agent-S3 behaviour (simular-ai/Agent-S,
commit 3aa272d, Apache-2.0; see THIRD_PARTY_NOTICES.md) into a testable,
async-capable core with explicit ports. It is deliberately *not* wired into
``HarnessService``/runner/API/UI yet.

Layout (separation of concerns):

- :mod:`apps.harness.agent_s.prompts` — verbatim prompt constants.
- :mod:`apps.harness.agent_s.actions` — exact AI-visible action interface
  (signatures/docstrings only; bodies raise ``NotImplementedError``).
- :mod:`apps.harness.agent_s.worker_prompt` — ``dir()``-based prompt builder.
- :mod:`apps.harness.agent_s.messages` — Agent-S wire-message helpers.
- :mod:`apps.harness.agent_s.parsing` — ``parse_code_from_string``,
  ``extract_agent_functions`` and ``split_thinking_response`` quirks.
- :mod:`apps.harness.agent_s.actions_parser` — safe ``ast`` parser for the
  single ``agent.method(...)`` line (no ``eval``/``exec``).
- :mod:`apps.harness.agent_s.ports` — ``CompletionPort``, ``Usage``,
  ``OcrPort``, ``CodeExecutionPort``, ``ActionMaterializer`` protocols.
- :mod:`apps.harness.agent_s.formatting` — ``SINGLE_ACTION``/``CODE_VALID``
  format checkers (CODE_VALID double-materialization quirk preserved).
- :mod:`apps.harness.agent_s.llm` — ``call_llm_safe`` / ``call_llm_formatted``
  retry semantics (async, injectable sleep).
- :mod:`apps.harness.agent_s.grounding` — grounding prompts, text-last
  messages, OCR table/coordinate logic, ``resize_coordinates`` scaling.
- :mod:`apps.harness.agent_s.code_agent` — code-agent state machine
  (20-step budget, DONE/FAIL/BUDGET quirks, summary call).
- :mod:`apps.harness.agent_s.materializer` — default ``ActionMaterializer``
  implementing every action via the completion/OCR/code ports.
- :mod:`apps.harness.agent_s.worker` — full Agent-S step orchestration and
  typed step output.
- :mod:`apps.harness.agent_s.session` — multi-turn session wrapper
  (``AgentS3``/``Worker`` equivalent: reset, turn counter, history, flush).

No Django/ORM, Socket.IO, service or runner imports are allowed in this
package; tests run without network or database access.
"""

from __future__ import annotations
