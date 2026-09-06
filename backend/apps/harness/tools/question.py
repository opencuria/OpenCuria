"""OpenCode-style user question tool: pauses until the user answers."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolError, ToolResult


class QuestionOption(BaseModel):
    """One selectable option for a structured question."""

    label: str = Field(description="Short option label.")
    description: str = Field(default="", description="Optional longer description.")


class QuestionItem(BaseModel):
    """One structured question presented to the user."""

    header: str = Field(default="", description="Optional section header.")
    question: str = Field(description="The question text.")
    options: list[QuestionOption] = Field(
        default_factory=list,
        description="Selectable options (free-text when empty).",
    )
    multiple: bool = Field(
        default=False,
        description="Allow selecting more than one option.",
    )


class QuestionArgs(BaseModel):
    """Arguments for the question tool."""

    questions: list[QuestionItem] = Field(
        min_length=1,
        description="Structured questions to ask the user mid-run.",
    )


class QuestionTool(Tool):
    """Ask the user structured questions and wait for answers."""

    name = "question"
    description = (
        "Ask the user one or more structured questions and pause until they "
        "answer. Use when a decision or clarification is required mid-run."
    )
    args_schema: type[BaseModel] = QuestionArgs
    permission_key = "question"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a question invocation."""
        assert isinstance(args, QuestionArgs)
        if args.questions:
            return args.questions[0].question[:120]
        return "Question"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Persist questions, wait for user answers, return them as JSON."""
        validated = self.coerce_args(args)
        assert isinstance(validated, QuestionArgs)
        callback = ctx.on_question
        if callback is None:
            raise ToolError(
                "Question handler not configured for this run",
                tool=self.name,
            )
        payload = [item.model_dump(mode="json") for item in validated.questions]
        pending = callback(questions=payload, call_id=ctx.call_id)
        timeout = ctx.question_timeout
        try:
            if timeout is not None and timeout > 0:
                answers = await asyncio.wait_for(pending, timeout=timeout)
            else:
                answers = await pending
        except asyncio.TimeoutError as exc:
            raise ToolError(
                f"Question timed out after {timeout:.0f}s",
                tool=self.name,
            ) from exc
        if not isinstance(answers, list):
            raise ToolError("Question answers must be a list", tool=self.name)
        return ToolResult(
            output=json.dumps({"answers": answers}, ensure_ascii=False),
            metadata={"answers": answers, "questions": payload},
        )
