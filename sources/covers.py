"""批量取封面：串行 + 候选名 + 本地缓存，避开 AniList 限流。"""

from __future__ import annotations

import asyncio
import json
import os
import re

import aiohttp

from .anilist import AniListError, base_title, search_media

CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "plugin_data", "astrbot_plugin_anime_list", "cover_cache.json",
)

_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def _save() -> None:
    if _cache is None:
        return
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _variants(name: str) -> list[str]:
    """生成候选检索词：原名、去季数、去符号、去空格、首段。"""
    out: list[str] = []

    def add(s: str) -> None:
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)

    add(name)
    add(base_title(name))
    add(re.sub(r"[★☆♪◆◇・\[\]【】()（）!！?？~～\s]+", " ", name).strip())
    add(re.sub(r"[★☆♪◆◇\[\]【】()（）!！?？~～]+", "", name).strip())
    add(name.split()[0] if " " in name else "")
    return out


async def batch_covers(
    session: aiohttp.ClientSession,
    names: list[str],
    delay: float = 0.5,
) -> dict[str, str]:
    """返回 {原名: 封面URL}。确认无图的才写空值，请求失败不写，下次还能补。"""
    cache = _load()
    result: dict[str, str] = {}
    clean = list(dict.fromkeys(n for n in names if n))
    dirty = False

    for name in clean:
        if name in cache:
            if cache[name]:
                result[name] = cache[name]
            continue

        url = ""
        confirmed = False
        for kw in _variants(name):
            try:
                cands = await search_media(session, kw, limit=1)
            except AniListError:
                break  # 失败就放弃这轮，下次再来
            if cands:
                url = (cands[0].get("coverImage") or {}).get("extraLarge") or ""
                confirmed = True
                break
            await asyncio.sleep(delay)
        else:
            confirmed = True  # 所有候选都查过且没结果

        if confirmed:
            cache[name] = url
            dirty = True
        if url:
            result[name] = url
        await asyncio.sleep(delay)

    if dirty:
        _save()
    return result
