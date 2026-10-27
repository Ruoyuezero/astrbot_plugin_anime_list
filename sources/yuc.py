"""yuc.wiki 新番表解析。"""

from __future__ import annotations

import difflib
import re
import unicodedata
from datetime import date, timedelta

import aiohttp
from bs4 import BeautifulSoup

BASE = "https://yuc.wiki/{season}/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

_KANA_RE = re.compile(r"[\u3040-\u30ff]")
TIME_RE = re.compile(r"(\d{1,2})/(\d{1,2})\s*周([一二三四五六日])\s*(早间|上午|下午|晚间|深夜|凌晨)?")
WEEK_ORDER = ["一", "二", "三", "四", "五", "六", "日"]

_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL = 1800  # 秒


def season_of(d: date | None = None) -> str:
    """返回该日期所属季度的页名，如 202610。"""
    d = d or date.today()
    month = ((d.month - 1) // 3) * 3 + 1
    return f"{d.year}{month:02d}"


def prev_season(season: str) -> str:
    year, month = int(season[:4]), int(season[4:])
    month -= 3
    if month < 1:
        year -= 1
        month += 12
    return f"{year}{month:02d}"


def season_label(season: str) -> str:
    names = {1: "冬", 4: "春", 7: "夏", 10: "秋"}
    return f"{season[:4]}年{names.get(int(season[4:]), '')}季（{season}）"


async def fetch_season(season: str, session: aiohttp.ClientSession) -> list[dict]:
    """抓取并解析某一季的番剧列表。"""
    import time

    hit = _cache.get(season)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    async with session.get(BASE.format(season=season), headers={"User-Agent": UA}) as resp:
        resp.raise_for_status()
        html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for tbl in soup.find_all("table"):
        node = tbl.find(string=TIME_RE)
        if not node:
            continue
        m = TIME_RE.search(node.strip())
        lines = [x.strip() for x in tbl.get_text("|", strip=True).split("|") if x.strip()]
        if not lines:
            continue

        # yuc 用专门的 class 区分中文名与日文名，直接读，不靠猜
        cn_title, jp_title = "", ""
        p_cn = tbl.select_one("p.title_cn_r")
        p_jp = tbl.select_one("p.title_jp_r")
        if p_cn is not None:
            cn_title = p_cn.get_text("", strip=True)
        if p_jp is not None:
            jp_title = p_jp.get_text("", strip=True)
        if not cn_title:
            title_td = tbl.select_one("td.date_title_")
            if title_td is not None:
                cn_title = title_td.get_text("", strip=True)
        # 旧版结构没有那两个 class：前几行按字符特征分组，含假名/字母的归日文，其余归中文
        def _is_jp(x: str) -> bool:
            return bool(_KANA_RE.search(x) or re.search(r"[A-Za-z]", x))

        if not jp_title:
            # 优先取含假名的真日文名，其次才是含字母的（避免「第4期 Part.1」这种抢先）
            pick = next(((i, x) for i, x in enumerate(lines[1:5], start=1) if _KANA_RE.search(x)), None)
            if pick is None:
                pick = next(((i, x) for i, x in enumerate(lines[1:5], start=1)
                             if re.search(r"[A-Za-z]", x)), None)
            for i, cand in ([pick] if pick else []):
                if True:
                    jp_title = cand
                    # 日文名也可能折行，把紧随其后的日文行并回来
                    for nxt in lines[i + 1:i + 3]:
                        if _is_jp(nxt) and "改编" not in nxt and "原创" not in nxt:
                            jp_title += " " + nxt
                        else:
                            break
                    break
        if not cn_title:
            cn_title = lines[0]
        # 中文名可能折成多行，把紧随其后的非日文行并回来
        for cand in lines[1:5]:
            if _is_jp(cand) or "改编" in cand or "原创" in cand:
                break
            if cand != cn_title:
                cn_title += cand
        img = tbl.find("img")
        if img is None and tbl.parent is not None:
            # 封面图常挂在外层 div 的姊妹 table 里
            img = tbl.parent.find("img")
        src = ""
        if img is not None:
            src = (img.get("data-src") or img.get("src") or "").strip()
            if "d06ad4b201012067db7d59c9dedfadeb6da75cf3" in src:
                src = ""  # 站点占位图
        items.append({
            "cn": cn_title or lines[0],
            "jp": jp_title,
            "type": next((x for x in lines if "改编" in x or "原创" in x), ""),
            "tags": lines[3] if len(lines) > 3 else "",
            "month": int(m.group(1)),
            "day": int(m.group(2)),
            "week": m.group(3),
            "slot": m.group(4) or "",
            "img": src,
            "season": season,
        })

    # 长篇 / 年番区块：写法是「08:30~」加「(长篇)」，没有 M/D 周几，单独收
    for div in soup.select("div.div_date"):
        p_time = div.find("p", class_="imgtext5")
        if not p_time:
            continue
        p_tag = div.find("p", class_="imgep")
        block = div.parent
        title_td = block.select_one("td.date_title_") if block else None
        if title_td is None:
            continue
        cn = title_td.get_text("", strip=True)
        if not cn or any(it["cn"] == cn for it in items):
            continue
        img = div.find("img")
        src = ""
        if img is not None:
            src = (img.get("data-src") or img.get("src") or "").strip()
            if "d06ad4b201012067db7d59c9dedfadeb6da75cf3" in src:
                src = ""
        items.append({
            "cn": cn,
            "jp": "",
            "type": (p_tag.get_text(strip=True) if p_tag else "长篇"),
            "tags": "",
            "month": 0,
            "day": 0,
            "week": "*",          # 每天都播，不归到某个星期
            "slot": p_time.get_text(strip=True).rstrip("~～"),
            "img": src,
            "season": season,
        })

    _cache[season] = (time.time(), items)
    return items


def week_range(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def this_week(seasons: list[list[dict]]) -> list[dict]:
    """合并多个季度的番，按星期排序，同一部去重。"""
    seen: set[str] = set()
    out: list[dict] = []
    for items in seasons:
        for it in items:
            key = it["jp"] or it["cn"]
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
    out.sort(key=lambda x: (WEEK_ORDER.index(x["week"]) if x["week"] in WEEK_ORDER else -1,
                            x["day"], x["cn"]))
    return out


def _norm(text: str) -> str:
    """归一化：全角转半角、去标点空格、统一小写，方便模糊比对。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\s·・:：\-—–~～!！?？,，.。'\"“”‘’()（）\[\]【】#＃]+", "", text)
    return text.lower()


def _score(item: dict, kw: str) -> float:
    if not kw:
        return 0.0
    best = 0.0
    for field in (_norm(item.get("cn", "")), _norm(item.get("jp", ""))):
        if not field:
            continue
        if field == kw:
            s = 100.0
        elif field.startswith(kw):
            s = 92.0 - min(15.0, (len(field) - len(kw)) * 0.9)
        elif kw in field:
            s = 84.0 - min(12.0, (len(field) - len(kw)) * 0.8)
        else:
            s = difflib.SequenceMatcher(None, kw, field).ratio() * 80.0
        if s > best:
            best = s
    # 完整番剧条目优先，港台/重播等分区块降权
    if "动画" in (item.get("type") or "") or "改编" in (item.get("type") or ""):
        best += 5.0
    return best


def search(items: list[dict], keyword: str, threshold: float = 45.0) -> list[dict]:
    """按相似度从高到低返回候选，取不到足够像的就返回空。"""
    kw = _norm(keyword)
    if not kw:
        return []
    scored = [(_score(it, kw), it) for it in items]
    scored = [x for x in scored if x[0] >= threshold]
    scored.sort(key=lambda x: (-x[0], len(x[1]["cn"]), x[1]["day"]))
    return [it for _, it in scored]
