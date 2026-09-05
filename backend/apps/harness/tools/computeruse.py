"""Computer-use tools for the ``computeruse`` subagent (OpenComputer parity)."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import structlog
from pydantic import BaseModel, Field, model_validator

from .base import Tool, ToolContext, ToolError, ToolResult
from .question import QuestionItem, QuestionTool

log = structlog.get_logger(__name__)

GRID_MAX = 1000
GRID_SCALE = 1001
MIN_REGION_SPAN = 20

COMPUTER_USE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "view_screen",
        "view_region",
        "move_mouse",
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "drag",
        "scroll",
        "type_text",
        "press_key",
        "open_url",
        "wait",
        "ask_user",
    }
)

CAPTURE_TEXT = (
    "Treat this returned image as a fresh normalized 1000x1000 grid: "
    "top-left=(0,0), center=(500,500), bottom-right=(1000,1000)."
)


@dataclass
class LastCapture:
    """Pixel bounds of the most recent returned screenshot frame."""

    target_left: int
    target_top: int
    target_width: int
    target_height: int


@dataclass
class ComputerUseState:
    """Per-session coordinate frame for computer-use tools."""

    last_capture: LastCapture | None = None


def get_computer_use_state(ctx: ToolContext) -> ComputerUseState:
    """Return lazily initialized computer-use state on *ctx*."""
    if ctx.computer_use is None:
        ctx.computer_use = ComputerUseState()
    return ctx.computer_use


def scale_coordinate(value: float, from_size: int, to_size: int) -> int:
    """Map a normalized grid value onto pixel coordinates."""
    scaled = round(value * to_size / from_size)
    return max(0, min(to_size - 1, scaled))


def pixel_coordinates(
    capture: LastCapture, *, x: float, y: float
) -> tuple[int, int]:
    """Convert normalized *x*/*y* into absolute desktop pixels."""
    pixel_x = capture.target_left + scale_coordinate(x, GRID_SCALE, capture.target_width)
    pixel_y = capture.target_top + scale_coordinate(y, GRID_SCALE, capture.target_height)
    return pixel_x, pixel_y


def region_pixels(
    capture: LastCapture,
    *,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> tuple[int, int, int, int]:
    """Convert normalized region bounds into pixel crop coordinates."""
    target_left = capture.target_left + scale_coordinate(
        left, GRID_SCALE, capture.target_width
    )
    target_top = capture.target_top + scale_coordinate(
        top, GRID_SCALE, capture.target_height
    )
    target_right = capture.target_left + scale_coordinate(
        right, GRID_SCALE, capture.target_width
    )
    target_bottom = capture.target_top + scale_coordinate(
        bottom, GRID_SCALE, capture.target_height
    )
    crop_w = target_right - target_left + 1
    crop_h = target_bottom - target_top + 1
    return target_left, target_top, crop_w, crop_h


def validate_region_bounds(
    *,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> None:
    """Validate normalized region bounds."""
    bounds = (left, top, right, bottom)
    if any(not isinstance(value, (int, float)) or value != value for value in bounds):
        raise ToolError("Region bounds must be valid numbers.")
    if any(value < 0 or value > GRID_MAX for value in bounds):
        raise ToolError("Region bounds must be valid numbers on the normalized 0..1000 grid.")
    if right <= left or bottom <= top:
        raise ToolError("Region right/bottom bounds must be greater than left/top bounds.")
    if right - left < MIN_REGION_SPAN or bottom - top < MIN_REGION_SPAN:
        raise ToolError(
            "The viewed region must span at least 20 normalized units in each direction."
        )


def validate_http_url(url: str) -> str:
    """Return *url* when it uses http/https; otherwise raise."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolError("Only http and https URLs may be opened.")
    return parsed.geturl()


def decode_screenshot(result: dict[str, Any]) -> tuple[bytes, int, int]:
    """Decode runner screenshot payload into JPEG bytes and dimensions."""
    image_b64 = str(result.get("image_b64") or "")
    if not image_b64:
        raise ToolError("Desktop screenshot did not return image data.")
    try:
        jpeg = base64.b64decode(image_b64)
    except (ValueError, TypeError) as exc:
        raise ToolError("Desktop screenshot returned invalid image data.") from exc
    width = int(result.get("width") or 0)
    height = int(result.get("height") or 0)
    if width < 1 or height < 1:
        raise ToolError("Desktop screenshot returned invalid dimensions.")
    return jpeg, width, height


class ComputerUseController:
    """Shared desktop automation logic for computer-use tools."""

    async def ensure_desktop(self, ctx: ToolContext) -> None:
        """Ensure the workspace desktop session is running."""
        await ctx.accessor.desktop_action("ensure")

    async def capture_full_screen(self, ctx: ToolContext) -> ToolResult:
        """Capture the full primary display and reset the coordinate frame."""
        await self.ensure_desktop(ctx)
        info = await ctx.accessor.desktop_action("display_info")
        width = int(info["width"])
        height = int(info["height"])
        shot = await ctx.accessor.desktop_action("screenshot", {"full": True})
        jpeg, shot_width, shot_height = decode_screenshot(shot)
        state = get_computer_use_state(ctx)
        state.last_capture = LastCapture(
            target_left=0,
            target_top=0,
            target_width=shot_width,
            target_height=shot_height,
        )
        text = (
            f"Primary desktop captured at {shot_width}x{shot_height}. {CAPTURE_TEXT}"
        )
        return ToolResult(output=text, image_jpeg=jpeg, metadata={"width": width, "height": height})

    async def capture_region(
        self,
        ctx: ToolContext,
        *,
        left: float,
        top: float,
        right: float,
        bottom: float,
    ) -> ToolResult:
        """Capture a normalized region of the most recent frame."""
        state = get_computer_use_state(ctx)
        if state.last_capture is None:
            raise ToolError("view_screen must be called before view_region.")
        validate_region_bounds(left=left, top=top, right=right, bottom=bottom)
        crop_x, crop_y, crop_w, crop_h = region_pixels(
            state.last_capture,
            left=left,
            top=top,
            right=right,
            bottom=bottom,
        )
        shot = await ctx.accessor.desktop_action(
            "screenshot",
            {
                "crop_x": crop_x,
                "crop_y": crop_y,
                "crop_w": crop_w,
                "crop_h": crop_h,
            },
        )
        jpeg, shot_width, shot_height = decode_screenshot(shot)
        state.last_capture = LastCapture(
            target_left=crop_x,
            target_top=crop_y,
            target_width=shot_width,
            target_height=shot_height,
        )
        text = (
            f"Desktop region captured at {shot_width}x{shot_height}. {CAPTURE_TEXT}"
        )
        return ToolResult(output=text, image_jpeg=jpeg)

    async def verify_action(
        self,
        ctx: ToolContext,
        text: str,
        *,
        delay_ms: int = 300,
    ) -> ToolResult:
        """Run a mutating action verification screenshot."""
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)
        capture = await self.capture_full_screen(ctx)
        return ToolResult(
            output=f"{text} Verification: {capture.output}",
            image_jpeg=capture.image_jpeg,
            metadata=dict(capture.metadata),
        )

    def require_capture(self, ctx: ToolContext) -> LastCapture:
        """Return the current coordinate frame or raise."""
        capture = get_computer_use_state(ctx).last_capture
        if capture is None:
            raise ToolError(
                "view_screen must be called before using screenshot coordinates."
            )
        return capture

    async def optional_move(
        self,
        ctx: ToolContext,
        args: dict[str, Any],
    ) -> None:
        """Move the mouse when both x and y are supplied."""
        if args.get("x") is None and args.get("y") is None:
            return
        if args.get("x") is None or args.get("y") is None:
            raise ToolError("Supply both x and y coordinates or neither.")
        capture = self.require_capture(ctx)
        x, y = pixel_coordinates(capture, x=float(args["x"]), y=float(args["y"]))
        await ctx.accessor.desktop_action("move", {"x": x, "y": y})

    async def move_mouse(self, ctx: ToolContext, *, x: float, y: float) -> ToolResult:
        """Move the mouse and verify."""
        capture = self.require_capture(ctx)
        pixel_x, pixel_y = pixel_coordinates(capture, x=x, y=y)
        await ctx.accessor.desktop_action("move", {"x": pixel_x, "y": pixel_y})
        return await self.verify_action(
            ctx,
            f"Mouse moved to ({pixel_x}, {pixel_y}).",
            delay_ms=450,
        )

    async def click(
        self,
        ctx: ToolContext,
        *,
        button: str,
        double: bool = False,
        x: float | None = None,
        y: float | None = None,
    ) -> ToolResult:
        """Click at optional coordinates."""
        if (x is None) != (y is None):
            raise ToolError("Supply both x and y coordinates or neither.")
        if x is not None and y is not None:
            await self.optional_move(ctx, {"x": x, "y": y})
        payload: dict[str, Any] = {"button": button}
        if double:
            payload["double"] = True
        await ctx.accessor.desktop_action("click", payload)
        label = f"{button} click"
        if double:
            label = f"Double {label}"
        return await self.verify_action(ctx, f"{label.capitalize()} completed.")

    async def drag(
        self,
        ctx: ToolContext,
        *,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
    ) -> ToolResult:
        """Drag between two normalized coordinates."""
        capture = self.require_capture(ctx)
        sx, sy = pixel_coordinates(capture, x=start_x, y=start_y)
        ex, ey = pixel_coordinates(capture, x=end_x, y=end_y)
        await ctx.accessor.desktop_action(
            "drag",
            {"start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey},
        )
        return await self.verify_action(
            ctx,
            f"Dragged from ({sx}, {sy}) to ({ex}, {ey}).",
            delay_ms=500,
        )

    async def scroll(
        self,
        ctx: ToolContext,
        *,
        direction: str,
        amount: int,
        x: float | None = None,
        y: float | None = None,
    ) -> ToolResult:
        """Scroll in *direction* by *amount* steps."""
        if direction not in {"up", "down", "left", "right"}:
            raise ToolError("Scroll direction must be up, down, left, or right.")
        if amount < 1 or amount > 20:
            raise ToolError("Scroll amount must be between 1 and 20.")
        if (x is None) != (y is None):
            raise ToolError("Supply both x and y coordinates or neither.")
        pixel_x: int | None = None
        pixel_y: int | None = None
        if x is not None and y is not None:
            capture = self.require_capture(ctx)
            pixel_x, pixel_y = pixel_coordinates(capture, x=x, y=y)
            await ctx.accessor.desktop_action("move", {"x": pixel_x, "y": pixel_y})
        payload: dict[str, Any] = {"direction": direction, "amount": amount}
        if pixel_x is not None and pixel_y is not None:
            payload["x"] = pixel_x
            payload["y"] = pixel_y
        await ctx.accessor.desktop_action("scroll", payload)
        return await self.verify_action(
            ctx,
            f"Scrolled {direction} {amount} steps.",
            delay_ms=450,
        )

    async def type_text(self, ctx: ToolContext, text: str) -> ToolResult:
        """Type literal text into the focused application."""
        if not text:
            raise ToolError("type_text requires non-empty text.")
        await ctx.accessor.desktop_action("type", {"text": text})
        return await self.verify_action(
            ctx,
            f"Typed {len(text)} characters.",
        )

    async def press_key(
        self,
        ctx: ToolContext,
        *,
        key: str,
        modifiers: list[str] | None = None,
    ) -> ToolResult:
        """Press a key with optional modifiers."""
        if not key.strip():
            raise ToolError("press_key requires a key.")
        await ctx.accessor.desktop_action(
            "key",
            {"key": key.strip(), "modifiers": list(modifiers or [])},
        )
        return await self.verify_action(ctx, f"Pressed {key.strip()}.")

    async def open_url(self, ctx: ToolContext, url: str) -> ToolResult:
        """Open an http/https URL in the default browser."""
        safe = validate_http_url(url)
        await ctx.accessor.desktop_action("open_url", {"url": safe})
        return await self.verify_action(
            ctx,
            f"Opened {safe} in the default browser.",
            delay_ms=1000,
        )

    async def wait(self, ctx: ToolContext, milliseconds: int) -> ToolResult:
        """Wait briefly, then capture a verification screenshot."""
        if milliseconds < 50 or milliseconds > 10000:
            raise ToolError("Wait milliseconds must be between 50 and 10000.")
        await asyncio.sleep(milliseconds / 1000)
        return await self.verify_action(
            ctx,
            f"Waited {milliseconds} ms.",
            delay_ms=0,
        )


_CONTROLLER = ComputerUseController()


class EmptyArgs(BaseModel):
    """Empty tool arguments."""


class CoordinateField(BaseModel):
    """Shared normalized coordinate field."""

    x: float = Field(ge=0, le=GRID_MAX, description="Horizontal position.")
    y: float = Field(ge=0, le=GRID_MAX, description="Vertical position.")


class OptionalCoordinateArgs(BaseModel):
    """Optional normalized click coordinates."""

    x: float | None = Field(
        default=None,
        ge=0,
        le=GRID_MAX,
        description="Optional horizontal position.",
    )
    y: float | None = Field(
        default=None,
        ge=0,
        le=GRID_MAX,
        description="Optional vertical position.",
    )

    @model_validator(mode="after")
    def _both_or_neither(self) -> OptionalCoordinateArgs:
        """Require both coordinates or neither."""
        if (self.x is None) != (self.y is None):
            raise ValueError("Supply both x and y coordinates or neither.")
        return self


class ViewRegionArgs(BaseModel):
    """Arguments for view_region."""

    left: float = Field(ge=0, le=GRID_MAX, description="Left edge of the region.")
    top: float = Field(ge=0, le=GRID_MAX, description="Top edge of the region.")
    right: float = Field(ge=0, le=GRID_MAX, description="Right edge of the region.")
    bottom: float = Field(
        ge=0, le=GRID_MAX, description="Bottom edge of the region."
    )


class DragArgs(BaseModel):
    """Arguments for drag."""

    startX: float = Field(ge=0, le=GRID_MAX, description="Horizontal drag start.")
    startY: float = Field(ge=0, le=GRID_MAX, description="Vertical drag start.")
    endX: float = Field(ge=0, le=GRID_MAX, description="Horizontal drag end.")
    endY: float = Field(ge=0, le=GRID_MAX, description="Vertical drag end.")


class ScrollArgs(BaseModel):
    """Arguments for scroll."""

    direction: Literal["up", "down", "left", "right"]
    amount: int = Field(ge=1, le=20, description="Number of scroll steps.")
    x: float | None = Field(
        default=None,
        ge=0,
        le=GRID_MAX,
        description="Optional horizontal position to scroll over.",
    )
    y: float | None = Field(
        default=None,
        ge=0,
        le=GRID_MAX,
        description="Optional vertical position to scroll over.",
    )

    @model_validator(mode="after")
    def _both_or_neither(self) -> ScrollArgs:
        """Require both coordinates or neither."""
        if (self.x is None) != (self.y is None):
            raise ValueError("Supply both x and y coordinates or neither.")
        return self


class TypeTextArgs(BaseModel):
    """Arguments for type_text."""

    text: str = Field(min_length=1)


class PressKeyArgs(BaseModel):
    """Arguments for press_key."""

    key: str = Field(min_length=1, description="Examples: enter, tab, escape, a, 1")
    modifiers: list[str] = Field(
        default_factory=list,
        description="Examples: control, shift, alt, command",
    )


class OpenUrlArgs(BaseModel):
    """Arguments for open_url."""

    url: str = Field(min_length=1)


class WaitArgs(BaseModel):
    """Arguments for wait."""

    milliseconds: int = Field(ge=50, le=10000)


class AskUserArgs(BaseModel):
    """Arguments for ask_user."""

    question: str = Field(
        min_length=1,
        description="One concise question that explains what information is needed.",
    )


class ViewScreenTool(Tool):
    """Capture the entire primary display."""

    name = "view_screen"
    description = (
        "Capture the entire primary display. The returned image uses a "
        "normalized 0..1000 coordinate grid."
    )
    args_schema: type[BaseModel] = EmptyArgs
    permission_key = "view_screen"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Capture the full desktop."""
        return await _CONTROLLER.capture_full_screen(ctx)


class ViewRegionTool(Tool):
    """Zoom into a rectangular region of the most recent image."""

    name = "view_region"
    description = (
        "Zoom into a rectangular region of the most recent returned image for "
        "more accurate inspection. The returned region becomes a fresh "
        "normalized 0..1000 grid."
    )
    args_schema: type[BaseModel] = ViewRegionArgs
    permission_key = "view_region"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Capture a cropped region."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ViewRegionArgs)
        return await _CONTROLLER.capture_region(
            ctx,
            left=validated.left,
            top=validated.top,
            right=validated.right,
            bottom=validated.bottom,
        )


class MoveMouseTool(Tool):
    """Move or hover the pointer."""

    name = "move_mouse"
    description = (
        "Move or hover the pointer at normalized x/y coordinates in the most "
        "recent returned image."
    )
    args_schema: type[BaseModel] = CoordinateField
    permission_key = "move_mouse"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Move the mouse."""
        validated = self.coerce_args(args)
        assert isinstance(validated, CoordinateField)
        return await _CONTROLLER.move_mouse(ctx, x=validated.x, y=validated.y)


class LeftClickTool(Tool):
    """Left-click at the cursor or coordinates."""

    name = "left_click"
    description = (
        "Left-click at the cursor, or at normalized x/y coordinates. Supply "
        "both x and y or neither."
    )
    args_schema: type[BaseModel] = OptionalCoordinateArgs
    permission_key = "left_click"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Left-click."""
        validated = self.coerce_args(args)
        assert isinstance(validated, OptionalCoordinateArgs)
        return await _CONTROLLER.click(
            ctx, button="left", x=validated.x, y=validated.y
        )


class RightClickTool(Tool):
    """Right-click at the cursor or coordinates."""

    name = "right_click"
    description = (
        "Right-click at the cursor, or at normalized x/y coordinates. Supply "
        "both x and y or neither."
    )
    args_schema: type[BaseModel] = OptionalCoordinateArgs
    permission_key = "right_click"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Right-click."""
        validated = self.coerce_args(args)
        assert isinstance(validated, OptionalCoordinateArgs)
        return await _CONTROLLER.click(
            ctx, button="right", x=validated.x, y=validated.y
        )


class MiddleClickTool(Tool):
    """Middle-click at the cursor or coordinates."""

    name = "middle_click"
    description = (
        "Middle-click at the cursor, or at normalized x/y coordinates. "
        "Supply both x and y or neither."
    )
    args_schema: type[BaseModel] = OptionalCoordinateArgs
    permission_key = "middle_click"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Middle-click."""
        validated = self.coerce_args(args)
        assert isinstance(validated, OptionalCoordinateArgs)
        return await _CONTROLLER.click(
            ctx, button="middle", x=validated.x, y=validated.y
        )


class DoubleClickTool(Tool):
    """Double left-click at the cursor or coordinates."""

    name = "double_click"
    description = (
        "Double left-click at the cursor, or at normalized x/y coordinates. "
        "Supply both x and y or neither."
    )
    args_schema: type[BaseModel] = OptionalCoordinateArgs
    permission_key = "double_click"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Double-click."""
        validated = self.coerce_args(args)
        assert isinstance(validated, OptionalCoordinateArgs)
        return await _CONTROLLER.click(
            ctx,
            button="left",
            double=True,
            x=validated.x,
            y=validated.y,
        )


class DragTool(Tool):
    """Drag between two normalized positions."""

    name = "drag"
    description = (
        "Drag from one normalized position to another in the most recent "
        "returned image."
    )
    args_schema: type[BaseModel] = DragArgs
    permission_key = "drag"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Drag the mouse."""
        validated = self.coerce_args(args)
        assert isinstance(validated, DragArgs)
        return await _CONTROLLER.drag(
            ctx,
            start_x=validated.startX,
            start_y=validated.startY,
            end_x=validated.endX,
            end_y=validated.endY,
        )


class ScrollTool(Tool):
    """Scroll vertically or horizontally."""

    name = "scroll"
    description = (
        "Scroll vertically or horizontally, optionally after moving to "
        "normalized x/y coordinates. Supply both x and y or neither."
    )
    args_schema: type[BaseModel] = ScrollArgs
    permission_key = "scroll"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Scroll the mouse wheel."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ScrollArgs)
        return await _CONTROLLER.scroll(
            ctx,
            direction=validated.direction,
            amount=validated.amount,
            x=validated.x,
            y=validated.y,
        )


class TypeTextTool(Tool):
    """Type literal text into the focused application."""

    name = "type_text"
    description = "Type literal text into the currently focused application."
    args_schema: type[BaseModel] = TypeTextArgs
    permission_key = "type_text"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Type text."""
        validated = self.coerce_args(args)
        assert isinstance(validated, TypeTextArgs)
        return await _CONTROLLER.type_text(ctx, validated.text)


class PressKeyTool(Tool):
    """Press a key with optional modifiers."""

    name = "press_key"
    description = "Press a key, optionally with modifier keys."
    args_schema: type[BaseModel] = PressKeyArgs
    permission_key = "press_key"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Press a key."""
        validated = self.coerce_args(args)
        assert isinstance(validated, PressKeyArgs)
        return await _CONTROLLER.press_key(
            ctx,
            key=validated.key,
            modifiers=validated.modifiers,
        )


class OpenUrlTool(Tool):
    """Open an http or https URL in the default browser."""

    name = "open_url"
    description = "Open an http or https URL in the default web browser."
    args_schema: type[BaseModel] = OpenUrlArgs
    permission_key = "open_url"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Open a URL."""
        validated = self.coerce_args(args)
        assert isinstance(validated, OpenUrlArgs)
        try:
            return await _CONTROLLER.open_url(ctx, validated.url)
        except ToolError:
            raise
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


class WaitTool(Tool):
    """Wait briefly for the desktop to update."""

    name = "wait"
    description = "Wait briefly for an application or page to update."
    args_schema: type[BaseModel] = WaitArgs
    permission_key = "wait"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Wait and verify."""
        validated = self.coerce_args(args)
        assert isinstance(validated, WaitArgs)
        return await _CONTROLLER.wait(ctx, validated.milliseconds)


class AskUserTool(Tool):
    """Pause and ask the user one structured question."""

    name = "ask_user"
    description = (
        "Pause the task and ask the user for missing information. Use before "
        "guessing an ambiguous target, recipient, preference, or consequential "
        "choice."
    )
    args_schema: type[BaseModel] = AskUserArgs
    permission_key = "ask_user"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Delegate to the shared question handler."""
        validated = self.coerce_args(args)
        assert isinstance(validated, AskUserArgs)
        question_tool = QuestionTool()
        return await question_tool.execute(
            {"questions": [QuestionItem(question=validated.question).model_dump()]},
            ctx,
        )


def computeruse_tools() -> tuple[Tool, ...]:
    """Return all computer-use tool instances."""
    return (
        AskUserTool(),
        ViewScreenTool(),
        ViewRegionTool(),
        MoveMouseTool(),
        LeftClickTool(),
        RightClickTool(),
        MiddleClickTool(),
        DoubleClickTool(),
        DragTool(),
        ScrollTool(),
        TypeTextTool(),
        PressKeyTool(),
        OpenUrlTool(),
        WaitTool(),
    )
