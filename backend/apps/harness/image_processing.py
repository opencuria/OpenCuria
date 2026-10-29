"""Bounded image previews shared by user attachments and file-reading tools."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

MAX_IMAGE_SOURCE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_EDGE = 2048
MAX_IMAGE_PREVIEW_BYTES = 1_500_000


@dataclass(frozen=True)
class ImagePreview:
    """A decoded, provider-safe image, with the original file left untouched."""

    content: bytes
    mime: str


def _prepare_image(content: bytes) -> ImagePreview:
    if not content or len(content) > MAX_IMAGE_SOURCE_BYTES:
        raise ValueError("Image exceeds the preview ingestion limit")
    with Image.open(
        io.BytesIO(content), formats=("PNG", "JPEG", "GIF", "WEBP")
    ) as source:
        if source.width * source.height > MAX_IMAGE_PIXELS:
            raise ValueError("Image exceeds the preview pixel limit")
        source.load()
        mime = Image.MIME[source.format]
        if (
            source.format != "GIF"
            and not getattr(source, "is_animated", False)
            and source.mode in {"RGB", "RGBA", "L", "LA", "P"}
            and source.getexif().get(274, 1) == 1
            and max(source.size) <= MAX_IMAGE_EDGE
            and len(content) <= MAX_IMAGE_PREVIEW_BYTES
        ):
            return ImagePreview(content, mime)
        image = ImageOps.exif_transpose(source)
        image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
        alpha = "A" in image.getbands() or "transparency" in image.info
        image = image.convert("RGBA" if alpha else "RGB")

    for _ in range(8):
        output = io.BytesIO()
        image.save(output, format="PNG")
        if output.tell() <= MAX_IMAGE_PREVIEW_BYTES:
            return ImagePreview(output.getvalue(), "image/png")
        if not alpha:
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85)
            if output.tell() <= MAX_IMAGE_PREVIEW_BYTES:
                return ImagePreview(output.getvalue(), "image/jpeg")
        image.thumbnail(
            (max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)),
            Image.Resampling.LANCZOS,
        )
    raise ValueError("Image could not be reduced to the preview limit")


async def prepare_image(content: bytes) -> ImagePreview:
    """Validate and resize an image off the event loop without writing files."""
    return await asyncio.to_thread(_prepare_image, content)


def image_path_notice(path: str, reason: str) -> str:
    """Explain a path-only attachment without exposing parser or I/O errors."""
    return f"\n[Image not loaded: {reason}. Original file: {path}.]\n"
