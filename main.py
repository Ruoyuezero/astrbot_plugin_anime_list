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
from .sources import anilist, tavily, yuc
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


_INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yuc_index.json")
_index_cache: dict[str, str] | None = None


def _load_index() -> dict[str, str]:
    global _index_cache
    if _index_cache is None:
        try:
            with open(_INDEX_PATH, encoding="utf-8") as f:
                _index_cache = json.load(f)
        except Exception:
            _index_cache = {}
    return _index_cache


def _canonical_cn(jp: str) -> str:
    """由日文名反查索引里的规范中文全名，取最短的一条。"""
    if not jp:
        return ""
    key = jp.replace(" ", "").replace("　", "")
    hits = [(len(cn), cn) for cn, v in _load_index().items()
            if v.replace(" ", "").replace("　", "") == key]
    return min(hits)[1] if hits else ""


def _index_lookup(keyword: str) -> str:
    """兼容旧调用：只取日文名。"""
    return _index_hit(keyword)[1]


def _index_hit(keyword: str) -> tuple[str, str]:
    """在 2019-2026 全量索引里查找，返回 (中文名, 日文名)。"""
    idx = _load_index()
    if not idx:
        return "", ""
    key = (keyword or "").strip()
    if key in idx:
        return key, idx[key]

    def _match(kw: str, strip_particles: bool = False) -> tuple[str, str]:
        if not kw:
            return "", ""
        starts, contains = [], []
        for cn, jp in idx.items():
            n = yuc._norm(cn)
            if strip_particles:
                n = re.sub(r"[的之]", "", n)
            if n.startswith(kw):
                starts.append((len(n), cn, jp))
            elif kw in n:
                contains.append((len(n), cn, jp))
        if starts:
            best = min(starts)
            return best[1], best[2]
        if contains:
            best = min(contains)
            return best[1], best[2]
        import difflib
        best, best_cn, best_score = "", "", 0.0
        for cn, jp in idx.items():
            score = difflib.SequenceMatcher(None, kw, yuc._norm(cn)).ratio()
            if score > best_score:
                best, best_cn, best_score = jp, cn, score
        return (best_cn, best) if best_score >= 0.85 else ("", "")

    kw = yuc._norm(key)
    hit = _match(kw)
    if hit[1]:          # 元组恒为真，必须看是否有日文名
        return hit
    # 简称常省掉「的」「之」这类虚词，去掉再试一次
    kw2 = re.sub(r"[的之]", "", kw)
    if kw2 and kw2 != kw:
        return _match(kw2, strip_particles=True)
    return _match(kw, strip_particles=True)
    return ""


def _title_distance(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, yuc._norm(a), yuc._norm(b)).ratio()


_ALIAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cn_alias.json")
_alias_cache: dict[str, str] | None = None


_CN_TITLE_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugin_data", "astrbot_plugin_anime_list", "cn_title_cache.json")


