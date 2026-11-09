"""
modules/banner_render.py
Free-form banner layers (Figma-style): lists of fills (solid / gradient / image), strokes (inside / center /
outside), effects (drop shadow, inner shadow, layer blur, background blur), rectangles, ellipses and text with
fonts, italic, wrapping and spacing, plus rotation, flipping and per-corner radius.

Only layers marked "v2": true go through here (the announcement designer). Layers without the marker keep
rendering through the original welcome code path, so welcome banners are unchanged.

Layers saved by the first version of the designer (block / logo with color, stroke_*, shadow_* ...) still
render: `_fills_of`, `_strokes_of` and `_effects_of` translate those fields.
"""

from __future__ import annotations

import io
import math
import os
import re

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
except ImportError:  # pragma: no cover - Pillow is a hard requirement of the bot
    Image = ImageChops = ImageDraw = ImageFilter = ImageFont = ImageOps = ImageStat = None

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")

# family -> style key (r, b, i, bi) -> file. Families with one style ignore bold / italic.
FONT_FILES: dict[str, dict[str, str]] = {
    "Poppins": {"r": "Poppins-Regular.ttf", "b": "Poppins-Bold.ttf", "i": "Poppins-Italic.ttf", "bi": "Poppins-BoldItalic.ttf"},
    "Lato": {"r": "Lato-Regular.ttf", "b": "Lato-Bold.ttf", "i": "Lato-Italic.ttf", "bi": "Lato-BoldItalic.ttf"},
    "Bebas Neue": {"r": "BebasNeue-Regular.ttf"},
    "Anton": {"r": "Anton-Regular.ttf"},
    "Pacifico": {"r": "Pacifico-Regular.ttf"},
}

_font_cache: dict[tuple, object] = {}
_RESAMPLE = None


def _lanczos():
    global _RESAMPLE
    if _RESAMPLE is None:
        _RESAMPLE = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    return _RESAMPLE


def is_banner_layer(layer: dict) -> bool:
    return isinstance(layer, dict) and layer.get("v2") is True


def _num(value, fallback: float, lo: float, hi: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    if parsed != parsed:  # NaN
        parsed = fallback
    return max(lo, min(hi, parsed))


def _rgb(color, default=(255, 255, 255)) -> tuple[int, int, int, int]:
    """Hex colour (#rgb / #rrggbb / #rrggbbaa) or 'transparent' -> RGBA."""
    text = str(color or "").strip().lower()
    if text in {"transparent", "none"}:
        return (default[0], default[1], default[2], 0)
    match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", text)
    if not match:
        return (default[0], default[1], default[2], 255)
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    r, g, b = int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)
    a = int(digits[6:8], 16) if len(digits) == 8 else 255
    return (r, g, b, a)


def _scale_alpha(image, percent: float):
    """Multiply the alpha channel of an RGBA image by percent / 100 (in place)."""
    if percent >= 100:
        return image
    image.putalpha(image.getchannel("A").point(lambda v: int(v * max(0.0, percent) / 100.0)))
    return image


def _apply_mask(image, mask):
    """Clip an RGBA image to an 'L' mask without darkening anti-aliased edges."""
    image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    return image


# ----------------------------------------------------------------- legacy -> lists

def _fills_of(layer: dict) -> list[dict]:
    fills = layer.get("fills")
    if isinstance(fills, list):
        return [f for f in fills if isinstance(f, dict)]
    kind = str(layer.get("type") or "").lower()
    if kind in {"logo", "image"}:
        return [{"type": "image", "url": layer.get("image_url"), "fit": layer.get("image_fit") or "cover", "opacity": 100, "visible": True}]
    if str(layer.get("fill_mode") or "solid") == "gradient" and kind != "text":
        return [{"type": "gradient", "color": layer.get("color"), "color2": layer.get("gradient_color"),
                 "angle": layer.get("gradient_angle", 90), "opacity": 100, "visible": True}]
    return [{"type": "solid", "color": layer.get("color") or ("#FFFFFF" if kind == "text" else "#1F232B"), "opacity": 100, "visible": True}]


