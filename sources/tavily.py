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


# 「| 原名 | xxx |」「原名：xxx」这类明确标注，纯汉字也信任（如 夏目友人帳、鬼滅の刃）
_EXPLICIT = [
    re.compile(r"\|\s*(?:原名|日文名)\s*\|\s*([^\s|，,。；;]+)"),
    re.compile(r"(?:日语|日文名|日文原名|原名)\s*[：:]\s*([^\s，,。；;）)】\]|]+)"),
]


def _pick_jp(text: str, allow_kana_only: bool = False) -> str:
    """allow_kana_only=True 时只接受含假名的候选（用于标题、摘要这类不可靠来源）。"""
    for pat in _EXPLICIT + _PATTERNS[1:]:
        m = pat.search(text)
        if m:
            cand = m.group(1).strip()
            if allow_kana_only and not _KANA.search(cand):
                continue
            return cand
    return ""


def _relevant(item: dict, keyword: str) -> bool:
    """只认标题里真的出现用户输入名字的结果，正文太长不足为凭。"""
    probe = re.sub(r"[^\u4e00-\u9fff0-9]", "", keyword or "")[:6]   # 保留数字，「打了300年」才认得出
    if not probe:
        return False
    return probe in (item.get("title") or "")


def _candidates(results: list[dict], keyword: str = "") -> list[str]:
    """从搜索结果里挑出可能的日文原名，按可信度排序。"""
    if keyword:
        results = [r for r in results if _relevant(r, keyword)]
    out: list[str] = []

    def add(name: str, require_kana: bool = True) -> None:
        name = (name or "").strip(" 「」『』《》，,。")
        if not name or name in out or len(name) > 80:
            return
        if require_kana and not _KANA.search(name):
            return
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
        # 正文里的「原名」字段最可靠，允许纯汉字
        add(_pick_jp(item.get("content") or ""), require_kana=False)
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


_CN_PATTERNS = [
    re.compile(r"\|\s*译名\s*\|\s*([^\s|，,。；;！!？?]+)"),
    re.compile(r"(?:译名|中文名)\s*[：:]\s*([^\s，,。；;！!？?]+)"),
]


def _pick_cn(text: str, keyword: str = "") -> str:
    """从页面里取官方 / 常用中文译名。"""
    for pat in _CN_PATTERNS:
        for m in pat.finditer(text):
            cand = m.group(1).strip(" 「」『』《》，,。")
            if not cand or not re.search(r"[\u4e00-\u9fff]", cand):
                continue
            if keyword and keyword[:3] not in cand and cand[:3] not in keyword:
                continue
            return cand
    return ""


async def search_cn_name(
    session: aiohttp.ClientSession,
    api_key: str,
    keyword: str,
    max_results: int = 5,
) -> str:
    """查常用中文译名，优先萌娘百科。"""
    if not api_key or not keyword:
        return ""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    for domains in (["zh.moegirl.org.cn"], SITES):
        payload = {"query": f"{keyword} 动画", "max_results": max_results,
                   "search_depth": "basic", "include_domains": domains}
        try:
            async with session.post(ENDPOINT, json=payload, headers=headers, timeout=25) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
        except Exception:
            continue
        for item in (data.get("results") or []):
            if not _relevant(item, keyword):
                continue
            # 站点条目的标题通常就是常用译名，去掉「- 萌娘百科」这类后缀
            raw_title = item.get("title") or ""
            title = re.split(r"\s*[-—–]\s*(?:维基百科|萌娘百科|Bangumi|百度百科|Moegirl).*$", raw_title)[0].strip()
            title = title.split(" - ")[0].strip()
            if title and re.search(r"[\u4e00-\u9fff]", title):
                return title
            name = _pick_cn(item.get("content") or "", keyword)
            if name:
                return name
    return ""
