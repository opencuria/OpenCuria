"""Fixed workspace desktop geometry helpers."""

from __future__ import annotations

DEFAULT_DESKTOP_WIDTH = 1920
DEFAULT_DESKTOP_HEIGHT = 1080
MIN_DESKTOP_WIDTH = 800
MAX_DESKTOP_WIDTH = 3840
MIN_DESKTOP_HEIGHT = 600
MAX_DESKTOP_HEIGHT = 2160


def validate_desktop_dimension(value: int, *, kind: str) -> int:
    """Return *value* when it is a valid even desktop dimension.

    Args:
        value: Requested pixel size.
        kind: ``width`` or ``height``.

    Raises:
        ValueError: If *value* is outside the allowed even-pixel range.
    """
    name = "Width" if kind == "width" else "Height"
    minimum = MIN_DESKTOP_WIDTH if kind == "width" else MIN_DESKTOP_HEIGHT
    maximum = MAX_DESKTOP_WIDTH if kind == "width" else MAX_DESKTOP_HEIGHT
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")
    if value % 2 != 0:
        raise ValueError(f"{name} must be an even number.")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def validate_desktop_geometry(
    width: int,
    height: int,
) -> tuple[int, int]:
    """Validate a width/height pair and return the normalized values."""
    return (
        validate_desktop_dimension(width, kind="width"),
        validate_desktop_dimension(height, kind="height"),
    )
