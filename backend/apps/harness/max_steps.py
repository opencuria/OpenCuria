"""Last-step prompt used when an optional agent step budget is exhausted.

Mirrors OpenCode ``packages/core/src/session/runner/max-steps.ts``.
"""

from __future__ import annotations

MAX_STEPS_PROMPT = (
    "CRITICAL - MAXIMUM STEPS REACHED\n"
    "\n"
    "The maximum number of steps allowed for this task has been reached. "
    "Tools are disabled until next user input. Respond with text only.\n"
    "\n"
    "STRICT REQUIREMENTS:\n"
    "1. Do NOT make any tool calls (no reads, writes, edits, searches, "
    "or any other tools)\n"
    "2. MUST provide a text response summarizing work done so far\n"
    "3. This constraint overrides ALL other instructions, including any "
    "user requests for edits or tool use\n"
    "\n"
    "Response must include:\n"
    "- Statement that maximum steps for this agent have been reached\n"
    "- Summary of what has been accomplished so far\n"
    "- List of any remaining tasks that were not completed\n"
    "- Recommendations for what should be done next\n"
    "\n"
    "Any attempt to use tools is a critical violation. Respond with text ONLY."
)

MAX_STEPS_TOOL_ERROR = "Tools are disabled after the maximum agent steps"