def _strokes_of(layer: dict) -> list[dict]:
    strokes = layer.get("strokes")
    if isinstance(strokes, list):
        return [s for s in strokes if isinstance(s, dict)]
    width = _num(layer.get("stroke_width"), 0, 0, 40)
    if width <= 0:
        return []
    return [{"width": width, "color": layer.get("stroke_color") or "#FFFFFF", "opacity": layer.get("stroke_opacity", 100),
             "position": "inside", "visible": True}]


def _effects_of(layer: dict) -> list[dict]:
    effects = layer.get("effects")
    if isinstance(effects, list):
        return [e for e in effects if isinstance(e, dict)]
    if _num(layer.get("shadow_opacity"), 0, 0, 100) > 0:
        return [{"type": "drop_shadow", "color": layer.get("shadow_color") or "#000000", "opacity": layer.get("shadow_opacity"),
                 "x": layer.get("shadow_x", 0), "y": layer.get("shadow_y", 4), "blur": layer.get("shadow_blur", 8), "visible": True}]
    return []


# ----------------------------------------------------------------- fonts + text

def _load_font(family: str, size: int, bold: bool, italic: bool, fallback_font):
    key = (family, size, bold, italic)
    if key in _font_cache:
        return _font_cache[key]

    font = None
    files = FONT_FILES.get(family)
    if files and ImageFont is not None:
        style = ("b" if bold else "") + ("i" if italic else "")
        name = files.get(style or "r") or files.get("b" if bold else "r") or files["r"]
        try:
            font = ImageFont.truetype(os.path.join(FONT_DIR, name), size)
        except Exception:
            font = None
    if font is None and fallback_font is not None:
        font = fallback_font(size, bold)
    _font_cache[key] = font
    return font


def _text_width(font, text: str, letter_spacing: float) -> float:
    if not text:
        return 0.0
    if letter_spacing == 0:
        return float(font.getlength(text))
    return sum(font.getlength(ch) + letter_spacing for ch in text) - letter_spacing


def _wrap_lines(font, text: str, max_width: float, letter_spacing: float, wrap: bool) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not wrap:
            lines.append(paragraph)
            continue
        current = ""
        for word in paragraph.split(" "):
            trial = word if not current else f"{current} {word}"
            if not current or _text_width(font, trial, letter_spacing) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
            # A single word wider than the box is broken by characters
            while _text_width(font, current, letter_spacing) > max_width and len(current) > 1:
                cut = len(current) - 1
                while cut > 1 and _text_width(font, current[:cut], letter_spacing) > max_width:
                    cut -= 1
                lines.append(current[:cut])
                current = current[cut:]
        lines.append(current)
    return lines


def _draw_text(layer: dict, width: int, height: int, scale: int, render_text, fallback_font, fill_rgba, stroke_px: int, stroke_rgba):
    text = render_text(str(layer.get("text") or ""))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not text:
        return image

    family = str(layer.get("font_family") or "Poppins")
    size = int(_num(layer.get("font_size"), 22, 6, 400) * scale)
    bold = str(layer.get("font_weight") or "normal").lower() == "bold"
    italic = str(layer.get("font_style") or "normal").lower() == "italic"
    font = _load_font(family, size, bold, italic, fallback_font)
    spacing = _num(layer.get("letter_spacing"), 0, -20, 100) * scale
    line_step = size * _num(layer.get("line_height"), 120, 60, 300) / 100.0
    align = str(layer.get("text_align") or "left").lower()
    vertical = str(layer.get("text_vertical_align") or "top").lower()
    wrap = layer.get("text_wrap", True) is not False

    lines = _wrap_lines(font, text, width, spacing, wrap)
    ascent, descent = font.getmetrics()
    half_leading = (line_step - (ascent + descent)) / 2.0
    total = line_step * len(lines)
    top = 0.0 if vertical == "top" else (height - total) if vertical == "bottom" else (height - total) / 2.0

    draw = ImageDraw.Draw(image)
    for index, raw in enumerate(lines):
        line = raw.strip() if wrap else raw
        line_w = _text_width(font, line, spacing)
        left = 0.0 if align == "left" else (width - line_w) if align == "right" else (width - line_w) / 2.0
        baseline = top + index * line_step + half_leading + ascent
        kwargs = {"font": font, "anchor": "ls", "fill": fill_rgba[:3] + (255,)}
        if stroke_px > 0:
            kwargs.update(stroke_width=stroke_px, stroke_fill=stroke_rgba)
        if spacing == 0:
            draw.text((left, baseline), line, **kwargs)
        else:
            cursor = left
            for ch in line:
                draw.text((cursor, baseline), ch, **kwargs)
                cursor += font.getlength(ch) + spacing
    return image


