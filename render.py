"""番剧卡片渲染：高清封面做底 + 宋体文字 + 阴影。"""

from __future__ import annotations

import io
import os
import re

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 820, 1300
RENDER_VERSION = 7  # 改动渲染样式时把这里 +1，旧图自动失效
M = 60
BLUR_PCT = 0  # 高斯模糊强度（%），0 表示封面原样

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\NotoSerifSC-VF.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]
_font_path: str | None = None
_font_cache: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}


def _resolve_font() -> str:
    global _font_path
    if _font_path:
        return _font_path
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            _font_path = path
            return path
    raise RuntimeError("找不到可用的中文字体")


def font(size: int, weight: str = "SemiBold") -> ImageFont.FreeTypeFont:
    key = (size, weight)
    if key in _font_cache:
        return _font_cache[key]
    f = ImageFont.truetype(_resolve_font(), size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        try:
            f.set_variation_by_axes([600])
        except Exception:
            pass
    _font_cache[key] = f
    return f


def cover_fill(img: Image.Image, w: int, h: int) -> Image.Image:
    ratio = max(w / img.width, h / img.height)
    nw, nh = int(img.width * ratio + 1), int(img.height * ratio + 1)
    img = img.resize((nw, nh), Image.LANCZOS)
    x, y = (nw - w) // 2, (nh - h) // 2
    return img.crop((x, y, x + w, y + h))


def wrap(text: str, fnt, maxw: int, limit: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = re.findall(r"[A-Za-z0-9'’\-]+|\s+|.", text)
    lines, cur = [], ""
    for word in words:
        probe = cur + word
        if draw.textlength(probe, font=fnt) <= maxw:
            cur = probe
        else:
            if cur.strip():
                lines.append(cur.rstrip())
            cur = word.lstrip()
            if len(lines) >= limit:
                break
    if cur.strip() and len(lines) < limit:
        lines.append(cur.rstrip())
    if lines and len(lines) >= limit:
        last = lines[limit - 1]
        while draw.textlength(last + "…", font=fnt) > maxw and len(last) > 1:
            last = last[:-1]
        lines[limit - 1] = last + "…"
    return lines[:limit]


def split_lines(text: str, fnt, maxw: int, max_lines: int, draw) -> list[str]:
    """按字符贪心折行，超出 max_lines 的部分用省略号收尾。"""
    lines, cur, idx = [], "", 0
    for i, ch in enumerate(text):
        if draw.textlength(cur + ch, font=fnt) <= maxw:
            cur += ch
            idx = i + 1
        else:
            lines.append(cur)
            cur = ch
            idx = i + 1
            if len(lines) >= max_lines - 1:
                break
    if cur:
        lines.append(cur)
    tail = text[idx:]
    if tail:
        last = lines[-1] + tail
        while draw.textlength(last, font=fnt) > maxw and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines[:max_lines]


def layout(draw, data, color, status_fill=None, draw_status=True, stroke=None):
    sw = 2 if stroke else 0

    def txt(xy, s, f, c, anchor="la"):
        draw.text(xy, s, font=f, fill=c, anchor=anchor,
                  stroke_width=sw, stroke_fill=stroke if stroke else None)

    f_small = font(23, "Medium")
    txt((M, 92), data["genre"], f_small, color)

    if not draw_status:
        st, tw = "", 0
    else:
        st = data["status_text"]
        tw = draw.textlength(st, font=f_small)
    h, w = 46, tw + 40
    bx, by = W - M - w, 82
    if draw_status:
        if status_fill is not None:
            # 胶囊自带底色，内部文字不描边
            draw.rounded_rectangle([bx, by, bx + w, by + h], radius=h // 2, fill=status_fill)
            draw.text((bx + w / 2, by + h / 2 + 1), st, font=f_small,
                      fill=(255, 255, 255, 255), anchor="mm")
        else:
            draw.rounded_rectangle([bx, by, bx + w, by + h], radius=h // 2, fill=color)
            draw.text((bx + w / 2, by + h / 2 + 1), st, font=f_small, fill=color, anchor="mm")

    # 标题：先缩字号，缩到最小仍放不下就折成两行
    max_w = W - 2 * M
    title_size = 70
    f_title = font(title_size)
    while draw.textlength(data["cn"], font=f_title) > max_w and title_size > 38:
        title_size -= 2
        f_title = font(title_size)
    if draw.textlength(data["cn"], font=f_title) <= max_w:
        txt((M, 762), data["cn"], f_title, color)
    else:
        f_two = font(50)
        lines = split_lines(data["cn"], f_two, max_w, 2, draw)
        y = 762 - 58 * (len(lines) - 1)
        for line in lines:
            txt((M, y), line, f_two, color)
            y += 58
    txt((M, 856), data["jp"], font(25, "Regular"), color)

    c = (color[0], color[1], color[2], 90)
    draw.line([(M, 908), (W - M, 908)], fill=c, width=2)

    f_lab = font(24, "Regular")
    f_val = font(24, "SemiBold")
    rows = [
        ("首播日期", data["start"]),
        ("更新情况", data["update"]),
        ("当前进度", data["progress"]),
        ("制作公司", data["studio"]),
    ]
    y = 934
    for lab, val in rows:
        txt((M, y), lab, f_lab, color)
        txt((M + 160, y), val, f_val, color)
        y += 50

    y += 6
    f_desc = font(23, "Regular")
    for line in wrap(data["desc"], f_desc, W - 2 * M, 3, draw):
        txt((M, y), line, f_desc, color)
        y += 38

    txt((W - M, H - 40), "yuc.wiki + AniList", font(18, "Regular"),
        (color[0], color[1], color[2], 150), anchor="ra")


def render_card(data: dict, cover_bytes: bytes, out_path: str) -> str:
    cover = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
    radius = max(0, round(BLUR_PCT * 0.5))
    bg = cover_fill(cover, W, H)
    # 源图常见 460×649，放大后用 USM 把线条拉回来
    bg = bg.filter(ImageFilter.UnsharpMask(radius=1.2, percent=80, threshold=3))
    if radius:
        bg = bg.filter(ImageFilter.GaussianBlur(radius))
    base = bg.convert("RGBA")

    if data.get("veil"):
        veil = Image.new("RGBA", (W, H), (255, 255, 255, 0))
        dv = ImageDraw.Draw(veil)
        for i in range(320):
            a = int(78 * (i / 320.0) ** 0.8)
            dv.line([(0, 690 + i), (W, 690 + i)], fill=(255, 255, 255, a))
        base = Image.alpha_composite(base, veil)

    # 按文字区亮度中位数分三档：亮 / 混杂 / 暗
    # 按文字实际落点取样：只量每一行字所在的那条带，空白背景不参与
    gray = base.convert("L")
    text_rows = [
        (762, 70), (856, 26),                                   # 中文标题、日文名
        (934, 26), (984, 26), (1034, 26), (1084, 26),           # 首播/更新/进度/制作
        (1140, 24), (1176, 24), (1212, 24),                     # 简介三行
    ]
    bands = []
    all_px = []
    for y0, fh in text_rows:
        y1 = min(H - 40, y0 + int(fh * 1.45))
        tile = gray.crop((M, y0, W - M, y1)).resize((48, 8))
        vals = list(tile.getdata())
        all_px += vals
        vals.sort()
        bands.append(vals[len(vals) // 2])
    all_px.sort()
    median = all_px[len(all_px) // 2]
    spread = max(bands) - min(bands)


    if spread > 70:
        # 上下落差大：中位数会被亮部带偏，一律按中档淡蓝 + 白描边
        mode = "mixed"
        main, shadow = (126, 186, 238, 255), (16, 26, 42, 210)
    elif median > 150:
        # 亮封面：黑字配白色阴影，不描边
        mode = "light"
        main, shadow = (12, 14, 20, 255), (255, 255, 255, 235)
    elif median < 95:
        mode = "dark"
        main, shadow = (255, 255, 255, 255), (214, 220, 232, 215)
    else:
        # 明暗混杂：淡蓝字 + 白描边
        mode = "mixed"
        main, shadow = (126, 186, 238, 255), (16, 26, 42, 210)

    if data.get("main_color"):
        main = tuple(data["main_color"])
    if data.get("shadow_color"):
        shadow = tuple(data["shadow_color"])

    def shift(layer, dx, dy):
        out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        out.paste(layer, (dx, dy))
        return out

    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layout(ImageDraw.Draw(sh), data, shadow, status_fill=None, draw_status=False)
    # 白色阴影不做位移，改成中心扩散的大半径柔光
    base = Image.alpha_composite(base, sh.filter(ImageFilter.GaussianBlur(9)))
    base = Image.alpha_composite(base, sh.filter(ImageFilter.GaussianBlur(4)))

    # 明暗混杂：淡蓝字 + 白描边
    outline = (255, 255, 255, 235) if mode == "mixed" else None
    if data.get("no_stroke"):
        outline = None
    finished = data.get("finished", False)
    cap = (24, 26, 32, 245) if finished else (206, 58, 68, 245)
    txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layout(ImageDraw.Draw(txt), data, main, status_fill=cap, stroke=outline)
    base = Image.alpha_composite(base, txt)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    base.convert("RGB").save(out_path, quality=94)
    return out_path
