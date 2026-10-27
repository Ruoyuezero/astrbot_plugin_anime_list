"""番剧缩略图网格渲染。"""

from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFilter

from .render import font

W = 1000
M = 50
PAPER = (247, 245, 240)
INK = (32, 30, 28)
SUB = (128, 122, 112)
ACCENT = (196, 74, 74)

COLS = 6
GAP = 14
CELL_W = (W - 2 * M - GAP * (COLS - 1)) // COLS
COVER_H = int(CELL_W * 1.41)
NAME_H = 44
ROW_H = COVER_H + NAME_H + 16


_PLACEHOLDER = [(226, 178, 178), (200, 196, 224), (186, 214, 200),
                (232, 208, 168), (196, 214, 232), (222, 200, 224)]


def _cover(raw: bytes | None, w: int, h: int, name: str = "") -> Image.Image:
    if raw:
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            ratio = max(w / img.width, h / img.height)
            img = img.resize((int(img.width * ratio) + 1, int(img.height * ratio) + 1), Image.LANCZOS)
            x = (img.width - w) // 2
            y = (img.height - h) // 2
            return img.crop((x, y, x + w, y + h))
        except Exception:
            pass
    # 没有封面就画一个带首字的色块，比纯灰好看
    color = _PLACEHOLDER[sum(ord(c) for c in name) % len(_PLACEHOLDER)]
    box = Image.new("RGB", (w, h), color)
    if name:
        d = ImageDraw.Draw(box)
        f = font(int(h * 0.30))
        d.text((w // 2, h // 2), name[0], font=f, fill=(255, 255, 255), anchor="mm")
    return box


def _fit(draw, text: str, fnt, maxw: int, lines: int = 2) -> list[str]:
    out, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=fnt) <= maxw:
            cur += ch
        else:
            out.append(cur)
            cur = ch
            if len(out) >= lines:
                break
    if cur and len(out) < lines:
        out.append(cur)
    if len(out) == lines:
        while draw.textlength(out[-1] + "…", font=fnt) > maxw and len(out[-1]) > 1:
            out[-1] = out[-1][:-1]
        if sum(len(x) for x in out) < len(text):
            out[-1] = out[-1] + "…"
    return out


def render_grid(items: list[dict], covers: dict[str, bytes], title: str, subtitle: str, out: str) -> str:
    rows = (len(items) + COLS - 1) // COLS
    H = 154 + rows * ROW_H + 56
    img = Image.new("RGB", (W, H), PAPER)
    img.paste(Image.new("RGB", (W, 10), ACCENT), (0, 0))

    d = ImageDraw.Draw(img)
    d.text((M, 54), title, font=font(44), fill=INK)
    d.text((M, 114), subtitle, font=font(23, "Regular"), fill=SUB)

    f_name = font(19, "Regular")
    y0 = 154
    for idx, it in enumerate(items):
        col, row = idx % COLS, idx // COLS
        x = M + col * (CELL_W + GAP)
        y = y0 + row * ROW_H
        raw = covers.get(it.get("jp") or "") or covers.get(it["cn"])
        box = _cover(raw, CELL_W, COVER_H, it["cn"])
        img.paste(box, (x, y))
        d.rectangle([x, y, x + CELL_W - 1, y + COVER_H - 1], outline=(226, 221, 212), width=1)
        # 名字两行
        for i, line in enumerate(_fit(d, it["cn"], f_name, CELL_W, 2)):
            d.text((x, y + COVER_H + 6 + i * 21), line, font=f_name, fill=INK)

    d.text((M, H - 38), "数据来源 yuc.wiki + AniList", font=font(17, "Regular"), fill=SUB)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    img.save(out, quality=92)
    return out
