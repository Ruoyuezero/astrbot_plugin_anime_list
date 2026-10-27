"""AniList GraphQL 查询：补首播/完结/集数/封面。"""

from __future__ import annotations

import asyncio
import re

import aiohttp

ENDPOINT = "https://graphql.anilist.co"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

MEDIA_FIELDS = """
  id
  title { native romaji }
  coverImage { extraLarge color }
  bannerImage
  genres
  description(asHtml: false)
  startDate { year month day }
  endDate { year month day }
  episodes
  status
  format
  averageScore
  nextAiringEpisode { episode airingAt }
  studios(isMain: true) { nodes { name } }
"""


class AniListError(Exception):
    pass


async def _post(session: aiohttp.ClientSession, query: str, retries: int = 4) -> dict:
    payload = {"query": query}
    headers = {"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"}
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with session.post(ENDPOINT, json=payload, headers=headers, timeout=20) as resp:
                if resp.status == 429:
                    # 被限流就乖乖退避，越撞等越久
                    await asyncio.sleep(2 * (attempt + 1))
                    last = AniListError("HTTP 429 限流")
                    continue
                if resp.status != 200:
                    last = AniListError(f"AniList HTTP {resp.status}")
                    await asyncio.sleep(1 + attempt)
                    continue
                data = await resp.json()
                if data.get("errors"):
                    raise AniListError(str(data["errors"])[:120])
                return data.get("data") or {}
        except AniListError as exc:
            if "429" not in str(exc):
                raise
            last = exc
            await asyncio.sleep(2 * (attempt + 1))
        except Exception as exc:  # 网络抖动重试
            last = exc
            await asyncio.sleep(1 + attempt)
    raise AniListError(f"AniList 请求失败：{last}")


def _escape(text: str) -> str:
    return text.replace("\\", " ").replace('"', " ")


async def search_media(session: aiohttp.ClientSession, keyword: str, limit: int = 5) -> list[dict]:
    q = (
        '{Page(perPage:%d){media(search:"%s",type:ANIME){id title{native romaji} '
        'startDate{year month day} episodes status format coverImage{extraLarge}}}}' % (limit, _escape(keyword))
    )
    data = await _post(session, q)
    return ((data.get("Page") or {}).get("media")) or []


async def media_detail(session: aiohttp.ClientSession, media_id: int) -> dict:
    data = await _post(session, "{Media(id:%d){%s}}" % (media_id, MEDIA_FIELDS))
    media = data.get("Media")
    if not media:
        raise AniListError("AniList 没有返回详情")
    return media


async def find_detail(
    session: aiohttp.ClientSession,
    *,
    jp: str = "",
    cn: str = "",
    year_hint: int | None = None,
) -> dict | None:
    """优先用日文原名检索，再退回中文名；返回最匹配的一条详情。"""
    for kw in [jp, cn]:
        if not kw:
            continue
        try:
            cands = await search_media(session, kw)
        except AniListError:
            continue
        if not cands:
            continue
        best = cands[0]
        if year_hint:
            same_year = [c for c in cands if (c.get("startDate") or {}).get("year") == year_hint]
            if same_year:
                best = same_year[0]
        try:
            return await media_detail(session, int(best["id"]))
        except AniListError:
            continue
    return None


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           # 日文番名常用旧字形表示季数
           "弐": 2, "参": 3, "肆": 4, "伍": 5, "陸": 6, "漆": 7, "捌": 8, "玖": 9}
_SEASON_RE = re.compile(
    r"season\s*(\d+)"
    r"|第\s*([0-9]+|[一二三四五六七八九十弐参肆伍陸漆捌玖])\s*[期季]"
    r"|(\d+)(?:st|nd|rd|th)\s*season"
    r"|\s([弐参肆伍陸漆捌玖])\s*$",   # 「夏目友人帳 漆」这种旧字形季数
    re.I)


def title_season(title: str) -> int:
    """从标题里读第几季，读不到就算第 1 季。"""
    if not title:
        return 1
    # 日文续作常用写法：～そのに～（第二部）、Ⅱ / Ⅲ 这类罗马数字
    if re.search(r"そのに|その弐", title):
        return 2
    if re.search(r"Ⅱ", title):
        return 2
    if re.search(r"Ⅲ", title):
        return 3

    m = _SEASON_RE.search(title)
    if not m:
        return 1
    raw = m.group(1) or m.group(2) or m.group(3) or m.group(4) or "1"
    if raw.isdigit():
        return int(raw)
    return _CN_NUM.get(raw, 1)


def base_title(title: str) -> str:
    """去掉季数标记，得到可以跨季检索的基础名。"""
    if not title:
        return ""
    cleaned = _SEASON_RE.sub(" ", title)
    cleaned = re.sub(r"[〜～][^〜～]{0,20}[〜～]\s*$", " ", cleaned)   # 「〜そのに(2)〜」这类副标题后缀
    cleaned = re.sub(r"\(\s*\d+\s*\)", " ", cleaned)              # 残留的 (2)
    return re.sub(r"\s+", " ", cleaned).strip(" -·・〜～")


async def find_season(session: aiohttp.ClientSession, base: str, season_no: int) -> dict | None:
    """在 AniList 里按基础名搜一批，挑出指定季数的那条。"""
    if not base:
        return None
    try:
        cands = await search_media(session, base, limit=12)
    except AniListError:
        return None
    if not cands:
        return None

    def year_of(c: dict) -> int:
        return (((c.get("startDate") or {}).get("year")) or 9999)

    matches = [c for c in cands if title_season((c.get("title") or {}).get("native") or "") == season_no
               or title_season((c.get("title") or {}).get("romaji") or "") == season_no]
    if not matches:
        return None      # 指定了这一季却没有对应条目，绝不拿别的季凑数
    # 同季常常还挂着特别篇/OVA，优先正片 TV，避免只拿到 1 集的衍生
    tv_only = [c for c in matches if (c.get("format") or "").upper() == "TV"]
    pool = tv_only or matches
    pool.sort(key=year_of)
    pick = pool[0] if season_no == 1 else pool[-1]
    try:
        return await media_detail(session, int(pick["id"]))
    except AniListError:
        return None
