# -*- coding: utf-8 -*-
"""页面磁盘缓存（v0.32 可选功能）。

把已抓取的 HTML 按 URL 的哈希存到本地目录，重跑时直接命中，
避免重复请求：断点续传、调试、或被限流后重试时提速明显。

结构（每页一个文件，便于并发写入）：
    <cache_dir>/<sha1(url)>.json   ->  {"url": ..., "status": 200, "html": "..."}
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path


class PageCache:
    """线程安全的页面缓存。"""

    def __init__(self, directory: str | Path | None, *, enabled: bool = True) -> None:
        self.root = Path(directory).expanduser() if directory else None
        self.enabled = bool(enabled and directory)
        self._lock = threading.Lock()
        self.hits = 0
        self.writes = 0
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logging.warning("无法创建缓存目录 %s（%s），本次不启用缓存。", self.root, exc)
                self.enabled = False

    def _path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        assert self.root is not None
        return self.root / f"{digest}.json"

    def get(self, url: str) -> tuple[str, int] | None:
        """命中返回 (html, status)，否则 None。"""
        if not self.enabled:
            return None
        path = self._path(url)
        try:
            with self._lock:
                raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if data.get("url") != url:
                return None
            with self._lock:
                self.hits += 1
            return data["html"], int(data.get("status", 200))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, url: str, html: str, status: int = 200) -> None:
        if not self.enabled:
            return
        path = self._path(url)
        try:
            fd, tmp = tempfile.mkstemp(prefix=".page-", dir=str(self.root))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"url": url, "status": status, "html": html}, fh, ensure_ascii=False)
            os.replace(tmp, path)
            with self._lock:
                self.writes += 1
        except OSError as exc:
            logging.debug("写入缓存失败（%s）：%s", path, exc)

    def stats(self) -> str:
        return f"缓存命中 {self.hits} 次，新写入 {self.writes} 页"
