"""Default action materializer (pure command construction + ports).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

Every ``OSWorldACI`` action is reproduced:

- ``click``/``drag_and_drop``/``scroll`` ground descriptions via the
  completion port (temperature 0) and scale with the exact
  ``round(coord * actual / grounding_dimension)`` formula; ``hold_keys`` use
  ``repr(k)`` quoting (``pyautogui.keyDown('x')``); ``click`` emits the
  doubled ``"import pyautogui; "`` prefix quirk (once from ``command``, once
  from the f-string);
- ``switch_applications`` uses the verbatim per-platform snippets
  (``UBUNTU_APP_SETUP`` with ``APP_NAME`` substitution on Linux);
- ``type`` keeps the pyperclip-bootstrap preamble, click-to-focus, overwrite,
  unicode-clipboard-vs-``write`` branch and enter handling, with
  ``repr(...)`` quoting throughout;
- ``save_to_knowledge`` extends the notes buffer and returns ``"WAIT"``;
- ``highlight_text_span`` grounds via OCR (no resize) and formats drag code
  with the verbatim ``button='{button}'`` interpolation;
- ``set_cell_values`` renders ``SET_CELL_VALUES_CMD`` via ``str.format``
  (values use Python ``str()`` of the dict; verbatim upstream starts
  ``soffice --accept=...`` via blocking ``subprocess.run`` — known
  upstream semantics, bounded by the 120s runner execute timeout);
- ``call_code_agent`` runs the code-agent state machine through the injected
  ports (``task or current_task_instruction``; ``"import time;
  time.sleep(2.222)"`` on success, ``time.sleep(1.111)`` without a task);
- ``hotkey``/``hold_and_press``/``wait``/``done``/``fail`` reproduce the
  exact string building (``hotkey`` single-quotes keys manually;
  ``hold_and_press`` emits ``pyautogui.press(['a', 'b'])`` list form).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import code_agent as _code_agent
from . import grounding as _grounding
from .actions_parser import ParsedAction
from .ports import (
    CodeExecutionPort,
    CompletionPort,
    MaterializationResult,
    OcrPort,
)


def _bind_defaults(action: ParsedAction) -> dict[str, Any]:
    """Fill omitted arguments from ``AgentActionSurface`` defaults.

    Mirrors the ``eval`` path in Agent-S, where Python applies default
    parameter values at call time. Raises ``TypeError`` on arity mismatch
    (surfaced as ``CODE_VALID`` feedback, then the ``wait(1.333)`` fallback).
    """
    import inspect as _inspect

    from .actions import AgentActionSurface

    sig = _inspect.signature(getattr(AgentActionSurface, action.method))
    bound = sig.bind(None, *action.args, **action.kwargs)
    bound.apply_defaults()
    params = list(sig.parameters)[1:]  # drop ``self``
    return {name: bound.arguments[name] for name in params}


TYPE_COMMAND_PREAMBLE = (
    "import pyautogui; "
    "\ntry:\n"
    "    import pyperclip\n"
    "except ImportError:\n"
    "    import subprocess\n"
    "    subprocess.run("
    "'echo \"osworld-public-evaluation\" | sudo -S apt-get install -y xclip xsel', "
    "shell=True, check=True)\n"
    "    subprocess.check_call("
    "[subprocess.sys.executable, '-m', 'pip', 'install', 'pyperclip'])\n"
    "    import pyperclip\n\n"
)


@dataclass
class MaterializerConfig:
    platform: str = "linux"
    width: int = 1920
    height: int = 1080
    grounding_width: int = 1920
    grounding_height: int = 1080
    code_agent_budget: int = 20


@dataclass
class DefaultActionMaterializer:
    """Stateful materializer: notes, code-agent result, last task."""

    completion: CompletionPort
    ocr: OcrPort | None = None
    code_execution: CodeExecutionPort | None = None
    config: MaterializerConfig = field(default_factory=MaterializerConfig)
    notes: list[str] = field(default_factory=list)
    current_task_instruction: str | None = None
    last_code_agent_result: dict[str, Any] | None = None
    screenshot: dict[str, Any] | None = field(default=None, repr=False)
    usage_sink: Any = field(default=None, repr=False)

    def _record_usage(self, purpose, usage) -> None:
        if self.usage_sink is not None:
            self.usage_sink(purpose, usage)

    @property
    def _last_usages(self) -> list:
        # Backwards-compat shim: usages now flow via ``usage_sink``.
        if not hasattr(self, "_usages_buf"):
            self._usages_buf: list = []
        return self._usages_buf

    async def materialize(
        self, action: ParsedAction, obs: dict[str, Any]
    ) -> MaterializationResult:
        self.screenshot = obs
        handler = getattr(self, f"_materialize_{action.method}", None)
        if handler is None:
            raise ValueError(f"unknown agent action: {action.method}")
        return await handler(_bind_defaults(action), obs)

    # -- grounding-backed actions -------------------------------------------
    async def _ground_and_resize(self, ref_expr: str, obs: dict[str, Any]) -> list[int]:
        coords, _ = await _grounding.generate_coords(
            self.completion,
            obs["screenshot"],
            ref_expr,
            record_usage=self._record_usage,
        )
        return _grounding.resize_coordinates(
            coords,
            width=self.config.width,
            height=self.config.height,
            grounding_width=self.config.grounding_width,
            grounding_height=self.config.grounding_height,
        )

    async def _materialize_click(self, args: dict[str, Any], obs: dict[str, Any]):
        x, y = await self._ground_and_resize(args["element_description"], obs)
        num_clicks = args["num_clicks"]
        button_type = args["button_type"]
        hold_keys = args["hold_keys"]
        command = "import pyautogui; "
        for k in hold_keys:
            command += f"pyautogui.keyDown({k!r}); "
        command += (
            "import pyautogui; "
            f"pyautogui.click({x}, {y}, clicks={num_clicks}, "
            f"button={button_type!r}); "
        )
        for k in hold_keys:
            command += f"pyautogui.keyUp({k!r}); "
        return MaterializationResult(exec_code=command)

    async def _materialize_drag_and_drop(
        self, args: dict[str, Any], obs: dict[str, Any]
    ):
        coords1, _ = await _grounding.generate_coords(
            self.completion,
            obs["screenshot"],
            args["starting_description"],
            record_usage=self._record_usage,
        )
        coords2, _ = await _grounding.generate_coords(
            self.completion,
            obs["screenshot"],
            args["ending_description"],
            record_usage=self._record_usage,
        )
        x1, y1 = _grounding.resize_coordinates(
            coords1,
            width=self.config.width,
            height=self.config.height,
            grounding_width=self.config.grounding_width,
            grounding_height=self.config.grounding_height,
        )
        x2, y2 = _grounding.resize_coordinates(
            coords2,
            width=self.config.width,
            height=self.config.height,
            grounding_width=self.config.grounding_width,
            grounding_height=self.config.grounding_height,
        )
        hold_keys = args["hold_keys"]
        command = "import pyautogui; "
        command += f"pyautogui.moveTo({x1}, {y1}); "
        for k in hold_keys:
            command += f"pyautogui.keyDown({k!r}); "
        command += (
            f"pyautogui.dragTo({x2}, {y2}, duration=1., button='left'); "
            "pyautogui.mouseUp(); "
        )
        for k in hold_keys:
            command += f"pyautogui.keyUp({k!r}); "
        return MaterializationResult(exec_code=command)

    async def _materialize_scroll(self, args: dict[str, Any], obs: dict[str, Any]):
        x, y = await self._ground_and_resize(args["element_description"], obs)
        clicks = args["clicks"]
        base = (
            "import pyautogui; import time; "
            f"pyautogui.moveTo({x}, {y}); time.sleep(0.5); "
        )
        if args["shift"]:
            suffix = f"pyautogui.hscroll({clicks})"
        else:
            suffix = f"pyautogui.vscroll({clicks})"
        return MaterializationResult(exec_code=base + suffix)

    # -- platform snippets ----------------------------------------------------
    async def _materialize_switch_applications(
        self, args: dict[str, Any], obs: dict[str, Any]
    ):
        from . import prompts as _prompts

        app_code = args["app_code"]
        if self.config.platform == "darwin":
            return MaterializationResult(
                exec_code=(
                    "import pyautogui; import time; "
                    "pyautogui.hotkey('command', 'space', interval=0.5); "
                    f"pyautogui.typewrite({app_code!r}); "
                    "pyautogui.press('enter'); time.sleep(1.0)"
                )
            )
        if self.config.platform == "linux":
            return MaterializationResult(
                exec_code=_prompts.UBUNTU_APP_SETUP.replace("APP_NAME", app_code)
            )
        if self.config.platform == "windows":
            return MaterializationResult(
                exec_code=(
                    "import pyautogui; import time; "
                    "pyautogui.hotkey('win', 'd', interval=0.5); "
                    f"pyautogui.typewrite({app_code!r}); "
                    "pyautogui.press('enter'); time.sleep(1.0)"
                )
            )
        raise AssertionError(
            f"Unsupported platform: {self.config.platform}. "
            "Supported platforms are: darwin, linux, windows."
        )

    async def _materialize_open(self, args: dict[str, Any], obs: dict[str, Any]):
        app_or_filename = args["app_or_filename"]
        if self.config.platform == "linux":
            return MaterializationResult(
                exec_code=(
                    "import pyautogui; import time; pyautogui.hotkey('win'); "
                    "time.sleep(0.5); "
                    f"pyautogui.write({app_or_filename!r}); time.sleep(1.0); "
                    "pyautogui.hotkey('enter'); time.sleep(0.5)"
                )
            )
        if self.config.platform == "darwin":
            return MaterializationResult(
                exec_code=(
                    "import pyautogui; import time; "
                    "pyautogui.hotkey('command', 'space', interval=0.5); "
                    f"pyautogui.typewrite({app_or_filename!r}); "
                    "pyautogui.press('enter'); time.sleep(1.0)"
                )
            )
        if self.config.platform == "windows":
            return MaterializationResult(
                exec_code=(
                    "import pyautogui; import time; "
                    "pyautogui.hotkey('win'); time.sleep(0.5); "
                    f"pyautogui.write({app_or_filename!r}); time.sleep(1.0); "
                    "pyautogui.press('enter'); time.sleep(0.5)"
                )
            )
        raise AssertionError(
            f"Unsupported platform: {self.config.platform}. "
            "Supported platforms are: darwin, linux, windows."
        )

    # -- typing / knowledge -----------------------------------------------------
    async def _materialize_type(self, args: dict[str, Any], obs: dict[str, Any]):
        command = TYPE_COMMAND_PREAMBLE
        if args["element_description"] is not None:
            x, y = await self._ground_and_resize(args["element_description"], obs)
            command += f"pyautogui.click({x}, {y}); "
        if args["overwrite"]:
            ctrl = "command" if self.config.platform == "darwin" else "ctrl"
            command += (
                f"pyautogui.hotkey({ctrl!r}, 'a'); pyautogui.press('backspace'); "
            )
        text = args["text"]
        has_unicode = any(ord(char) > 127 for char in text)
        if has_unicode:
            command += f"pyperclip.copy({text!r}); "
            ctrl = "command" if self.config.platform == "darwin" else "ctrl"
            command += f"pyautogui.hotkey({ctrl!r}, 'v'); "
        else:
            command += f"pyautogui.write({text!r}); "
        if args["enter"]:
            command += "pyautogui.press('enter'); "
        return MaterializationResult(exec_code=command)

    async def _materialize_save_to_knowledge(
        self, args: dict[str, Any], obs: dict[str, Any]
    ):
        self.notes.extend(args["text"])
        return MaterializationResult(exec_code="WAIT", terminal="WAIT")

    async def _materialize_highlight_text_span(
        self, args: dict[str, Any], obs: dict[str, Any]
    ):
        if self.ocr is None:
            raise RuntimeError("highlight_text_span requires an OcrPort")
        rows = await self.ocr.read_words(obs["screenshot"])
        coords1, _, _ = await _grounding.generate_text_coords(
            self.completion,
            rows,
            obs["screenshot"],
            args["starting_phrase"],
            alignment="start",
            record_usage=self._record_usage,
        )
        coords2, _, _ = await _grounding.generate_text_coords(
            self.completion,
            rows,
            obs["screenshot"],
            args["ending_phrase"],
            alignment="end",
            record_usage=self._record_usage,
        )
        x1, y1 = coords1
        x2, y2 = coords2
        button = args["button"]
        command = "import pyautogui; "
        command += f"pyautogui.moveTo({x1}, {y1}); "
        command += (
            f"pyautogui.dragTo({x2}, {y2}, duration=1., button='{button}'); "
            "pyautogui.mouseUp(); "
        )
        return MaterializationResult(exec_code=command)

    async def _materialize_set_cell_values(
        self, args: dict[str, Any], obs: dict[str, Any]
    ):
        from . import prompts as _prompts

        return MaterializationResult(
            exec_code=_prompts.SET_CELL_VALUES_CMD.format(
                cell_values=args["cell_values"],
                app_name=args["app_name"],
                sheet_name=args["sheet_name"],
            )
        )

    # -- code agent ---------------------------------------------------------------
    async def _materialize_call_code_agent(
        self, args: dict[str, Any], obs: dict[str, Any]
    ):
        task = args.get("task")
        task_to_execute = task if task is not None else self.current_task_instruction
        if task_to_execute:
            if self.code_execution is None:
                raise RuntimeError("call_code_agent requires a CodeExecutionPort")
            screenshot = obs.get("screenshot", "") if obs else ""
            result, _ = await _code_agent.run_code_agent(
                self.completion,
                self.code_execution,
                task_to_execute,
                screenshot if isinstance(screenshot, (bytes, bytearray)) else b"",
                budget=self.config.code_agent_budget,
                record_usage=self._record_usage,
            )
            self.last_code_agent_result = result.as_dict()
            return MaterializationResult(
                exec_code="import time; time.sleep(2.222)",
                code_agent_result=self.last_code_agent_result,
            )
        return MaterializationResult(exec_code="import time; time.sleep(1.111)")

    # -- trivial actions ------------------------------------------------------------
    async def _materialize_hotkey(self, args: dict[str, Any], obs: dict[str, Any]):
        keys = [f"'{key}'" for key in args["keys"]]
        return MaterializationResult(
            exec_code=f"import pyautogui; pyautogui.hotkey({', '.join(keys)})"
        )

    async def _materialize_hold_and_press(
        self, args: dict[str, Any], obs: dict[str, Any]
    ):
        press_keys_str = "[" + ", ".join(f"'{key}'" for key in args["press_keys"]) + "]"
        command = "import pyautogui; "
        for k in args["hold_keys"]:
            command += f"pyautogui.keyDown({k!r}); "
        command += f"pyautogui.press({press_keys_str}); "
        for k in args["hold_keys"]:
            command += f"pyautogui.keyUp({k!r}); "
        return MaterializationResult(exec_code=command)

    async def _materialize_wait(self, args: dict[str, Any], obs: dict[str, Any]):
        return MaterializationResult(
            exec_code=f"""import time; time.sleep({args["time"]})"""
        )

    async def _materialize_done(self, args: dict[str, Any], obs: dict[str, Any]):
        return MaterializationResult(exec_code="DONE", terminal="DONE")

    async def _materialize_fail(self, args: dict[str, Any], obs: dict[str, Any]):
        return MaterializationResult(exec_code="FAIL", terminal="FAIL")
