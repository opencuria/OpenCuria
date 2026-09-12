"""OpenCuria ports for the Agent-S domain core (second layer).

Adapted in part from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

This module is the *integration* side of the Agent-S port: it adapts the
harness provider/accessor world to the domain ports declared in
:mod:`apps.harness.agent_s.ports` (``CompletionPort``,
``CodeExecutionPort``, ``OcrPort``). No Django/ORM imports are allowed
here; only harness value types (``LLMMessage``/``Usage``/``ChatOptions``)
and the :class:`WorkspaceAccessor
<apps.harness.access.base.WorkspaceAccessor>` interface are used.

Layout:

- :class:`HarnessCompletionAdapter` — sends the exact Agent-S wire
  messages with ``tools=[]``, aggregates provider ``text``/``reasoning``
  deltas, and maps usage. Routing by purpose: worker/reflection/
  text_span/code/code_summary go to the main model, grounding to the
  separately configurable grounding model. ``temperature``/``use_thinking``
  come verbatim from the core; ``ChatOptions`` timeouts are preserved and
  ``reasoning_effort`` is only attached to ChatGPT/Responses-style
  resolutions (where it cannot corrupt Agent-S temperature/thinking
  semantics). With ``use_thinking=True`` the Agent-S
  ``<thoughts>…</thoughts><answer>…</answer>`` envelope is reconstructed
  from provider ``reasoning`` + ``text``.
- :class:`WorkspaceCodeExecutionAdapter` — runs code-agent code
  exclusively in the isolated workspace via ``exec_wait`` (bash:
  ``bash -lc``/timeout 30/stdout+stderr like
  ``LocalController.run_bash_script``; python: ``python3 -c`` with split
  stdout/stderr like ``run_python_script``). Workdir is ``/workspace``.
- :class:`WorkspaceActionExecutor` — runs the materialized PyAutoGUI
  snippet (and only that — never the raw LLM ``plan_code``) remotely via
  the generic ``desktop_action("execute", {"code": ...})`` runner RPC
  (``python3 -c`` with the desktop env; backend never executes locally).
  Any failure visibly fails the run. The RPC validates nonempty code,
  caps the payload at ``ACTION_EXECUTE_MAX_CHARS``, and reports
  exit/stdout/stderr.
- :class:`WorkspaceOcrAdapter` — writes the current PNG into a run-scoped
  ``.opencuria/computeruse/<run_id>/…`` path, runs a generic
  ``tesseract … tsv`` in the workspace, and parses word-level TSV rows.
  The exact Agent-S OCR cleaning stays in the core.
- :class:`DesktopScreenshotAdapter` — per-turn PNG capture through the
  generic ``desktop_action("screenshot", …)`` RPC with proportional
  downscaling to the configured max dimension (Agent-S CLI 2400-rule).
  The runner stays generic (``format``/``max_dimension``); the 2400-rule
  itself lives here.

Secrets are never logged: commands and payloads only appear as lengths or
redacted markers in the logs below.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Max bytes accepted for a remote ``desktop_action("execute")`` payload.
#: Guards the runner RPC without constraining legitimate materialized
#: snippets (typical actions are a few hundred bytes).
ACTION_EXECUTE_MAX_CHARS = 200_000

#: Explicit ``desktop_action("execute")`` timeout (seconds). The runner caps
#: one remote snippet at ``DESKTOP_EXECUTE_TIMEOUT_S`` (120s); pin the same
#: value here so the backend ``RunnerWorkspaceAccessor`` default (60s) never
#: aborts first. Documented distributed safety-timeout adaptation.
ACTION_EXECUTE_TIMEOUT_S = 120.0

#: Run-scoped OCR staging directory inside the workspace.
OCR_STAGING_ROOT = "/workspace/.opencuria/computeruse"



def decode_data_url_png(data_url: str) -> bytes:
    """Decode an Agent-S PNG data URL (``messages.encode_image_to_data_url``)."""
    prefix = "data:image/png;base64,"
    payload = data_url[len(prefix) :] if data_url.startswith(prefix) else data_url
    try:
        return base64.b64decode(payload)
    except (ValueError, TypeError) as exc:
        raise ValueError("Agent-S image part is not valid base64 PNG") from exc


def encode_png_data_url(png: bytes) -> str:
    """Encode raw PNG bytes as an Agent-S image data URL."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def extract_png_from_wire(messages: list[dict[str, Any]]) -> bytes:
    """Return the last PNG payload of Agent-S wire messages (for logging)."""
    for message in reversed(messages):
        for part in reversed(message.get("content", []) or []):
            if not isinstance(part, dict):
                continue
            if part.get("type") != "image_url":
                continue
            ref = part.get("image_url")
            url = ref.get("url") if isinstance(ref, dict) else ref
            if isinstance(url, str) and url:
                return decode_data_url_png(url)
    raise ValueError("Agent-S wire messages contain no PNG image part")


