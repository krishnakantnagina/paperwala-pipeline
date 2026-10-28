"""Before/after images for the review page: one per ad block."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

MARGIN = 60
MAX_WIDTH = 1400


def region_crop(before: Image.Image, after: Image.Image | None, bbox, out_path: Path,
                applied: bool, boxes=None) -> Path:
    x0, y0, x1, y1 = bbox
    box = (max(0, x0 - MARGIN), max(0, y0 - MARGIN),
           min(before.width, x1 + MARGIN), min(before.height, y1 + MARGIN))
    left = before.crop(box).convert("RGB")
    draw = ImageDraw.Draw(left)
    # Red = will be erased; orange = blocked by the safety rule.
    for bx0, by0, bx1, by1 in boxes or [bbox]:
        draw.rectangle((bx0 - box[0], by0 - box[1], bx1 - box[0], by1 - box[1]),
                       outline=(220, 30, 30) if applied else (240, 140, 0), width=4)
    panels = [left]
    if after is not None and after.size == before.size:
        panels.append(after.crop(box).convert("RGB"))
    w = sum(p.width for p in panels) + 20 * (len(panels) - 1)
    sheet = Image.new("RGB", (w, left.height), "white")
    x = 0
    for p in panels:
        sheet.paste(p, (x, 0))
        x += p.width + 20
    if sheet.width > MAX_WIDTH:
        sheet = sheet.resize((MAX_WIDTH, round(sheet.height * MAX_WIDTH / sheet.width)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "PNG", optimize=True)
    return out_path


def page_thumb(img: Image.Image, out_path: Path, width: int = 500) -> Path:
    thumb = img.convert("RGB").resize((width, round(img.height * width / img.width)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    thumb.save(out_path, "PNG", optimize=True)
    return out_path
