"""Agent-S loop/lifecycle for the harness (second layer).

Adapted in part from simular-ai/Agent-S, ``gui_agents/s3/cli_app.py::run_agent``
(https://github.com/simular-ai/Agent-S, commit 3aa272d, Apache License 2.0).
See THIRD_PARTY_NOTICES.md at the repository root. The surrounding OpenCuria
project remains AGPL-3.0-only.

This module owns the ``computeruse`` run lifecycle: desktop hold +
recording (kept from the previous OpenCuria lifecycle), per-turn PNG
capture, :class:`AgentSession <apps.harness.agent_s.session.AgentSession>`
prediction, and Agent-S signal handling — exactly mirroring
``gui_agents/s3/cli_app.py::run_agent``:

- per Agent-S step a fresh screenshot is captured (PNG, proportionally
  scaled to at most the configured max dimension);
- ``agent.predict`` produces ``(info, [exec_code])``;
- string-signal checks run in Agent-S order over the lowered
  ``exec_code``: contains ``done`` or ``fail`` (lowercased) → stop
  *without* executing anything; contains ``next`` → next turn without
  executing; contains ``wait`` → sleep the WAIT delay and continue;
  otherwise pre-delay, remote execution of the *materialized* code,
  post-delay;
- at most ``max_steps`` turns (default 15, configurable);
- the Agent-S worker ``engine_type`` is derived from the run model
  (see :func:`derive_worker_engine_type`; explicit
  ``worker_engine_params`` win);
- recording stops in ``finally`` and the desktop lease is always
  released; cancellation propagates after cleanup.

The result carries a short final status plus the session-video markdown;
``DONE``/``FAIL``/max-steps outcomes are distinguished via
``metadata``/``finish_reason``. Internal LLM texts are never streamed as
the final assistant answer — observability flows through separate
``reasoning``/``agent`` events. Per outer Agent-S step
``step_start``/``step_finish`` events are emitted and inner Agent-S
completion usages are summed into the OpenCuria ``Usage``/cost.

Only this integration module (plus ``adapters``) may use harness types;
the domain core stays Django/ORM-free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import structlog

log = structlog.get_logger(__name__)

EmitCallback = Callable[[dict[str, Any]], Awaitable[None]]

#: Signals checked (lowercased ``in``) in Agent-S order.
DONE_SIGNAL = "done"
FAIL_SIGNAL = "fail"
NEXT_SIGNAL = "next"
WAIT_SIGNAL = "wait"


@dataclass
class AgentSRunResult:
    """Outcome of one Agent-S computer-use run."""

    output: str
    steps: int
    usage: Any
    cost: float
    finish_reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


def resolve_run_config(
    *,
    effective_model: str,
    run_options: Any | None = None,
    desktop_width: int | None = None,
    desktop_height: int | None = None,
) -> Any:
    """Build the effective :class:`AgentSRunConfig` for a run.

    Precedence: an explicit ``RunOptions.agent_s_config`` wins (validated
    and, when its grounding fields are unset, completed here); otherwise
    the config is derived from the effective harness model with a
    controlled grounding fallback and real desktop geometry (or
    1920x1080). ``RunOptions.max_steps`` caps ``max_steps`` when set.
    """
    from .config import (
        FALLBACK_DESKTOP_HEIGHT,
        FALLBACK_DESKTOP_WIDTH,
        AgentSRunConfig,
    )

    model = (effective_model or "").strip()
    if not model:
        raise ValueError("Agent-S run requires an effective harness model")
    width = desktop_width if desktop_width and desktop_width > 0 else None
    height = desktop_height if desktop_height and desktop_height > 0 else None
    if width is None or height is None:
        width = FALLBACK_DESKTOP_WIDTH
        height = FALLBACK_DESKTOP_HEIGHT
    explicit = getattr(run_options, "agent_s_config", None)
    run_max_steps = getattr(run_options, "max_steps", None)
    run_temperature = getattr(run_options, "agent_s_temperature", None)
    if explicit is not None:
        if not isinstance(explicit, AgentSRunConfig):
            raise ValueError(
                "RunOptions.agent_s_config must be an AgentSRunConfig, "
                f"got {type(explicit).__name__}"
            )
        grounding_model = (explicit.grounding_model or "").strip() or model
        max_steps = explicit.max_steps
        if run_max_steps:
            max_steps = min(max_steps, int(run_max_steps))
        temperature = explicit.model_temperature
        if run_temperature is not None:
            temperature = run_temperature
        return AgentSRunConfig(
            main_model=(explicit.main_model or "").strip() or model,
            grounding_model=grounding_model,
            desktop_width=int(width),
            desktop_height=int(height),
            grounding_width=explicit.grounding_width,
            grounding_height=explicit.grounding_height,
            model_temperature=temperature,
            max_steps=max_steps,
            max_trajectory_length=explicit.max_trajectory_length,
            enable_reflection=explicit.enable_reflection,
            enable_code_agent=explicit.enable_code_agent,
            screenshot_max_dimension=explicit.screenshot_max_dimension,
            action_pre_delay=explicit.action_pre_delay,
            action_post_delay=explicit.action_post_delay,
            wait_delay=explicit.wait_delay,
            extra=dict(explicit.extra or {}),
        )
    max_steps = int(run_max_steps) if run_max_steps else 15
    return AgentSRunConfig(
        main_model=model,
        grounding_model=model,
        desktop_width=int(width),
        desktop_height=int(height),
        model_temperature=run_temperature,
        max_steps=max_steps,
    )


async def _default_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def _video_markdown(recording_path: str) -> str:
    return f"\n\n![Computer use]({recording_path})"


def default_recording_path(run_id: str) -> str:
    return f"/workspace/.opencuria/computeruse/{run_id}/session.mp4"


def sanitize_run_id(session_id: str) -> str:
    """Sanitize a session id for runner ``record_*`` actions."""
    import re
    import uuid as _uuid

    raw = (session_id or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned or not re.match(r"^[A-Za-z0-9]", cleaned):
        cleaned = f"run-{cleaned}" if cleaned else f"run-{_uuid.uuid4().hex[:12]}"
    cleaned = cleaned.strip("-")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", cleaned):
        cleaned = f"run-{_uuid.uuid4().hex[:12]}"
    return cleaned


def append_video_to_output(output: str, recording_path: str) -> str:
    """Append the recording markdown embed when not already present."""
    marker = f"![Computer use]({recording_path})"
    if marker in (output or ""):
        return output
    return (output or "").rstrip() + _video_markdown(recording_path)


def truncate_task_output(
    output: str,
    recording_path: str,
    max_chars: int,
) -> tuple[str, bool]:
    """Truncate *output* for parent task cards, preserving the video line."""
    full = append_video_to_output(output, recording_path)
    suffix = _video_markdown(recording_path)
    if full.endswith(suffix):
        text = full[: -len(suffix)].rstrip()
    else:
        marker = f"![Computer use]({recording_path})"
        idx = full.rfind(marker)
        if idx == -1:
            return full, False
        text = full[:idx].rstrip()
        suffix = full[idx:]
    if len(text) + len(suffix) <= max_chars:
        return text + suffix, False
    notice_reserve = 40
    avail = max(0, max_chars - len(suffix) - notice_reserve)
    return text[:avail] + f"\n…[truncated {len(text)} chars total]" + suffix, True


def classify_signal(exec_code: str) -> str:
    """Classify materialized code into done/fail/next/wait/action (in order)."""
    lowered = (exec_code or "").lower()
    if DONE_SIGNAL in lowered or FAIL_SIGNAL in lowered:
        return DONE_SIGNAL if DONE_SIGNAL in lowered else FAIL_SIGNAL
    if NEXT_SIGNAL in lowered:
        return NEXT_SIGNAL
    if WAIT_SIGNAL in lowered:
        return WAIT_SIGNAL
    return "action"


#: Agent-S long-context engines keep all text and trim only old images.
LONG_CONTEXT_ENGINE_TYPES = frozenset({"anthropic", "openai", "gemini"})


def derive_worker_engine_type(main_model: str) -> str:
    """Derive the Agent-S worker ``engine_type`` for a run model.

    Pure mapping on the main model/provider (mirrors the upstream
    ``engine_type`` contract behind ``Worker.flush_messages``):

    - ``openrouter/*`` → ``"open_router"`` (non-long strategy: whole turns
      are dropped, like upstream);
    - ``chatgpt/*`` (ChatGPT/OpenAI-native semantics) → ``"openai"``
      (long-context strategy: only old images are removed);
    - Bedrock Claude models → ``"anthropic"`` (long-context strategy);
    - ``openai-compatible/*`` and anything else → ``"huggingface"``
      (non-long default).

    Explicit ``worker_engine_params`` always override this derivation; no
    new persisted config is needed.
    """
    from ..providers.bedrock import _is_anthropic_claude_model
    from ..providers.model_ref import parse_model_ref

    provider, bare = parse_model_ref(main_model or "")
    lowered = f"{provider} {(bare or '')}".lower()
    if provider == "openrouter":
        return "open_router"
    if provider == "chatgpt":
        return "openai"
    if provider == "amazon-bedrock" and _is_anthropic_claude_model(bare or ""):
        return "anthropic"
    if "claude" in lowered and "anthropic" in lowered:
        return "anthropic"
    return "huggingface"


def _short_status(*, finish: str, steps: int, detail: str = "") -> str:
    if finish == "done":
        base = f"Computer-use task completed in {steps} step(s)."
    elif finish == "fail":
        base = "Computer-use task reported failure."
    else:
        base = f"Computer-use stopped after {steps} step(s) without done/fail."
    detail = (detail or "").strip()
    if detail:
        return f"{base} {detail[:500]}".rstrip()
    return base


async def _run_cleanup_rpc(
    make_coro: Callable[[], Awaitable[Any]],
) -> tuple[Any | None, BaseException | None, bool]:
    """Await one cleanup RPC to completion despite outer cancellation.

    The RPC runs as its own task awaited through ``asyncio.shield``: an
    outer cancel raises ``CancelledError`` out of the shield at once
    while the inner task keeps running, so on ``CancelledError`` keep
    waiting on the shielded task (best-effort across repeated cancels)
    and only remember the cancellation instead of propagating it here.

    Returns ``(result, error, cancelled)``: *result* on success,
    *error* for a failed RPC (never raised), and *cancelled* when the
    awaiting task observed at least one outer cancellation while
    waiting. The caller runs every cleanup step and re-raises a late
    cancellation afterwards — no cancel suppression, no lease leak.
    """
    try:
        coro = make_coro()
    except Exception as exc:
        return None, exc, False
    task: asyncio.Task[Any] = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                # The RPC itself ended cancelled — nothing left to wait
                # for; surface its outcome as a best-effort error.
                try:
                    task.result()
                except BaseException as exc:
                    return None, exc, cancelled
                return None, None, cancelled
            cancelled = True
            continue
        except Exception as exc:
            return None, exc, cancelled
        return result, None, cancelled


async def run_agent_s_computeruse(
    *,
    prompt: str,
    run_id: str,
    accessor: Any,
    completion: Any,
    code_execution: Any | None,
    ocr: Any,
    action_executor: Any,
    screenshot: Any,
    config: Any,
    emit: EmitCallback | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    worker_engine_params: dict[str, Any] | None = None,
    session_factory: Any | None = None,
) -> AgentSRunResult:
    """Run the Agent-S computer-use loop (mirrors ``run_agent``).

    Desktop hold + recording use the existing OpenCuria lifecycle; every
    turn captures a fresh PNG, calls ``agent.predict``, applies the
    Agent-S string-signal order, executes materialized code remotely, and
    stops recording / releases the lease in ``finally``. Cancellation
    propagates after cleanup.
    """
    from ..providers.base import Usage
    from .materializer import DefaultActionMaterializer, MaterializerConfig
    from .session import AgentSession

    sleep_fn = sleep or _default_sleep
    send = emit or _noop_emit

    total_usage = Usage()
    max_steps = int(config.max_steps)
    run_id = sanitize_run_id(run_id or "session")
    recording_path = default_recording_path(run_id)
    held = False
    record_started = False
    steps = 0
    finish = "max_steps"
    last_info: dict[str, Any] = {}
    last_exec_code = ""
    # The worker routes every inner Agent-S completion usage of a step
    # through ``record``/``usage_sink``; capture them here so they can be
    # summed into the OpenCuria Usage/cost below.
    collected_usages: list[Any] = []

    def _record_step_usage(purpose: Any, usage: Any) -> None:
        collected_usages.append(usage)

    try:
        await accessor.desktop_action("hold", {"kind": "computeruse", "run_id": run_id})
        held = True
        # Real desktop geometry only exists after hold (the cold desktop
        # is not live before the lease). Explicit persisted desktop dims
        # are not present in this phase, so the live geometry wins over
        # the pre-hold config values. A throwing display_geometry falls
        # back to 1920x1080 (see DesktopScreenshotAdapter + config).
        try:
            real_width, real_height = await screenshot.display_geometry()
        except Exception:
            from .config import FALLBACK_DESKTOP_HEIGHT, FALLBACK_DESKTOP_WIDTH

            real_width, real_height = (
                FALLBACK_DESKTOP_WIDTH,
                FALLBACK_DESKTOP_HEIGHT,
            )
        resolved_config = replace(
            config, desktop_width=real_width, desktop_height=real_height
        )
        materializer = DefaultActionMaterializer(
            completion=completion,
            ocr=ocr,
            code_execution=(
                code_execution if resolved_config.enable_code_agent else None
            ),
            config=MaterializerConfig(
                platform="linux",
                width=int(resolved_config.desktop_width),
                height=int(resolved_config.desktop_height),
                grounding_width=int(resolved_config.grounding_width),
                grounding_height=int(resolved_config.grounding_height),
            ),
        )
        if session_factory is not None:
            session = session_factory(completion, materializer)
        else:
            params = dict(worker_engine_params or {})
            # ``Worker.use_thinking`` must see the model string exactly as
            # Agent-S would: the bare resolved provider model id (no
            # OpenCuria ``provider/`` namespace). Only derive it when the
            # caller did not pass explicit engine params.
            if "model" not in params:
                from ..providers.model_ref import parse_model_ref

                _, bare = parse_model_ref(resolved_config.main_model)
                params["model"] = bare or resolved_config.main_model.strip()
            # Worker/reflection temperature comes from the persisted
            # Agent-S config (None = Agent-S SDK default); grounding and
            # code/summary keep their fixed Agent-S values (0 / 1).
            if "temperature" not in params:
                params["temperature"] = resolved_config.model_temperature
            # Upstream trajectory-flush parity: map the run model to the
            # Agent-S engine type (explicit params win).
            if "engine_type" not in params:
                params["engine_type"] = derive_worker_engine_type(
                    resolved_config.main_model
                )
            session = AgentSession(
                completion=completion,
                materializer=materializer,
                platform="linux",
                worker_engine_params=params,
                max_trajectory_length=int(resolved_config.max_trajectory_length),
                enable_reflection=bool(resolved_config.enable_reflection),
                code_execution_available=bool(resolved_config.enable_code_agent),
            )
        max_steps = int(resolved_config.max_steps)
        try:
            start = await accessor.desktop_action("record_start", {"run_id": run_id})
        except Exception as exc:
            raise RuntimeError(f"Computer-use record_start failed: {exc}") from exc
        if not (isinstance(start, dict) and start.get("ok")):
            raise RuntimeError(f"Computer-use record_start failed: {start!r}")
        record_started = True
        recording_path = str(start.get("path") or default_recording_path(run_id))

        for _ in range(max_steps):
            steps += 1
            await send({"type": "step_start", "step": steps})
            step_usage = Usage()
            step_finished = False

            async def _finish_step() -> None:
                nonlocal step_finished
                if step_finished:
                    return
                step_finished = True
                await send(
                    {
                        "type": "step_finish",
                        "step": steps,
                        "tokens": _usage_tokens(step_usage),
                        "cost": step_usage.cost,
                    }
                )

            def _absorb_collected() -> None:
                nonlocal step_usage, total_usage
                for agent_usage in list(collected_usages):
                    converted = _to_harness_usage(agent_usage)
                    total_usage = total_usage.merge(converted)
                    step_usage = step_usage.merge(converted)
                collected_usages.clear()

            try:
                png, width, height = await screenshot.capture_png(
                    max_dimension=int(resolved_config.screenshot_max_dimension)
                )
                obs = {"screenshot": png, "width": width, "height": height}
                info, codes = await session.predict(
                    prompt, obs, record=_record_step_usage, sleep=sleep_fn
                )
                last_info = dict(info or {})
                exec_code = codes[0] if codes else ""
                last_exec_code = exec_code
                _absorb_collected()
                for part in _observable_parts(last_info):
                    await send(part)
                signal = classify_signal(exec_code)
                if signal in (DONE_SIGNAL, FAIL_SIGNAL):
                    finish = "done" if signal == DONE_SIGNAL else "fail"
                    await _finish_step()
                    break
                if signal == NEXT_SIGNAL:
                    await _finish_step()
                    continue
                if signal == WAIT_SIGNAL:
                    await sleep_fn(float(resolved_config.wait_delay))
                    await _finish_step()
                    continue
                await sleep_fn(float(resolved_config.action_pre_delay))
                # Only the core-materialized snippet runs — never raw plan_code.
                await action_executor.execute_action_code(exec_code)
                await sleep_fn(float(resolved_config.action_post_delay))
                await _finish_step()
            except asyncio.CancelledError:
                # Persist usage for already-executed completions even on
                # cancel; the step emit below is best-effort (a second
                # cancel may interrupt it — cleanup in finally still runs
                # shielded).
                _absorb_collected()
                try:
                    await _finish_step()
                except asyncio.CancelledError:
                    pass
                raise
            except Exception:
                # Best-effort: persist already-collected usage for this
                # step before failing visibly (cleanup still runs; the
                # runner wraps this into a run-level error).
                _absorb_collected()
                await _finish_step()
                raise
        detail = str(last_info.get("plan", "") or last_exec_code or "").strip()
        status = _short_status(finish=finish, steps=steps, detail=detail)
        finish_reason = (
            "stop"
            if finish == "done"
            else ("error" if finish == "fail" else "max_steps")
        )
        run_result = {
            "output": status,
            "steps": steps,
            "usage": total_usage,
            "cost": total_usage.cost,
            "finish_reason": finish_reason,
            "metadata": {
                "agent_s_finish": finish.upper()
                if finish in ("done", "fail")
                else "MAX_STEPS",
                "steps": steps,
            },
        }
    finally:
        # Every cleanup RPC completes even under outer cancellation
        # (see ``_run_cleanup_rpc``): an outer cancel while awaiting a
        # shielded RPC only marks ``cleanup_cancelled`` — the RPC is
        # still awaited to completion and the desktop lease is always
        # released. A cancellation that first arrives *during* this
        # cleanup of an otherwise successful run is re-raised afterwards
        # (a cancellation that already aborted the run body propagates
        # from the body itself).
        final_recording_path = recording_path
        cleanup_cancelled = False
        if record_started:
            stop, stop_error, stop_cancelled = await _run_cleanup_rpc(
                lambda: accessor.desktop_action("record_stop", {"run_id": run_id})
            )
            cleanup_cancelled = cleanup_cancelled or stop_cancelled
            if stop_error is not None:  # pragma: no cover - best effort
                log.warning(
                    "computeruse_record_stop_failed",
                    run_id=run_id,
                    error=str(stop_error),
                )
            elif isinstance(stop, dict) and stop.get("path"):
                final_recording_path = str(stop["path"])
        if held:
            _, release_error, release_cancelled = await _run_cleanup_rpc(
                lambda: accessor.desktop_action(
                    "release", {"kind": "computeruse", "run_id": run_id}
                )
            )
            cleanup_cancelled = cleanup_cancelled or release_cancelled
            if release_error is not None:  # pragma: no cover - best effort
                log.warning(
                    "computeruse_desktop_release_failed",
                    run_id=run_id,
                    error=str(release_error),
                )
        if cleanup_cancelled:
            raise asyncio.CancelledError()
    run_result["output"] = append_video_to_output(
        run_result["output"], final_recording_path
    )
    run_result["metadata"]["recording_path"] = final_recording_path
    return AgentSRunResult(**run_result)


def _usage_tokens(usage: Any) -> dict[str, Any]:
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _to_harness_usage(agent_usage: Any) -> Any:
    from ..providers.base import Usage as HarnessUsage

    prompt = int(getattr(agent_usage, "input_tokens", 0) or 0)
    completion = int(getattr(agent_usage, "output_tokens", 0) or 0)
    return HarnessUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost=float(getattr(agent_usage, "cost", 0.0) or 0.0),
    )


def _observable_parts(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose plan/reflections as persisted runner events (never assistant).

    ``HarnessService._persist_runner_event`` only persists ``part_updated``
    (text/reasoning deltas) and ``agent`` events are persisted by the new
    ``agent`` branch as ``HarnessPartType.AGENT`` (frontend card type).
    """
    events: list[dict[str, Any]] = []
    plan = str(info.get("plan", "") or "").strip()
    if plan:
        events.append({"type": "agent", "delta": {"plan": plan[:4000]}})
    reflection = info.get("reflection")
    if isinstance(reflection, str) and reflection.strip():
        events.append(
            {
                "type": "part_updated",
                "delta": {"reasoning": reflection[:4000]},
            }
        )
    thoughts = info.get("reflection_thoughts")
    if isinstance(thoughts, str) and thoughts.strip():
        events.append(
            {
                "type": "part_updated",
                "delta": {"reasoning": thoughts[:4000]},
            }
        )
    return events


async def _noop_emit(event: dict[str, Any]) -> None:
    return None


__all__ = [
    "DONE_SIGNAL",
    "LONG_CONTEXT_ENGINE_TYPES",
    "derive_worker_engine_type",
    "FAIL_SIGNAL",
    "NEXT_SIGNAL",
    "WAIT_SIGNAL",
    "AgentSRunResult",
    "append_video_to_output",
    "classify_signal",
    "default_recording_path",
    "resolve_run_config",
    "run_agent_s_computeruse",
    "sanitize_run_id",
    "truncate_task_output",
]
