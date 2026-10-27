"""番剧查询插件：/list 周报、季度表、番剧卡片。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import date, datetime

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from . import sources
from .render import RENDER_VERSION, render_card
from .render_grid import render_grid
from .render_list import render_schedule
from .sources import anilist, yuc
from .sources.covers import batch_covers
from .sources.image_cache import fetch_image

USAGE = (
    "用法：\n"
    "· /list —— 本周节目单\n"
    "· /list 2026秋 —— 该季度新番表\n"
    "· /listpic —— 本周缩略图网格\n"
    "· /listpic 2026秋 —— 该季度缩略图\n"
    "· /listpic 番剧名 —— 番剧信息卡片"
)

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugin_data", "astrbot_plugin_anime_list",
)
CARD_DIR = os.path.join(DATA_DIR, "cards")
COVER_DIR = os.path.join(DATA_DIR, "covers")
WEEK_NAMES = ["一", "二", "三", "四", "五", "六", "日"]


def _status_text(media: dict) -> tuple[str, bool]:
    status = (media.get("status") or "").upper()
    if status == "FINISHED":
        return "已完结", True
    if status == "NOT_YET_RELEASED":
        return "未播出", False
    return "连载中", False


def _fmt_date(d: dict | None) -> str:
    if not d or not d.get("year"):
        return "未定"
    month = d.get("month") or 1
    day = d.get("day") or 1
    return f"{d['year']}-{month:02d}-{day:02d}"


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SEASON_ARG_RE = re.compile(
    r"第\s*([0-9]+|[一二三四五六七八九十])\s*[期季]|season\s*([0-9]+)|(?:^|\s)s([0-9]+)$", re.I)


def _parse_season_arg(arg: str) -> tuple[str, int]:
    """把「侦探已死 第一季」拆成（侦探已死，1）；没写季数就返回 0。"""
    m = _SEASON_ARG_RE.search(arg)
    if not m:
        return arg.strip(), 0
    raw = m.group(1) or m.group(2) or m.group(3) or ""
    no = int(raw) if raw.isdigit() else _CN_NUM.get(raw, 0)
    rest = (arg[:m.start()] + " " + arg[m.end():]).strip(" ,，·-")
    return (rest or arg.strip()), no


_SEASON_NAMES = {"冬": 1, "春": 4, "夏": 7, "秋": 10}


_ALIAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cn_alias.json")
_alias_cache: dict[str, str] | None = None


def _alias_of(name: str) -> str:
    """yuc 的长篇区块只有中文名，用这张表换成 AniList 认得的日文名。"""
    global _alias_cache
    if _alias_cache is None:
        try:
            with open(_ALIAS_PATH, encoding="utf-8") as f:
                _alias_cache = json.load(f)
        except Exception:
            _alias_cache = {}
    return _alias_cache.get(name.strip(), "")


def _sig(*parts) -> str:
    """内容签名：数据或渲染版本一变，文件名就变，自然淘汰旧图。"""
    blob = json.dumps([parts, RENDER_VERSION], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:12]


def _normalize_season(arg: str) -> str:
    """202603 这种写法的年月份，归一到 yuc 真正的季度页名（202601 等）。"""
    try:
        year, month = int(arg[:4]), int(arg[4:])
        if 1 <= month <= 12:
            return yuc.season_of(date(year, month, 1))
    except Exception:
        pass
    return arg


def _season_code(arg: str) -> str:
    """把「2026春」或「202610」解析成季度页名；不是季度参数就返回空串。"""
    text = (arg or "").strip()
    m = re.fullmatch(r"(\d{4})\s*年?\s*([春夏秋冬])\s*季?", text)
    if m:
        return f"{m.group(1)}{_SEASON_NAMES[m.group(2)]:02d}"
    if re.fullmatch(r"\d{6}", text):
        return _normalize_season(text)
    return ""


def _clean_desc(text: str, limit: int = 220) -> str:
    text = (text or "").replace("<br>", " ").replace("<i>", "").replace("</i>", "")
    text = re.sub(r"<[^>]+>", "", text).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text or "暂无简介"


class AnimeListPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.session: aiohttp.ClientSession | None = None

    async def terminate(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    # ------------------------------------------------------------ 数据
    async def _sess(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _season_items(self, season: str) -> list[dict]:
        try:
            return await yuc.fetch_season(season, await self._sess())
        except Exception as exc:
            logger.warning(f"[AnimeList] 抓取 {season} 失败：{exc}")
            return []

    async def _week_items(self) -> tuple[list[dict], str]:
        """本周在播：当季 + 上季，只保留本月与上月开播的番。"""
        today = date.today()
        season = yuc.season_of(today)
        cur = await self._season_items(season)
        prev = await self._season_items(yuc.prev_season(season))
        last_month = 12 if today.month == 1 else today.month - 1
        months = {today.month, last_month}
        merged = yuc.this_week([cur, prev])
        # 长篇/年番没有月份（month=0），无条件保留
        kept = [it for it in merged if it["month"] in months or it.get("week") == "*"]
        return await self._drop_finished(kept), season

    async def _drop_finished(self, items: list[dict]) -> list[dict]:
        """踢掉已完结的番：只核实上个月开播的，本月刚开播的必然还在连载。"""
        this_month = date.today().month
        risky = [it for it in items if it["month"] != this_month and it.get("jp")]
        if not risky:
            return items
        done: set[str] = set()
        for it in risky:
            try:
                media = await anilist.find_detail(await self._sess(), jp=it["jp"], cn=it["cn"])
            except Exception:
                continue
            if media and (media.get("status") or "").upper() == "FINISHED":
                done.add(it["jp"] or it["cn"])
        if done:
            logger.info(f"[AnimeList] 剔除已完结 {len(done)} 部：{sorted(done)}")
        return [it for it in items if (it["jp"] or it["cn"]) not in done]

    # ------------------------------------------------------------ 文本
    def _week_text(self, items: list[dict], season: str) -> list[str]:
        start, end = yuc.week_range()
        long_cnt = sum(1 for it in items if it.get("week") == "*")
        head = (f"本周连载（{start.month}/{start.day} - {end.month}/{end.day}）\n"
                f"共 {len(items)} 部在播" + (f"，其中长篇 {long_cnt} 部" if long_cnt else ""))
        chunks: list[str] = []
        buf: list[str] = [head]

        # 长篇 / 年番：每天都播，单独放前面
        longs = [it for it in items if it.get("week") == "*"]
        if longs:
            buf.append("\n长篇／年番")
            for it in longs:
                slot = f" {it['slot']}" if it["slot"] else ""
                buf.append(f"　{it['cn']}{slot}")

        for w in WEEK_NAMES:
            group = [it for it in items if it["week"] == w]
            if not group:
                continue
            block = [f"\n周{w}"]
            for it in group:
                slot = f" {it['slot']}" if it["slot"] else ""
                block.append(f"　{it['cn']}{slot}")
            if len("\n".join(buf + block)) > 800 and len(buf) > 1:
                chunks.append("\n".join(buf))
                buf = block
            else:
                buf += block
        if len(buf) > 1:
            chunks.append("\n".join(buf))
        return chunks

    def _season_text(self, items: list[dict], season: str) -> list[str]:
        header = f"{yuc.season_label(season)}　共 {len(items)} 部"
        chunks: list[str] = []
        buf = [header]
        count = 0
        for it in items:
            buf.append(f"· 周{it['week']} {it['day']}日 {it['cn']}")
            count += 1
            if count >= 18:
                chunks.append("\n".join(buf))
                buf, count = [], 0
        if buf:
            chunks.append("\n".join(buf))
        return chunks

    # ------------------------------------------------------------ 卡片
    async def _build_card(self, keyword: str) -> str | None:
        season = yuc.season_of()
        keyword, want_season = _parse_season_arg(keyword)
        # 两季合并后统一打分，取相似度最高的一条
        pool = (await self._season_items(season)) + (await self._season_items(yuc.prev_season(season)))
        found = yuc.search(pool, keyword)
        # 「侦探已死2」这种名字后面直接跟数字的写法，按季数处理
        if not want_season and keyword and keyword[-1].isdigit():
            m = re.match(r"^(.*?)(\d+)$", keyword)
            base_kw = m.group(1).strip() if m else ""
            num = int(m.group(2)) if m else 0
            if base_kw and 1 <= num <= 9:
                in_yuc = bool(yuc.search(pool, base_kw))
                if in_yuc or _alias_of(base_kw):
                    keyword, want_season = base_kw, num
                    found = yuc.search(pool, keyword) if in_yuc else []
        hit = found[0] if found else None
        hit_season = anilist.title_season((hit["jp"] or hit["cn"])) if hit else 0

        # 长篇/年番在 yuc 没有日文名，先查中文别名表
        if hit is not None and not hit.get("jp"):
            hit["jp"] = _alias_of(hit["cn"])
        if not hit and not want_season:
            alias_jp = _alias_of(keyword)

        media = None
        # 明确指定第几季时，先用基础名去跨季检索
        if want_season:
            if hit and hit.get("jp"):
                base = anilist.base_title(hit["jp"])
            else:
                base = _alias_of(keyword) or keyword   # 往年番走别名表
            media = await anilist.find_season(await self._sess(), base, want_season)
        elif hit and hit_season > 1:
            # 没写季数时默认回到第一季
            base = anilist.base_title(hit["jp"] or "")
            if base:
                media = await anilist.find_season(await self._sess(), base, 1)
        if not media and hit:
            media = await anilist.find_detail(await self._sess(), jp=hit["jp"], cn=hit["cn"],
                                              year_hint=int(season[:4]))
        if not media:
            alias_jp = _alias_of(keyword)
            if alias_jp:
                media = await anilist.find_detail(await self._sess(), jp=alias_jp, cn=keyword)
        if not media:
            media = await anilist.find_detail(await self._sess(), jp=keyword, cn=keyword)
        if not media:
            return None

        cover_url = (media.get("coverImage") or {}).get("extraLarge")
        if not cover_url:
            return None

        status_text, finished = _status_text(media)
        episodes = media.get("episodes")
        nxt = media.get("nextAiringEpisode") or {}
        if nxt.get("episode"):
            current = max(0, int(nxt["episode"]) - 1)
        elif finished and episodes:
            current = episodes
        else:
            current = 0

        # 媒体季数与当季条目不一致时，不能沿用当季的中文名和档期
        media_season = anilist.title_season((media.get("title") or {}).get("native") or "")
        mismatch = bool(hit and hit_season != media_season)
        if mismatch:
            cn_name = anilist.base_title(hit["cn"]) or hit["cn"]
            week = slot = ""
        else:
            cn_name = hit["cn"] if hit else ((media.get("title") or {}).get("native") or keyword)
            week = f"周{hit['week']}" if hit else ""
            slot = f" {hit['slot']}" if hit and hit["slot"] else ""

        start = media.get("startDate") or {}
        update = f"{week}{slot} · 共 {episodes} 集" if episodes else (f"{week}{slot}".strip() or "档期未定")
        if mismatch:
            update = f"共 {episodes} 集" if episodes else "档期未定"
        progress = f"已更新 {current} / {episodes} 集" if episodes else f"已更新 {current} 集"
        if finished:
            progress = f"已完结 {episodes or '?'} 集"
        elif nxt.get("airingAt"):
            nxt_day = datetime.fromtimestamp(int(nxt["airingAt"]))
            progress += f"（下一集 {nxt_day.month}/{nxt_day.day}）"

        studios = [n["name"] for n in ((media.get("studios") or {}).get("nodes") or [])]
        data = {
            "cn": cn_name,
            "jp": (media.get("title") or {}).get("native") or "",
            "genre": " · ".join((media.get("genres") or [])[:3]) or (hit["type"] if hit else ""),
            "status_text": status_text,
            "finished": finished,
            "start": _fmt_date(start),
            "update": update,
            "progress": progress,
            "studio": studios[0] if studios else "-",
            "desc": _clean_desc(media.get("description") or ""),
        }
        os.makedirs(CARD_DIR, exist_ok=True)
        out = os.path.join(CARD_DIR, f"card_{media.get('id')}_{_sig(data, cover_url)}.jpg")
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return out  # 同样的数据、同样的样式，直接复用
        cover = await fetch_image(await self._sess(), cover_url, COVER_DIR, anilist.UA)
        if not cover:
            return None
        await asyncio.to_thread(render_card, data, cover, out)
        return out

    @filter.command("listpic")
    async def anime_list_pic(self, event: AstrMessageEvent):
        """番剧缩略图网格与详情卡片。"""
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/?listpic\s*", "", raw, flags=re.I).strip()

        if not arg:
            items, season = await self._week_items()
            if not items:
                yield event.plain_result("这周没抓到番剧数据，可能是 yuc.wiki 暂时不可用 (｡>﹏<｡)")
                return
            start, end = yuc.week_range()
            out = await self._grid(items, "本周连载",
                                   f"{start:%m/%d} - {end:%m/%d}　共 {len(items)} 部在播", "week")
            yield event.chain_result([Image.fromFileSystem(out)])
            return

        season_code = _season_code(arg)
        if season_code:
            items = await self._season_items(season_code)
            if not items:
                yield event.plain_result(f"{season_code} 这一季还没有数据呢")
                return
            out = await self._grid(items, yuc.season_label(season_code), f"共 {len(items)} 部", f"season_{season_code}")
            yield event.chain_result([Image.fromFileSystem(out)])
            return

        path = await self._build_card(arg)
        if not path:
            yield event.plain_result(f"没找到「{arg}」这部番 (｡>﹏<｡)\n换个译名或者用日文名试试？")
            return
        yield event.chain_result([Image.fromFileSystem(path)])

    # ------------------------------------------------------------ 缩略图网格
    async def _grid(self, items: list[dict], title: str, subtitle: str, tag: str) -> str:
        out = os.path.join(CARD_DIR, f"grid_{tag}_{_sig([it['cn'] for it in items], title, subtitle)}.jpg")
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return out

        sess = await self._sess()
        # 长篇/年番没有日文名，先用中文别名表补上，否则批量取封面会整批跳过
        for it in items:
            if not it.get("jp"):
                alias = _alias_of(it["cn"])
                if alias:
                    it["jp"] = alias
        mapping = await batch_covers(sess, [it["jp"] for it in items if it["jp"]])
        sem = asyncio.Semaphore(8)

        async def grab(url: str) -> bytes | None:
            async with sem:
                return await fetch_image(sess, url, COVER_DIR, anilist.UA)

        keys = list(mapping)
        blobs = await asyncio.gather(*(grab(mapping[k]) for k in keys)) if keys else []
        covers = {k: b for k, b in zip(keys, blobs) if b}
        await asyncio.to_thread(render_grid, items, covers, title, subtitle, out)
        return out

    # ------------------------------------------------------------ 指令
    @filter.command("list")
    async def anime_list(self, event: AstrMessageEvent):
        """番剧周报、季度表与详情卡片。"""
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/?list\s*", "", raw, flags=re.I).strip()

        if not arg:
            items, season = await self._week_items()
            if not items:
                yield event.plain_result("这周没抓到番剧数据，可能是 yuc.wiki 暂时不可用 (｡>﹏<｡)")
                return
            start, end = yuc.week_range()
            subtitle_txt = f"{start:%m/%d} - {end:%m/%d}　共 {len(items)} 部在播"
            out = os.path.join(CARD_DIR, f"week_{_sig([it['cn'] for it in items], subtitle_txt)}.jpg")
            if not (os.path.exists(out) and os.path.getsize(out) > 1024):
                await asyncio.to_thread(render_schedule, items, "本周连载", subtitle_txt, out)
            yield event.chain_result([Image.fromFileSystem(out)])
            return

        season_code = _season_code(arg)
        if season_code:
            items = await self._season_items(season_code)
            if not items:
                yield event.plain_result(f"{season_code} 这一季还没有数据呢")
                return
            label = yuc.season_label(season_code)
            sub_txt = f"共 {len(items)} 部"
            out = os.path.join(CARD_DIR, f"season_{season_code}_{_sig([it['cn'] for it in items], sub_txt)}.jpg")
            if not (os.path.exists(out) and os.path.getsize(out) > 1024):
                await asyncio.to_thread(render_schedule, items, label, sub_txt, out)
            yield event.chain_result([Image.fromFileSystem(out)])
            return

        path = await self._build_card(arg)
        if not path:
            yield event.plain_result(f"没找到「{arg}」这部番 (｡>﹏<｡)\n换个译名或者用日文名试试？")
            return
        yield event.chain_result([Image.fromFileSystem(path)])
