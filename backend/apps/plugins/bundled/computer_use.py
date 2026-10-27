"""Stable identity of the workspace Computer Use integration."""

import uuid

PLUGIN_ID = uuid.UUID("7ca41130-a36a-491d-83fa-cd912bcaaf52")
PLUGIN_SLUG = "computer-use"
SERVER_SLUG = "cua_repl"
ARTIFACT_ID = "computer-use"
VERSION = "0.1.0"
TOOL_POLICY = {
    "exposed_tools": ["js", "js_reset"],
    "internal_tools": ["turn_ended"],
    "desktop_tools": ["js"],
    "execution_tools": ["js", "js_reset"],
    "on_run_end": "turn_ended",
    "desktop_kind": "computeruse",
    "exclusive_input": True,
}
