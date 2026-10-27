"""周报 / 季度表渲染成图片。"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFilter

from .render import font

W = 980
M = 54
PAPER = (247, 245, 240)
INK = (38, 36, 34)
SUB = (122, 116, 108)
ACCENT = (196, 74, 74)
WEEK_NAMES = ["一", "二", "三", "四", "五", "六", "日"]


def _ellipsis(draw, text: str, fnt, maxw: int) -> str:
    if draw.textlength(text, font=fnt) <= maxw:
        return text
    while text and draw.textlength(text + "…", font=fnt) > maxw:
        text = text[:-1]
    return text + "…"


def render_schedule(items: list[dict], title: str, subtitle: str, out: str) -> str:
    groups = []
    for w in WEEK_NAMES:
        g = [it for it in items if it.get("week") == w]
        if g:
            groups.append((w, g))

    row_h = 36
    col_gap = 34
    col_w = (W - 2 * M - col_gap) // 2
    blocks = []
    for w, g in groups:
        rows = (len(g) + 1) // 2
        blocks.append((w, g, 54 + rows * row_h))

    total = 168 + sum(b[2] for b in blocks) + 24 * max(0, len(blocks) - 1) + 66
    H = max(560, total)

    img = Image.new("RGB", (W, H), PAPER)

    # 顶部主题色横条 + 一点噪点质感
    top = Image.new("RGB", (W, 10), ACCENT)
    img.paste(top, (0, 0))

    d = ImageDraw.Draw(img)
    f_title = font(46)
    f_sub = font(24, "Regular")
    f_week = font(30)
    f_item = font(23, "Regular")

    d.text((M, 56), title, font=f_title, fill=INK)
    d.text((M, 116), subtitle, font=f_sub, fill=SUB)

    y = 168
    for w, g, block_h in blocks:
        # 星期标题 + 细线
        d.text((M, y), f"周{w}", font=f_week, fill=ACCENT)
        line_y = y + 44
        d.line([(M, line_y), (W - M, line_y)], fill=(222, 217, 208), width=2)

        for i, it in enumerate(g):
            col = i % 2
            row = i // 2
            x = M + col * (col_w + col_gap)
            ty = line_y + 12 + row * row_h
            name = _ellipsis(d, it["cn"], f_item, col_w - 60)
            d.text((x, ty), name, font=f_item, fill=INK)
            if it.get("slot"):
                d.text((x + col_w - 46, ty + 3), it["slot"], font=font(17, "Regular"), fill=SUB)

        y += block_h + 24

    d.text((M, H - 46), "数据来源 yuc.wiki", font=font(18, "Regular"), fill=SUB)
    parent = os.path.dirname(os.path.abspath(out))
    os.makedirs(parent, exist_ok=True)
    img.save(out, quality=94)
    return out
