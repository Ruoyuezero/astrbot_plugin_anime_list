"""Tavily 搜索兜底：索引里没有的老番，靠搜索拿官方日文名。"""

from __future__ import annotations

import json
import re

import aiohttp

ENDPOINT = "https://api.tavily.com/search"

# 公认收录番名最全的几个站
SITES = ["zh.moegirl.org.cn", "bangumi.tv", "zh.wikipedia.org",
         "ja.wikipedia.org", "baike.baidu.com"]

# 解析时优先采纳这些站点的结果
PRIORITY_HOSTS = ("moegirl.org.cn", "bangumi.tv")

# 维基 / Bangumi / 萌娘百科里常见的写法
_PATTERNS = [
    # 萌娘百科 / 维基的表格行：「| 原名 | けいおん！ |」
    re.compile(r"\|\s*(?:原名|日文名)\s*\|\s*([^\s|，,。；;]+)"),
    # 行内写法：「原名：xxx」「日语：xxx」
    re.compile(r"(?:日语|日文名|日文原名|原名)\s*[：:]\s*([^\s，,。；;）)】\]|]+)"),
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


def _relevant(item: dict, keyword: str) -> bool:
    """搜索结果必须真的提到用户输入的名字，否则一律不认。"""
    probe = re.sub(r"[^\u4e00-\u9fff]", "", keyword or "")[:4]
    if not probe:
        return True
    text = (item.get("title") or "") + (item.get("content") or "")
    return probe in text


def _candidates(results: list[dict], keyword: str = "") -> list[str]:
    """从搜索结果里挑出可能的日文原名，按可信度排序。"""
    if keyword:
        results = [r for r in results if _relevant(r, keyword)]
    out: list[str] = []

    def add(name: str) -> None:
        name = (name or "").strip(" 「」『』《》，,。")
        if name and _KANA.search(name) and name not in out and len(name) <= 80:
            out.append(name)

    def order_key(item: dict) -> int:
        url = (item.get("url") or "").lower()
        return 0 if any(h in url for h in PRIORITY_HOSTS) else 1

    for item in sorted(results, key=order_key):
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

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    query = f"{keyword} 动画 日文原名"

    # 先只搜萌娘百科，再放宽到其它动漫站，最后不限域名
    for domains in (["zh.moegirl.org.cn"], SITES, None):
        payload = {"query": query, "max_results": max_results, "search_depth": "basic"}
        if domains:
            payload["include_domains"] = domains
        try:
            async with session.post(ENDPOINT, json=payload, headers=headers, timeout=25) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
        except Exception:
            continue
        names = _candidates(data.get("results") or [], keyword)
        if names:
            return names
    return []