# ----------------------------------------------------------------- shapes, fills, strokes

def _shape_mask(width: int, height: int, radii, ellipse: bool):
    """'L' mask of a rounded rectangle (own radius per corner, tl/tr/br/bl) or an ellipse."""
    scale = 4
    big_w, big_h = max(1, width * scale), max(1, height * scale)
    if ellipse:
        mask = Image.new("L", (big_w, big_h), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, big_w - 1, big_h - 1), fill=255)
        return mask.resize((width, height), _lanczos())

    limit = min(big_w, big_h) // 2
    tl, tr, br, bl = (max(0, min(limit, int(r * scale))) for r in radii)
    mask = Image.new("L", (big_w, big_h), 255)
    draw = ImageDraw.Draw(mask)
    corners = (
        (tl, (0, 0), (0, 0, 2 * tl, 2 * tl)),
        (tr, (big_w - tr, 0), (big_w - 2 * tr, 0, big_w, 2 * tr)),
        (br, (big_w - br, big_h - br), (big_w - 2 * br, big_h - 2 * br, big_w, big_h)),
        (bl, (0, big_h - bl), (0, big_h - 2 * bl, 2 * bl, big_h)),
    )
    for radius, origin, ellipse_box in corners:
        if radius <= 0:
            continue
        draw.rectangle((origin[0], origin[1], origin[0] + radius - 1, origin[1] + radius - 1), fill=0)
        draw.ellipse(ellipse_box, fill=255)
    return mask.resize((width, height), _lanczos())


def _corner_radii(layer: dict, scale: int, width: int, height: int) -> tuple[float, float, float, float]:
    uniform = _num(layer.get("radius"), 0, 0, 5000)
    limit = min(width, height) / 2
    # A corner without its own value follows the uniform radius
    return tuple(  # type: ignore[return-value]
        min(limit, _num(layer.get(key), uniform, 0, 5000) * scale)
        for key in ("radius_tl", "radius_tr", "radius_br", "radius_bl")
    )


def _gradient_image(width: int, height: int, start: tuple, end: tuple, angle: float):
    """Linear gradient with CSS angle semantics (0deg = to top, 90deg = to right)."""
    theta = math.radians(angle)
    dx, dy = math.sin(theta), -math.cos(theta)
    length = max(1e-6, abs(width * dx) + abs(height * dy))

    ramp = Image.new("L", (2, 258))
    ramp_pixels = ramp.load()
    for row in range(258):
        value = 0 if row == 0 else 255 if row == 257 else row - 1
        ramp_pixels[0, row] = value
        ramp_pixels[1, row] = value

    # Map every output pixel onto the ramp row that matches its position along the gradient line
    k = 255.0 / length
    f = 1.0 + 255.0 * (0.5 - (width / 2 * dx + height / 2 * dy) / length)
    tmap = ramp.transform((width, height), Image.AFFINE, (0.0, 0.0, 1.0, k * dx, k * dy, f), resample=Image.BILINEAR)

    channels = []
    for i in range(4):
        lut = [int(round(start[i] + (end[i] - start[i]) * (t / 255.0))) for t in range(256)]
        channels.append(tmap.point(lut))
    return Image.merge("RGBA", channels)


