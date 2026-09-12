"""Multi-turn session wrapper (mirrors ``AgentS3`` + ``Worker`` lifecycle).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import worker as _worker
from .materializer import DefaultActionMaterializer, MaterializerConfig
from .ports import ActionMaterializer, CompletionPort
from .worker import StepOutput, WorkerState


@dataclass
class AgentSession:
    """Owns worker state + materializer across turns (``AgentS3.reset``).

    ``code_execution_available`` mirrors Agent-S hiding ``call_code_agent``
    when no env/controller is present. When ``None`` (default) it is derived
    from a :class:`DefaultActionMaterializer` (``code_execution is not
    None``); with a custom materializer it must be given explicitly.
    """

    completion: CompletionPort
    materializer: ActionMaterializer
    platform: str = "linux"
    worker_engine_params: dict[str, Any] = field(default_factory=dict)
    max_trajectory_length: int = 8
    enable_reflection: bool = True
    code_execution_available: bool | None = None
    state: WorkerState = field(init=False)

    def __post_init__(self) -> None:
        self.reset()

    def _resolve_code_execution_available(self) -> bool:
        if self.code_execution_available is not None:
            return self.code_execution_available
        if isinstance(self.materializer, DefaultActionMaterializer):
            return self.materializer.code_execution is not None
        raise ValueError(
            "code_execution_available must be given explicitly "
            "with a custom ActionMaterializer"
        )

    def reset(self) -> None:
        self.state = _worker.create_state(
            platform=self.platform,
            worker_engine_params=self.worker_engine_params,
            code_execution_available=self._resolve_code_execution_available(),
            max_trajectory_length=self.max_trajectory_length,
            enable_reflection=self.enable_reflection,
        )

    async def predict(
        self,
        instruction: str,
        obs: dict[str, Any],
        *,
        record=None,
        sleep=None,
        signature_binders=None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Run one turn; returns ``(info, [exec_code])`` like ``predict``."""
        if signature_binders is None:
            signature_binders = _worker.default_signature_binders()
        output: StepOutput = await _worker.run_step(
            self.state,
            self.completion,
            self.materializer,
            instruction,
            obs,
            record=record,
            sleep=sleep,
            signature_binders=signature_binders,
        )
        info = {
            "plan": output.plan,
            "plan_code": output.plan_code,
            "exec_code": output.exec_code,
            "reflection": output.reflection,
            "reflection_thoughts": output.reflection_thoughts,
            "code_agent_output": output.code_agent_output,
        }
        return info, [output.exec_code]


def create_session(
    completion: CompletionPort,
    *,
    platform: str = "linux",
    worker_engine_params: dict[str, Any] | None = None,
    max_trajectory_length: int = 8,
    enable_reflection: bool = True,
    materializer_config: MaterializerConfig | None = None,
    ocr=None,
    code_execution=None,
) -> AgentSession:
    """Build a session with the default materializer (test convenience)."""
    materializer = DefaultActionMaterializer(
        completion=completion,
        ocr=ocr,
        code_execution=code_execution,
        config=materializer_config or MaterializerConfig(platform=platform),
    )
    return AgentSession(
        completion=completion,
        materializer=materializer,
        platform=platform,
        worker_engine_params=dict(worker_engine_params or {}),
        max_trajectory_length=max_trajectory_length,
        enable_reflection=enable_reflection,
    )