def _save_cn_title(jp: str, cn: str) -> None:
    """记录日文名对应的常用译名，供下次直接用。"""
    if not jp or not cn:
        return
    try:
        data = {}
        if os.path.exists(_CN_TITLE_CACHE):
            with open(_CN_TITLE_CACHE, encoding="utf-8") as f:
                data = json.load(f)
        if data.get(jp) == cn:
            return
        data[jp] = cn
        os.makedirs(os.path.dirname(_CN_TITLE_CACHE), exist_ok=True)
        with open(_CN_TITLE_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _save_alias(name: str, jp: str) -> None:
    """搜索兜底成功后，把新映射写回 cn_alias.json 并刷新缓存。"""
    global _alias_cache
    name, jp = name.strip(), jp.strip()
    if not name or not jp:
        return
    data = {}
    try:
        with open(_ALIAS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if data.get(name) == jp:
        return
    data[name] = jp
    try:
        with open(_ALIAS_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(data.items(), key=lambda kv: kv[0])), f,
                      ensure_ascii=False, indent=1)
        _alias_cache = data
        logger.info(f"[AnimeList] 别名表新增：{name} -> {jp}")
    except Exception as exc:
        logger.warning(f"[AnimeList] 别名表写入失败：{exc}")


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
        """三步走：映射表 → AniList 直接搜 → Tavily 搜萌娘百科。"""
        keyword, want_season = _parse_season_arg(keyword)

        # 「…2」这种末尾数字当季数，前提是基础名在映射表里
        if not want_season and keyword and keyword[-1].isdigit():
            m = re.match(r"^(.*?)(\d+)$", keyword)
            base_kw = m.group(1).strip() if m else ""
            num = int(m.group(2)) if m else 0
            if base_kw and 1 <= num <= 9 and (_index_hit(base_kw)[1] or _alias_of(base_kw)):
                keyword, want_season = base_kw, num
                map_cn, jp = _index_hit(base_kw)

        sess = await self._sess()
        map_cn, jp = _index_hit(keyword)
        if not jp:
            jp = _alias_of(keyword)
        media = None
        cn_title = ""

        # ① 映射表
        hit_cn = map_cn      # 映射表里的中文名，直接拿来当标题
        if jp:
            base_jp = anilist.base_title(jp)
            if not hit_cn:
                hit_cn = keyword
            # 季数优先看中文名（「第2期」这类标记最可靠），日文名只作补充
            hit_season = anilist.title_season(hit_cn) or anilist.title_season(jp)
            if want_season:
                media = await anilist.find_season(sess, base_jp, want_season)
                if not media:
                    logger.info(f"[AnimeList] {keyword} 第{want_season}季不存在，不出图")
                    return None
            elif hit_season > 1:
                # 没写季数却命中了续作：按约定回到第一季，标题也去掉季数标记
                media = await anilist.find_season(sess, base_jp, 1)
                if media:
                    logger.info(f"[AnimeList] {keyword} 命中续作 {hit_cn}，回退第一季")
                    hit_cn = anilist.base_title(hit_cn) if hit_cn else hit_cn
                    map_cn = hit_cn
            if not media:
                media = await anilist.find_detail(sess, jp=jp, cn=keyword)

        # ② AniList 直接搜
        if not media:
            if want_season:
                media = await anilist.find_season(sess, keyword, want_season)
                if not media:
                    return None       # 指定了这一期但查不到，绝不退回别的季
            else:
                media = await anilist.find_detail(sess, jp=keyword, cn=keyword)

        # ③ Tavily 搜萌娘百科（顺手带回常用译名）
        if not media:
            media, fallback_cn = await self._search_fallback(keyword)
            if fallback_cn:
                cn_title = fallback_cn

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

        update = f"共 {episodes} 集" if episodes else "档期未定"
        progress = f"已更新 {current} / {episodes} 集" if episodes else f"已更新 {current} 集"
        if finished:
            progress = f"已完结 {episodes or '?'} 集"
        elif nxt.get("airingAt"):
            nxt_day = datetime.fromtimestamp(int(nxt["airingAt"]))
            progress += f"（下一集 {nxt_day.month}/{nxt_day.day}）"

        studios = [n["name"] for n in ((media.get("studios") or {}).get("nodes") or [])]
        # 标题：映射表里的中文名优先，其次兜底时记下的常用译名，最后才用用户输入
        if not cn_title:
            cn_title = hit_cn or keyword
        # 标题末尾补上期数：以最终查到的这部为准，用户没写也能对
        media_season = anilist.title_season((media.get("title") or {}).get("native") or "") or want_season or 1
        if media_season > 1 and anilist.title_season(cn_title) <= 1:
            cn_title = f"{cn_title} 第{media_season}期"
        data = {
            "cn": cn_title,
            "jp": (media.get("title") or {}).get("native") or "",
            "genre": " · ".join((media.get("genres") or [])[:3]),
            "status_text": status_text,
            "finished": finished,
            "start": _fmt_date(media.get("startDate")),
            "update": update,
            "progress": progress,
            "studio": studios[0] if studios else "-",
            "desc": _clean_desc(media.get("description") or ""),
        }
        os.makedirs(CARD_DIR, exist_ok=True)
        out = os.path.join(CARD_DIR, f"card_{media.get('id')}_{_sig(data, cover_url)}.jpg")
        done = (media.get("status") or "").upper() == "FINISHED"
        if done and os.path.exists(out) and os.path.getsize(out) > 1024:
            return out
        cover = await fetch_image(await self._sess(), cover_url, COVER_DIR, anilist.UA)
        if not cover:
            return None
        await asyncio.to_thread(render_card, data, cover, out)
        return out

    async def _common_cn(self, native_jp: str, keyword: str) -> str:
        """用萌娘百科查常用中文译名，结果落盘缓存，避免反复搜索。"""
        cache_path = os.path.join(DATA_DIR, "cn_title_cache.json")
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
        if native_jp in cache:
            return cache[native_jp]

        api_key = str(self.config.get("tavily_api_key") or "").strip()
        if not api_key:
            return ""
        try:
            name = await tavily.search_cn_name(await self._sess(), api_key, keyword)
        except Exception:
            return ""
        if name:
            cache[native_jp] = name
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
        return name

    async def _search_fallback(self, keyword: str) -> tuple:
        """索引、别名、直搜都失败时，用搜索服务找日文名，顺手记下常用译名。"""
        api_key = str(self.config.get("tavily_api_key") or "").strip()
        if not api_key:
            return None, ""
        try:
            limit = int(self.config.get("tavily_max_results") or 5)
        except Exception:
            limit = 5

        sess = await self._sess()
        cn_name = ""
        try:
            candidates = await tavily.search_anime_name(sess, api_key, keyword, limit)
        except Exception as exc:
            logger.warning(f"[AnimeList] 搜索兜底失败：{exc}")
            return None, ""
        if not candidates:
            return None, ""

        # 顺手取一次常用译名，供卡片标题使用
        try:
            cn_name = await tavily.search_cn_name(sess, api_key, keyword)
        except Exception:
            cn_name = ""

        for jp in candidates:
            media = await anilist.find_detail(sess, jp=jp, cn=keyword)
            if media:
                logger.info(f"[AnimeList] 搜索兜底命中：{keyword} -> {jp}（译名：{cn_name or '无'}）")
                _save_alias(keyword, jp)
                if cn_name:
                    _save_cn_title(jp, cn_name)
                return media, cn_name
        return None, ""

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
        # 本周连载会随时变化，网格每次重新渲染，不复用旧图
        out = os.path.join(CARD_DIR, f"grid_{tag}_{_sig([it['cn'] for it in items], title, subtitle)}.jpg")

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