def _fit_image(source, width: int, height: int, mode: str):
    if mode == "stretch":
        return source.resize((width, height), _lanczos())
    if mode == "contain":
        fitted = ImageOps.contain(source, (width, height), method=_lanczos())
        out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        out.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
        return out
    return ImageOps.fit(source, (width, height), method=_lanczos(), centering=(0.5, 0.5))


async def _fill_image(fill: dict, width: int, height: int, render_text, fetch_bytes):
    kind = str(fill.get("type") or "solid").lower()
    if kind == "gradient":
        start = _rgb(fill.get("color"), (255, 255, 255))
        end = _rgb(fill.get("color2") or fill.get("gradient_color"), (88, 101, 242))
        return _gradient_image(width, height, start, end, _num(fill.get("angle"), 90, -360, 360))
    if kind == "image":
        url = render_text(str(fill.get("url") or fill.get("image_url") or ""))
        raw = await fetch_bytes(url) if url else None
        if not raw:
            return None
        try:
            source = Image.open(io.BytesIO(raw)).convert("RGBA")
            return _fit_image(source, width, height, str(fill.get("fit") or "cover"))
        except Exception:
            return None
    return Image.new("RGBA", (width, height), _rgb(fill.get("color"), (31, 35, 43)))


def _stroke_ring(stage_size, pad: int, width: int, height: int, radii, ellipse: bool, stroke_px: int, position: str, color):
    """One stroke drawn on the padded stage, inside / centred on / outside the layer edge."""
    if position == "outside":
        grow, shrink = stroke_px, 0
    elif position == "center":
        grow, shrink = stroke_px // 2, stroke_px - stroke_px // 2
    else:
        grow, shrink = 0, stroke_px
    shrink = min(shrink, max(0, min(width, height) // 2))

    outer = Image.new("L", stage_size, 0)
    outer.paste(_shape_mask(width + 2 * grow, height + 2 * grow, tuple(r + grow for r in radii), ellipse), (pad - grow, pad - grow))
    inner = Image.new("L", stage_size, 0)
    inner_w, inner_h = width - 2 * shrink, height - 2 * shrink
    if inner_w > 0 and inner_h > 0:
        inner.paste(_shape_mask(inner_w, inner_h, tuple(max(0, r - shrink) for r in radii), ellipse), (pad + shrink, pad + shrink))
    ring = ImageChops.subtract(outer, inner)
    layer = Image.new("RGBA", stage_size, color[:3] + (255,))
    layer.putalpha(ring.point(lambda v: int(v * color[3] / 255)))
    return layer


def _stroke_reach(position: str, stroke_px: int) -> int:
    return stroke_px if position == "outside" else stroke_px // 2 if position == "center" else 0


# ----------------------------------------------------------------- effects

def _bleed_blur(image, sigma: float):
    """Gaussian blur that does not drag transparent black into the colours (fringe-free)."""
    alpha = image.getchannel("A")
    coverage = alpha.point(lambda v: 255 if v > 0 else 0)
    try:
        mean = ImageStat.Stat(image.convert("RGB"), coverage).mean
        base = Image.new("RGB", image.size, tuple(int(round(c)) for c in mean[:3]))
    except Exception:
        base = Image.new("RGB", image.size, (0, 0, 0))
    rgb = Image.composite(image.convert("RGB"), base, coverage)
    merged = Image.merge("RGBA", (*rgb.split(), alpha))
    return merged.filter(ImageFilter.GaussianBlur(sigma))


def _shadow_from_alpha(alpha, color, opacity: float, dx: int, dy: int, blur: float):
    shifted = ImageChops.offset(alpha, dx, dy)
    if blur > 0:
        shifted = shifted.filter(ImageFilter.GaussianBlur(blur / 2.0))  # CSS blur radius = 2 sigma
    shifted = shifted.point(lambda v: int(v * max(0.0, min(100.0, opacity)) / 100.0))
    shadow = Image.new("RGBA", alpha.size, color[:3] + (255,))
    shadow.putalpha(shifted)
    return shadow


def _composite(canvas, image, left: int, top: int) -> None:
    """Alpha-composite `image` at (left, top), clipped to the canvas (negative offsets allowed)."""
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(canvas.width, left + image.width), min(canvas.height, top + image.height)
    if x1 <= x0 or y1 <= y0:
        return
    canvas.alpha_composite(image.crop((x0 - left, y0 - top, x1 - left, y1 - top)), (x0, y0))


# ----------------------------------------------------------------- the layer

async def render_banner_layer(canvas, layer: dict, scale: int, *, render_text, fetch_bytes, fallback_font=None) -> None:
    """Draw one free-form layer onto `canvas` (RGBA, already `scale` times the logical size)."""
    if Image is None or not bool(layer.get("enabled", True)):
        return

    layer_type = str(layer.get("type") or "text").lower()
    is_text = layer_type == "text"
    ellipse = layer_type == "ellipse"
    width = max(1, int(_num(layer.get("width"), 160, 1, 6000) * scale))
    height = max(1, int(_num(layer.get("height"), 80, 1, 6000) * scale))
    left = int(_num(layer.get("x"), 0, -6000, 12000) * scale)
    top = int(_num(layer.get("y"), 0, -6000, 12000) * scale)
    radii = _corner_radii(layer, scale, width, height)
    opacity = _num(layer.get("opacity"), 100, 0, 100)
    rotation = _num(layer.get("rotation"), 0, -360, 360)
    flip_x, flip_y = bool(layer.get("flip_x")), bool(layer.get("flip_y"))

    fills = [f for f in _fills_of(layer) if f.get("visible", True) is not False]
    strokes = []
    for stroke in _strokes_of(layer):
        px = int(_num(stroke.get("width"), 0, 0, 40) * scale)
        rgba = _rgb(stroke.get("color"), (255, 255, 255))
        rgba = rgba[:3] + (int(rgba[3] * _num(stroke.get("opacity"), 100, 0, 100) / 100.0),)
        if stroke.get("visible", True) is not False and px > 0 and rgba[3] > 0:
            strokes.append((px, str(stroke.get("position") or "inside"), rgba))
    effects = []
    for effect in _effects_of(layer):
        if effect.get("visible", True) is False:
            continue
        kind = str(effect.get("type") or "drop_shadow")
        if kind in {"drop_shadow", "inner_shadow", "layer_blur", "background_blur"}:
            effects.append((
                kind,
                _rgb(effect.get("color"), (0, 0, 0)),
                _num(effect.get("opacity"), 50, 0, 100),
                int(_num(effect.get("x"), 0, -400, 400) * scale),
                int(_num(effect.get("y"), 0, -400, 400) * scale),
                _num(effect.get("blur"), 0, 0, 200) * scale,
            ))

    # Room around the layer for outside strokes, shadows, blur and rotation
    reach = max([_stroke_reach(position, px) for px, position, _ in strokes], default=0) if not is_text else 0
    effect_reach = 0
    for kind, _, _, dx, dy, blur in effects:
        if kind == "drop_shadow":
            effect_reach = max(effect_reach, int(math.ceil(blur * 1.5)) + max(abs(dx), abs(dy)))
        elif kind == "layer_blur":
            effect_reach = max(effect_reach, int(math.ceil(blur * 1.5)))
    rotate_extra = 0
    if rotation % 360:
        box_w, box_h = width + 2 * reach, height + 2 * reach
        rotate_extra = int(math.ceil(math.hypot(box_w, box_h) / 2 - min(box_w, box_h) / 2)) + 2
    pad = reach + effect_reach + rotate_extra + 2
    stage_size = (width + 2 * pad, height + 2 * pad)

    # ---- content
    stage = Image.new("RGBA", stage_size, (0, 0, 0, 0))
    shape_mask = None
    if is_text:
        text_fill = next((f for f in reversed(fills) if str(f.get("type") or "solid") != "image"), {"color": "#FFFFFF", "opacity": 100})
        fill_rgba = _rgb(text_fill.get("color"), (255, 255, 255))
        outline = strokes[0] if strokes else (0, "inside", (0, 0, 0, 255))
        content = _draw_text(layer, width, height, scale, render_text, fallback_font, fill_rgba, outline[0], outline[2])
        _scale_alpha(content, _num(text_fill.get("opacity"), 100, 0, 100) * fill_rgba[3] / 255.0)
        stage.paste(content, (pad, pad))
    else:
        shape_mask = _shape_mask(width, height, radii, ellipse)
        content = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        for fill in fills:
            image = await _fill_image(fill, width, height, render_text, fetch_bytes)
            if image is None:
                continue
            _scale_alpha(image, _num(fill.get("opacity"), 100, 0, 100))
            content = Image.alpha_composite(content, _apply_mask(image, shape_mask))
        stage.paste(content, (pad, pad))

    # inner shadows sit on the fill, under the strokes
    for kind, color, shadow_opacity, dx, dy, blur in effects:
        if kind != "inner_shadow":
            continue
        alpha = stage.getchannel("A")
        hole = ImageChops.invert(alpha)
        glow = _shadow_from_alpha(hole, color, shadow_opacity, dx, dy, blur)
        _apply_mask(glow, alpha)
        stage = Image.alpha_composite(stage, glow)

    if not is_text:
        for px, position, rgba in strokes:
            stage = Image.alpha_composite(stage, _stroke_ring(stage_size, pad, width, height, radii, ellipse, px, position, rgba))

    if opacity < 100:
        _scale_alpha(stage, opacity)

    mask_stage = None
    if shape_mask is not None and any(effect[0] == "background_blur" for effect in effects):
        mask_stage = Image.new("L", stage_size, 0)
        mask_stage.paste(shape_mask, (pad, pad))

    if flip_x:
        stage = ImageOps.mirror(stage)
        mask_stage = ImageOps.mirror(mask_stage) if mask_stage is not None else None
    if flip_y:
        stage = ImageOps.flip(stage)
        mask_stage = ImageOps.flip(mask_stage) if mask_stage is not None else None
    if rotation % 360:
        # Same direction as CSS rotate(): positive = clockwise, around the layer centre
        center = (pad + width / 2, pad + height / 2)
        stage = stage.rotate(-rotation, resample=Image.BICUBIC, center=center)
        if mask_stage is not None:
            mask_stage = mask_stage.rotate(-rotation, resample=Image.BICUBIC, center=center)

    origin = (left - pad, top - pad)

    # ---- effects that need the canvas or the finished layer
    for kind, _, _, _, _, blur in effects:
        if kind == "layer_blur" and blur > 0:
            stage = _bleed_blur(stage, blur / 2.0)
        elif kind == "background_blur" and blur > 0 and mask_stage is not None:
            x0, y0 = max(0, origin[0]), max(0, origin[1])
            x1, y1 = min(canvas.width, origin[0] + stage.width), min(canvas.height, origin[1] + stage.height)
            if x1 > x0 and y1 > y0:
                region = canvas.crop((x0, y0, x1, y1))
                blurred = _bleed_blur(region, blur / 2.0)
                region_mask = mask_stage.crop((x0 - origin[0], y0 - origin[1], x1 - origin[0], y1 - origin[1]))
                canvas.paste(blurred, (x0, y0), region_mask)

    for kind, color, shadow_opacity, dx, dy, blur in effects:
        if kind == "drop_shadow":
            # The stage alpha already includes the layer opacity
            shadow = _shadow_from_alpha(stage.getchannel("A"), color, shadow_opacity, dx, dy, blur)
            _composite(canvas, shadow, origin[0], origin[1])

    _composite(canvas, stage, origin[0], origin[1])
