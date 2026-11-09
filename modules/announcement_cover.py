"""Announcement cover picture: a Roblox game's cover photo with the announcement title written on it."""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

_STRIP_RATIO = 2.6
_FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Poppins-Bold.ttf"


def _draw_title(cover: bytes, title: str) -> bytes:
    image = Image.open(io.BytesIO(cover)).convert("RGB")
    # Upscaled so the text edges stay sharp when Discord shows the picture large
    image = image.resize((image.width * 2, image.height * 2), Image.LANCZOS)
    # Short wide strip (2.6 : 1) instead of the full 16 : 9 picture, cut a little above the middle where the artwork is
    strip = int(image.width / _STRIP_RATIO)
    top = int((image.height - strip) * 0.3)
    image = image.crop((0, top, image.width, top + strip))
    width, height = image.size
    draw = ImageDraw.Draw(image)

    max_width = int(width * 0.9)
    size = int(height * 0.24)
    while size > 16:
        font = ImageFont.truetype(str(_FONT), size)
        stroke = max(2, size // 8)
        left, top, right, bottom = draw.textbbox((0, 0), title, font=font, stroke_width=stroke)
        if right - left <= max_width:
            break
        size -= 4
    x = int(width * 0.04) - left
    y = int(height * 0.8) - (bottom - top) // 2 - top
    draw.text((x, y), title, font=font, fill="white", stroke_width=stroke, stroke_fill="black")

    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


async def render_cover_with_title(cover_url: str, title: str) -> bytes | None:
    """PNG bytes of the cover with the title on it, or None if the cover can't be downloaded."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(cover_url) as resp:
            if resp.status != 200:
                return None
            cover = await resp.read()
    return await asyncio.to_thread(_draw_title, cover, title)