class HarnessCompletionAdapter:
    """Adapt Agent-S wire completions onto a harness model resolver.

    ``resolve`` maps a model ref to a ``ResolvedModel``-like
    (``adapter``/``model_id``); ``chat_options_factory`` preserves the
    runner ``ChatOptions`` timeouts per call. No OpenCuria system prompt
    is composed here and no tool schemas are offered: ``tools=[]``.
    """

    def __init__(
        self,
        resolve: Callable[[str], Any],
        *,
        main_model: str,
        grounding_model: str,
        chat_options_factory: Callable[[], Any] | None = None,
        emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        if not (main_model or "").strip():
            raise ValueError("HarnessCompletionAdapter requires main_model")
        grounded = (grounding_model or "").strip() or main_model.strip()
        self._resolve = resolve
        self._main_model = main_model.strip()
        self._grounding_model = grounded
        self._chat_options_factory = chat_options_factory
        self._emit = emit
        self.calls: list[dict[str, Any]] = []

    @property
    def main_model(self) -> str:
        return self._main_model

    @property
    def grounding_model(self) -> str:
        return self._grounding_model

    def _model_for(self, purpose: str) -> str:
        if purpose == "grounding":
            return self._grounding_model
        return self._main_model

    def _chat_options(self, temperature: float | None) -> Any:
        from ..providers.base import ChatOptions

        base = None
        if self._chat_options_factory is not None:
            base = self._chat_options_factory()
        if base is None:
            base = ChatOptions()
        return ChatOptions(
            temperature=temperature,
            max_tokens=base.max_tokens,
            header_timeout_seconds=base.header_timeout_seconds,
            chunk_timeout_seconds=base.chunk_timeout_seconds,
            timeout_seconds=base.timeout_seconds,
            reasoning_effort=base.reasoning_effort,
            tool_choice=None,
        )

    def _wire_to_harness(self, messages: list[dict[str, Any]]) -> list[Any]:
        from ..providers.base import LLMMessage

        harness: list[LLMMessage] = []
        for message in messages:
            role = str(message.get("role", "user"))
            if role not in ("system", "user", "assistant", "tool"):
                role = "user"
            content = message.get("content", [])
            parts: list[dict[str, Any]] = []
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    parts.append(
                        {"type": "text", "text": str(part.get("text", ""))}
                    )
                elif part_type == "image_url":
                    parts.append(
                        {"type": "image_url", "image_url": part.get("image_url")}
                    )
            harness.append(LLMMessage(role=role, content=parts or None))
        return harness

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        purpose: str,
        temperature: float | None,
        use_thinking: bool,
    ) -> Any:
        """Run one Agent-S completion (exact wire messages, ``tools=[]``)."""
        from .ports import Completion as AgentSCompletion
        from .ports import Usage as AgentSUsage

        model_ref = self._model_for(purpose)
        resolved = self._resolve(model_ref)
        harness_messages = self._wire_to_harness(messages)
        self.calls.append(
            {
                "purpose": purpose,
                "model": model_ref,
                "model_id": resolved.model_id,
                "temperature": temperature,
                "use_thinking": use_thinking,
                "messages": messages,
            }
        )
        log.debug(
            "agents_completion",
            purpose=purpose,
            model_ref=model_ref,
            temperature=temperature,
            use_thinking=use_thinking,
        )
        chat_options = self._chat_options(temperature)
        # ``reasoning_effort`` only survives on adapters whose wire format
        # carries it out-of-band (ChatGPT/Responses ``reasoning.effort``);
        # OpenAI-Chat-style payloads would otherwise send it as
        # ``reasoning: {effort}`` next to ``temperature``, corrupting the
        # Agent-S temperature/thinking contract.
        if getattr(resolved.adapter, "name", "") != "chatgpt":
            chat_options = _without_reasoning_effort(chat_options)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = AgentSUsage()
        async for delta in resolved.adapter.chat_stream(
            resolved.model_id, harness_messages, [], chat_options
        ):
            if delta.text:
                text_parts.append(delta.text)
            if delta.reasoning:
                reasoning_parts.append(delta.reasoning)
                if self._emit is not None:
                    await _safe_emit(
                        self._emit,
                        {
                            "type": "part_updated",
                            "delta": {"reasoning": delta.reasoning},
                        },
                    )
            if delta.usage is not None:
                usage = AgentSUsage(
                    input_tokens=usage.input_tokens + delta.usage.prompt_tokens,
                    output_tokens=usage.output_tokens
                    + delta.usage.completion_tokens,
                    cost=usage.cost + float(delta.usage.cost or 0.0),
                )
        text = "".join(text_parts)
        if use_thinking:
            thinking = "".join(reasoning_parts).strip()
            text = (
                f"<thoughts>\n{thinking}\n</thoughts>\n<answer>\n{text}\n</answer>"
            )
        return AgentSCompletion(text=text, usage=usage)


