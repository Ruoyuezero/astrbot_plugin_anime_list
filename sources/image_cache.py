"""图片磁盘缓存：同一个 URL 只下载一次。"""

from __future__ import annotations

import hashlib
import os

import aiohttp


def _path(cache_dir: str, url: str) -> str:
    name = hashlib.md5(url.encode("utf-8")).hexdigest()[:20]
    return os.path.join(cache_dir, name + ".img")


async def fetch_image(
    session: aiohttp.ClientSession,
    url: str,
    cache_dir: str,
    user_agent: str = "",
    timeout: int = 15,
) -> bytes | None:
    """优先读磁盘缓存，没有才下载并落盘。"""
    if not url:
        return None
    path = _path(cache_dir, url)
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            pass

    headers = {"User-Agent": user_agent} if user_agent else {}
    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
    except Exception:
        return None

    if len(data) > 1024:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except Exception:
            pass
    return data
