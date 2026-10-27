"""Tavily 搜索兜底：索引里没有的老番，靠搜索拿官方日文名。"""

from __future__ import annotations

import json
import re

import aiohttp

ENDPOINT = "https://api.tavily.com/search"

# 维基 / Bangumi / 萌娘百科里常见的写法
_PATTERNS = [
    re.compile(r"(?:日语|日文名|日文原名|原名|日文)[：:]\s*([^\s，,。；;）)】\]]+)"),
    re.compile(r"『([^』]{2,60})』"),
]

# 含假名（平假名 / 片假名）才算日文名
_KANA = re.compile(r"[\u3040-\u30ff]")


def _pick_jp(text: str) -> str:
    for pat in _PATTERNS:
        m = pat.search(text)
        if m:
            cand = m.group(1).strip()
            if _KANA.search(cand):
                return cand
    return ""


def _candidates(results: list[dict]) -> list[str]:
    """从搜索结果里挑出可能的日文原名，按可信度排序。"""
    out: list[str] = []

    def add(name: str) -> None:
        name = (name or "").strip(" 「」『』《》，,。")
        if name and _KANA.search(name) and name not in out and len(name) <= 80:
            out.append(name)

    for item in results:
        title = item.get("title") or ""
        # 标题整体就是日文的情况（如日文维基、Bangumi）
        if _KANA.search(title) and not re.search(r"[\u4e00-\u9fff]{4,}", title.split(" - ")[0]):
            add(title.split(" - ")[0])
        add(_pick_jp(title))
        add(_pick_jp(item.get("snippet") or ""))
        add(_pick_jp(item.get("content") or ""))
    return out


async def search_anime_name(
    session: aiohttp.ClientSession,
    api_key: str,
    keyword: str,
    max_results: int = 5,
) -> list[str]:
    """返回若干候选日文名，交给 AniList 逐个验证。"""
    if not api_key or not keyword:
        return []

    payload = {
        "api_key": api_key,
        "query": f"{keyword} 动画 日文原名",
        "max_results": max_results,
        "search_depth": "basic",
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        async with session.post(ENDPOINT, json=payload, headers=headers, timeout=25) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception:
        return []

    return _candidates(data.get("results") or [])