def _without_reasoning_effort(chat_options: Any) -> Any:
    from dataclasses import replace

    try:
        return replace(chat_options, reasoning_effort=None)
    except Exception:
        return chat_options


async def _safe_emit(
    emit: Callable[[dict[str, Any]], Awaitable[None]], event: dict[str, Any]
) -> None:
    try:
        await emit(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - observability only
        log.warning("agents_emit_failed", error=str(exc))


@dataclass
class WorkspaceCodeExecutionAdapter:
    """Run code-agent code exclusively in the isolated workspace.

    Bash mirrors ``LocalController.run_bash_script`` (``bash -lc``,
    timeout 30, stdout+stderr combined into ``output``); python mirrors
    ``run_python_script`` (``python3 -c`` with split stdout/stderr).
    Workdir is ``/workspace``.
    """

    accessor: Any
    workdir: str = "/workspace"
    timeout: int = 30

    async def run_python(self, code: str) -> dict[str, Any]:
        result = await self.accessor.exec_wait(
            ["python3", "-c", code],
            workdir=self.workdir,
            timeout=self.timeout,
        )
        log.debug(
            "agents_code_python",
            code_chars=len(code or ""),
            exit_code=result.exit_code,
        )
        status = "ok" if result.exit_code == 0 else "error"
        return {
            "status": status,
            "return_code": result.exit_code,
            "output": result.stdout or "",
            "error": result.stderr or "",
        }

    async def run_bash(self, code: str, timeout: int = 30) -> dict[str, Any]:
        effective = timeout if timeout and timeout > 0 else self.timeout
        result = await self.accessor.exec_wait(
            ["bash", "-lc", code],
            workdir=self.workdir,
            timeout=effective,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        log.debug(
            "agents_code_bash",
            code_chars=len(code or ""),
            exit_code=result.exit_code,
        )
        return {
            "status": "ok" if result.exit_code == 0 else "error",
            "returncode": result.exit_code,
            "output": combined,
            "error": "",
        }


#: Hint appended when the workspace lacks the Agent-S action dependencies.
AGENT_S_DEPS_HINT = (
    "Rebuild the workspace image to install the Agent-S dependencies "
    "(PyAutoGUI/pyperclip, tesseract-ocr, wmctrl, xclip/xsel, "
    "libreoffice-calc, python3-uno)."
)


@dataclass
class WorkspaceActionExecutor:
    """Execute materialized PyAutoGUI snippets via the desktop RPC.

    Only the core-materialized ``exec_code`` is ever executed — never the
    raw LLM ``plan_code``. Execution is remote-only: the runner's generic
    ``desktop_action("execute", {"code": ...})`` runs ``python3 -c`` with
    the desktop env; the backend never executes locally. Failures raise
    visibly so the run fails instead of silently continuing. The timeout
    defaults to ``ACTION_EXECUTE_TIMEOUT_S`` (runner parity) instead of the
    generic accessor default.
    """

    accessor: Any
    timeout: float | None = ACTION_EXECUTE_TIMEOUT_S

    async def execute_action_code(self, exec_code: str) -> dict[str, Any]:
        """Run *exec_code* (core-materialized) on the workspace display."""
        if not (exec_code or "").strip():
            raise ValueError("Refusing to execute empty action code")
        result = await self.accessor.desktop_action(
            "execute",
            {"code": exec_code},
            timeout=self.timeout,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            error = result.get("error") if isinstance(result, dict) else result
            raise RuntimeError(
                "Desktop action execution failed: " f"{str(error)[:500]}. "
                f"{AGENT_S_DEPS_HINT}"
            )
        log.debug(
            "agents_action_executed",
            code_chars=len(exec_code),
            exit_code=result.get("exit_code", 0),
        )
        if int(result.get("exit_code", 0) or 0) != 0:
            detail = str(result.get("stderr") or result.get("stdout") or "")
            lowered = detail.lower()
            hint = (
                f" {AGENT_S_DEPS_HINT}"
                if (
                    "pyautogui" in lowered
                    or "tesseract" in lowered
                    or "no module named" in lowered
                )
                else ""
            )
            raise RuntimeError(
                "Desktop action execution failed "
                f"(exit {result.get('exit_code', 0)}): "
                f"{detail[:500]} (stderr is forwarded verbatim).{hint}"
            )
        return {
            "exit_code": int(result.get("exit_code", 0) or 0),
            "stdout": str(result.get("stdout") or ""),
            "stderr": str(result.get("stderr") or ""),
        }


@dataclass
class WorkspaceOcrAdapter:
    """Word-level OCR without a backend tesseract dependency.

    The current PNG is staged run-scoped at
    ``.opencuria/computeruse/<run_id>/…`` and a generic
    ``tesseract … tsv`` runs in the workspace; the word-level TSV is
    parsed into the ``text/block_num/left/top/width/height`` rows the
    core expects. Exact OCR cleaning stays in the core.
    """

    accessor: Any
    run_id: str = "run"

    def _staging_dir(self) -> str:
        safe = "".join(
            ch if ch.isalnum() or ch in ("-", "_", ".") else "-"
            for ch in (self.run_id or "run").strip()
        ).strip("-") or "run"
        return f"{OCR_STAGING_ROOT}/{safe}/ocr"

    async def read_words(self, screenshot: bytes) -> list[dict[str, Any]]:
        if not screenshot:
            return []
        staging = self._staging_dir()
        png_path = f"{staging}/frame.png"
        tsv_path = f"{staging}/frame.tsv"
        await self.accessor.write_file(png_path, bytes(screenshot))
        # Parity with ``pytesseract.image_to_data`` defaults: no ``--psm``
        # override (pytesseract passes none, so Tesseract uses its own
        # default page segmentation).
        tesseract_cmd = (
            f"tesseract {shlex.quote(png_path)} {shlex.quote(tsv_path[:-4])} "
            "tsv"
        )
        log.debug(
            "agents_ocr_requested",
            png_bytes=len(screenshot),
            png_path=png_path,
        )
        result = await self.accessor.exec_wait(
            ["bash", "-lc", tesseract_cmd],
            workdir="/workspace",
            timeout=120,
        )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or "").strip()[:500]
            raise RuntimeError(
                "Workspace OCR failed "
                f"(exit {result.exit_code}): {detail}. "
                "Rebuild the workspace image to install tesseract-ocr."
            )
        content = await self.accessor.read_file(tsv_path)
        try:
            text = content.content.decode("utf-8", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"Workspace OCR TSV is not decodable: {exc}") from exc
        return parse_tesseract_tsv(text)


def parse_tesseract_tsv(tsv_text: str) -> list[dict[str, Any]]:
    """Parse word-level ``tesseract … tsv`` output into core OCR rows."""
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(tsv_text or ""), delimiter="\t")
    for record in reader:
        try:
            level = int(str(record.get("level", "") or "0"))
        except ValueError:
            continue
        if level != 5:  # word level only
            continue
        text = str(record.get("text", "") or "")
        if not text.strip():
            continue
        try:
            rows.append(
                {
                    "text": text,
                    "block_num": int(record.get("block_num", 0) or 0),
                    "left": int(record.get("left", 0) or 0),
                    "top": int(record.get("top", 0) or 0),
                    "width": int(record.get("width", 0) or 0),
                    "height": int(record.get("height", 0) or 0),
                }
            )
        except ValueError:
            continue
    return rows


@dataclass
class DesktopScreenshotAdapter:
    """Per-turn PNG capture through the generic screenshot RPC.

    Requests ``format="png"`` with an optional output ``max_dimension``
    (the runner scales generically via ffmpeg and reports the actual
    output width/height). Proportional downscaling to the configured max
    dimension (Agent-S CLI 2400-rule) is applied here — never in the
    runner beyond the generic RPC.
    """

    accessor: Any
    max_dimension: int = 2400

    async def capture_png(
        self, *, max_dimension: int | None = None
    ) -> tuple[bytes, int, int]:
        """Capture one PNG frame; returns ``(png, width, height)``."""
        limit = max_dimension if max_dimension is not None else self.max_dimension
        payload: dict[str, Any] = {"format": "png"}
        if limit is not None:
            payload["max_dimension"] = int(limit)
        result = await self.accessor.desktop_action("screenshot", payload)
        image_b64 = str(result.get("image_b64") or "")
        if not image_b64:
            raise RuntimeError("Desktop screenshot did not return image data.")
        try:
            png = base64.b64decode(image_b64)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "Desktop screenshot returned invalid image data."
            ) from exc
        width = int(result.get("width") or 0)
        height = int(result.get("height") or 0)
        if width < 1 or height < 1:
            raise RuntimeError("Desktop screenshot returned invalid dimensions.")
        log.debug(
            "agents_screenshot",
            png_bytes=len(png),
            width=width,
            height=height,
        )
        return png, width, height

    async def display_geometry(self) -> tuple[int, int]:
        """Return the real desktop geometry (``display_info`` RPC)."""
        try:
            info = await self.accessor.desktop_action("display_info")
            width = int(info.get("width") or 0)
            height = int(info.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
        except Exception as exc:
            log.debug("agents_display_info_failed", error=str(exc))
        from .config import FALLBACK_DESKTOP_HEIGHT, FALLBACK_DESKTOP_WIDTH

        return FALLBACK_DESKTOP_WIDTH, FALLBACK_DESKTOP_HEIGHT


__all__ = [
    "ACTION_EXECUTE_MAX_CHARS",
    "ACTION_EXECUTE_TIMEOUT_S",
    "AGENT_S_DEPS_HINT",
    "OCR_STAGING_ROOT",
    "DesktopScreenshotAdapter",
    "HarnessCompletionAdapter",
    "WorkspaceActionExecutor",
    "WorkspaceCodeExecutionAdapter",
    "WorkspaceOcrAdapter",
    "decode_data_url_png",
    "encode_png_data_url",
    "extract_png_from_wire",
    "parse_tesseract_tsv",
]
