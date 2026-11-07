#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小说章节下载器 / Novel chapter downloader (v0.3).

从小说目录页抓取章节，自动拼接多页章节、过滤广告与无关链接，
合并为一个 TXT 文件。

v0.31 —— 支持任意小说站
    新增 --url 参数：传入任意小说站的目录页网址即可。目录与正文不再依赖
    某个站点写死的选择器，而是用「多模板候选 + 结构启发式」自动识别
    （见 site_adapter.py），并支持目录页分页（--all-pages）。

v0.32 —— 提速
    - 多线程并发下载章节（--workers，默认 6）；
    - 自适应限速：遇到 429/503 或验证码时自动拉长请求间隔，恢复后再加速
      （见 concurrency.py）；
    - 可选的页面磁盘缓存（--cache-dir），重跑/续传直接命中，避免重复请求。

用法：
    python novel_download.py                                  # 内置目标站，第 1-100 章
    python novel_download.py --url https://example.com/book/1.html
    python novel_download.py --url ... --start 1 --end 50
    python novel_download.py --url ... --all                  # 目录中的全部章节
    python novel_download.py --url ... --dry-run              # 只解析目录
    python novel_download.py --url ... --resume               # 断点续传
    python novel_download.py --url ... --workers 8 --cache-dir .novel_cache

依赖：requests、beautifulsoup4（见 requirements.txt）
可选：Pillow（终端内显示验证码更清晰，非必需）
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import logging
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar, create_cookie
from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout
from urllib3.util.retry import Retry

try:  # Windows 控制台可能是 GBK，尽量切换为 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# 通用站点适配（v0.31）与并发 / 限速 / 缓存（v0.32）
import chapter_api
import site_adapter
from concurrency import RateLimiter, run_ordered
from page_cache import PageCache
from site_adapter import (
    CHAPTER_TITLE_RE,
    extract_next_page,
    is_ad_line,
    page_chapter_title,
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
BASE_URL = "https://www.shuhaoxs.com"
INDEX_URL = f"{BASE_URL}/book/t0yp00p.html"
DEFAULT_OUTPUT = "小说_1-100章.txt"

DEFAULT_START = 1
DEFAULT_END = 100
DEFAULT_WORKERS = 6              # 并发下载线程数（v0.32）
MAX_WORKERS = 16

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = (10, 30)       # (连接超时, 读取超时) 秒
MAX_RETRIES = 3                  # 连接失败/5xx 重试次数
RETRY_BACKOFF = 2.0              # 连接失败重试退避秒数（指数增长）
SERVER_ERROR_BACKOFF = 0.5       # 5xx/429 重试的线性退避基数（秒）
CHAPTER_DELAY = 0.8              # 请求间隔（秒），由 RateLimiter 自适应调整
JITTER = 0.4                     # 随机抖动上限（秒），避免请求节奏过于机械
MAX_PAGES_PER_CHAPTER = 50       # 单章最多翻页数，防御死循环
PROGRESS_EVERY = 10              # 每下载 N 章打印一次阶段进度
MAX_TOC_PAGES = 200              # 目录分页最多翻页数（--all-pages）

# 章节 URL 到基础 URL、分页序号的解析
# 章节 URL 到基础 URL、分页序号的解析
CHAPTER_URL_RE = re.compile(r"^(?P<base>.+?)(?:-(?P<page>\d+))?(?P<ext>\.s?html?)$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# 数据模型与异常
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chapter:
    """目录中的一章。urls 为同一章的多个分页地址（按顺序）。"""

    title: str
    urls: tuple[str, ...]
    index: int = 0


class WafVerificationRequired(RuntimeError):
    """页面被 WAF 图形验证码拦截，需要人工在终端输入验证码。"""


class RobotsDisallowed(RuntimeError):
    """robots.txt 禁止爬取。"""


class _HttpStatusError(ConnectionError):
    """服务器在重试耗尽后仍返回错误状态码。"""

    def __init__(self, status: int, url: str) -> None:
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status}：{url}")


class ImageDecodeError(Exception):
    """验证码图片无法在终端解码渲染。"""


@dataclass
class DownloadReport:
    """下载结果统计。"""

    chapters: list[Chapter] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    failed: list[tuple[Chapter, str]] = field(default_factory=list)


class LogCollector(logging.Handler):
    """收集 WARNING 及以上日志，用于最终汇总。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append((record.levelno, record.getMessage()))


# ---------------------------------------------------------------------------
# 文本与 HTML 处理
# ---------------------------------------------------------------------------


def normalize_text(raw: str) -> str:
    """统一换行、全角空格与行尾空白。"""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("\u3000", " ")
    return "\n".join(line.rstrip() for line in text.split("\n"))


def is_captcha_page(soup: BeautifulSoup, url: str) -> bool:
    """识别 GoEdge WAF 验证码页面，及其它常见的人机验证页。"""
    if "WAF/VERIFY" in url.upper():
        return True
    if soup.find("form", id="captcha-form") is not None:
        return True
    if soup.find("img", id="ui-captcha-image") is not None:
        return True
    title = soup.find("title")
    if title and any(k in title.get_text() for k in ("身份验证", "安全验证", "人机验证", "Verify Yourself")):
        return True
    # 只靠文字判断容易误伤：JS 源码里也可能同时出现「验证码」「请输入」
    # （实测站点的 public.js 就是如此），因此必须同时存在验证码图片/输入框。
    has_input = soup.find("input", attrs={"name": re.compile("CAPTCHA|code", re.I)}) is not None
    has_image = any(
        "captcha" in ((img.get("src") or "") + " " + (img.get("id") or "")).lower()
        for img in soup.find_all("img")
    )
    if has_input and (has_image or soup.find("form") is not None):
        body_text = soup.get_text(" ")
        return "验证码" in body_text or "verify" in body_text.lower()
    return False


def split_page_url(url: str) -> tuple[str, str, str]:
    """把 URL 拆成 (去掉扩展名的部分, 扩展名)。"""
    match = re.match(r"^(?P<stem>.+?)(?P<ext>\.s?html?)$", url, re.IGNORECASE)
    if not match:
        return url, ""
    return match.group("stem"), match.group("ext")


_PAGE_SUFFIX_URL_RE = re.compile(r"^(?P<base>.+)-(?P<page>\d+)$")


def _has_page_suffix(url: str) -> bool:
    """该 URL 是否带页面分页后缀（xxx-2.html 形式）。"""
    return bool(re.search(r"-\d+\.s?html?$", url, re.IGNORECASE))


def next_page_url(
    soup: BeautifulSoup,
    current_url: str,
    *,
    chapter_urls: tuple[str, ...],
    position: int,
) -> str | None:
    """找章节下一分页。

    只有两种可靠来源：
    1. 页面上真实的「下一页」链接；
    2. 目录页显式列出的同章分页（xxx-2.html）。
    不再按 URL 数字后缀预测：不少站点对不存在的页返回“残留内容页”而非 404，
    预测会把别的内容拼进正文。
    """
    link = extract_next_page(soup, current_url)
    if link is not None:
        return link
    if 0 <= position < len(chapter_urls) - 1:
        return chapter_urls[position + 1]
    return None


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """解析服务器 Retry-After 响应头（秒数形式）。"""
    value = (resp.headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, min(seconds, 120.0))


class _CachedResponse:
    """缓存命中时返回的轻量响应对象，接口与 requests.Response 的常用部分一致。"""

    __slots__ = ("url", "status_code", "text", "content", "encoding", "from_cache")

    def __init__(self, url: str, html: str, status: int = 200) -> None:
        self.url = url
        self.status_code = status
        self.text = html
        self.content = html.encode("utf-8", "replace")
        self.encoding = "utf-8"
        self.from_cache = True


# ---------------------------------------------------------------------------
# 应用设置
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    start: int = DEFAULT_START
    end: int | None = DEFAULT_END
    output: str = DEFAULT_OUTPUT
    cookies: str = "cookies.json"
    delay: float = CHAPTER_DELAY
    workers: int = DEFAULT_WORKERS          # 并发线程数（v0.32）
    cache_dir: str | None = None            # 页面缓存目录，None 表示不启用
    all_pages: bool = False                 # 抓取目录页的全部分页
    by_order: bool = False                  # 按目录顺序而非章号选章
    no_range_api: bool = False              # 不抓取分章区间接口
    obey_robots: bool = True
    resume: bool = False
    verify_throttle: bool = True            # 是否自动降速（测试时可关闭）
    sleep: Callable[[float], None] = time.sleep
    chapters: list[Chapter] = field(default_factory=list)
    index_url: str = INDEX_URL


# ---------------------------------------------------------------------------
# 下载器
# ---------------------------------------------------------------------------


class NovelDownloader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.index_url = settings.index_url or INDEX_URL
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        # 重试在 fetch() 里手动控制，便于验证码流程插入
        adapter = HTTPAdapter(max_retries=Retry(total=0), pool_maxsize=MAX_WORKERS * 2)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.waf_verified = False
        self._cookies_path = Path(settings.cookies).expanduser()
        self._viewer_opened = False
        # v0.32：自适应限速 + 可选的磁盘缓存
        self.limiter = RateLimiter(delay=settings.delay, sleep=settings.sleep)
        self.cache = PageCache(settings.cache_dir, enabled=bool(settings.cache_dir))
        # 失败页的短时负缓存：同一运行内不重复请求已知失败的分页
        self._failed: dict[str, _HttpStatusError] = {}
        self._failed_lock = threading.Lock()
        self._load_cookies()


    # ---- Cookie 持久化 -------------------------------------------------
    def _load_cookies(self) -> None:
        if not self._cookies_path.exists():
            return
        try:
            data = json.loads(self._cookies_path.read_text(encoding="utf-8"))
            jar = RequestsCookieJar()
            for item in data.get("cookies", []):
                jar.set_cookie(
                    create_cookie(
                        name=item["name"],
                        value=item["value"],
                        domain=item.get("domain") or "",
                        path=item.get("path") or "/",
                    )
                )
            self.session.cookies.update(jar)
            logging.info("已载入 %d 个 Cookie：%s", len(self.session.cookies), self._cookies_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logging.warning("读取 Cookie 文件失败（%s），将重新验证。", exc)

    def _save_cookies(self) -> None:
        try:
            payload = {
                "cookies": [
                    {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                    for c in self.session.cookies
                ]
            }
            self._cookies_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(self._cookies_path, 0o600)
            logging.info("Cookie 已保存到 %s，下次运行自动复用。", self._cookies_path)
        except OSError as exc:
            logging.warning("保存 Cookie 失败：%s", exc)

    # ---- HTTP -----------------------------------------------------------
    def fetch(self, url: str, *, referer: str | None = None, _retries: int = 0) -> requests.Response:
        """GET 页面：限速、缓存、自动重试，并处理 WAF 验证码与限流。

        线程安全：每次请求前经 RateLimiter 串行排队；requests.Session 内部
        以 urllib3 连接池工作，多线程并发读取是安全的。
        """
        cached = self.cache.get(url)
        if cached is not None:
            html, status = cached
            return _CachedResponse(url, html, status)

        with self._failed_lock:
            previous = self._failed.get(url)
        if previous is not None:
            raise previous

        headers = {"Referer": referer} if referer else {}
        last_error = ""
        while _retries <= MAX_RETRIES:
            self.limiter.acquire()
            try:
                resp = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            except (ConnectionError, Timeout, ChunkedEncodingError) as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                if _retries < MAX_RETRIES:
                    _retries += 1
                    wait = RETRY_BACKOFF ** _retries
                    logging.warning("请求失败（%s），%s 秒后重试 %d/%d：%s", last_error, wait, _retries, MAX_RETRIES, url)
                    self.settings.sleep(wait)
                    continue
                raise ConnectionError(f"{url} 连接失败（已重试 {MAX_RETRIES} 次）：{last_error}") from exc

            # 429 / 503 / 5xx：服务器限流或故障，自动降速并重试（404 不重试）
            if resp.status_code == 429 or resp.status_code == 503 or resp.status_code >= 500:
                if self.settings.verify_throttle and resp.status_code in (429, 503):
                    self.limiter.report_throttled()
                retry_after = _retry_after_seconds(resp)
                if _retries < MAX_RETRIES:
                    _retries += 1
                    wait = retry_after if retry_after is not None else min(SERVER_ERROR_BACKOFF * _retries, 8.0)
                    logging.warning(
                        "服务器返回 %s，%s 秒后重试 %d/%d：%s",
                        resp.status_code, wait, _retries, MAX_RETRIES, url,
                    )
                    self.settings.sleep(wait)
                    continue
                # 重试耗尽：明确报错，并记住该 URL，避免重试轮次重复请求
                error = _HttpStatusError(resp.status_code, url)
                with self._failed_lock:
                    self._failed[url] = error
                raise error

            resp.encoding = self._detect_encoding(resp)
            soup = BeautifulSoup(resp.text, "html.parser")
            if is_captcha_page(soup, str(resp.url)):
                logging.info("检测到人机验证页面。")
                self.limiter.report_captcha()
                self._handle_verification(resp, soup)
                return self.fetch(url, referer=referer, _retries=_retries)
            if not self.waf_verified:
                self.waf_verified = True
                self._save_cookies()
            if resp.status_code == 200:
                self.limiter.report_success()
            if resp.status_code in (200, 404, 410):
                # 404 也缓存：章节末尾的「下一页」探测只发一次请求
                self.cache.put(url, resp.text, resp.status_code)
            return resp
        raise ConnectionError(f"{url} 请求失败：{last_error}")

    @staticmethod
    def _detect_encoding(resp: requests.Response) -> str:
        """按 HTML 声明/内容嗅探确定编码，避免 apparent_encoding 误判为 ISO-8859-1。"""
        if resp.encoding and resp.encoding.lower() not in ("iso-8859-1", "ascii"):
            return resp.encoding
        match = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", resp.content[:4096], re.IGNORECASE)
        if match:
            name = match.group(1).decode("ascii", "ignore").lower()
            return {"gbk": "gb18030", "gb2312": "gb18030"}.get(name, name)
        return resp.apparent_encoding or "utf-8"

    # ---- 验证码 ---------------------------------------------------------
    def _handle_verification(self, resp: requests.Response, soup: BeautifulSoup) -> None:
        """下载验证码图片并在终端请用户输入验证码。"""
        form = soup.find("form", id="captcha-form")
        img = soup.find("img", id="ui-captcha-image")
        if form is None or img is None or not img.get("src"):
            raise WafVerificationRequired(
                "网站要求人机验证，但页面中找不到验证码表单；请用浏览器打开确认网站结构。"
            )

        action_url = str(resp.url)
        image_url = requests.compat.urljoin(action_url, img["src"])
        hidden = {
            inp["name"]: inp.get("value", "")
            for inp in form.find_all("input")
            if inp.get("name") and (inp.get("type") or "").lower() == "hidden"
        }
        code_input = form.find("input", id="GOEDGE_WAF_CAPTCHA_CODE")
        code_field = code_input.get("name") if code_input and code_input.get("name") else "GOEDGE_WAF_CAPTCHA_CODE"

        print()
        print("=" * 62)
        print("网站开启了人机验证（GoEdge WAF），需要输入一次验证码。")
        print("=" * 62)

        for attempt in range(1, 4):
            try:
                img_resp = self.session.get(image_url, headers={"Referer": action_url}, timeout=REQUEST_TIMEOUT)
                img_resp.raise_for_status()
            except (ConnectionError, Timeout, requests.HTTPError) as exc:
                raise WafVerificationRequired(f"下载验证码图片失败：{exc}") from exc

            self._show_captcha(img_resp.content)
            try:
                code = input("请输入验证码后回车：").strip()
            except EOFError as exc:
                raise WafVerificationRequired("当前不是交互式终端，无法输入验证码。") from exc
            if not code:
                print("验证码为空，请重新输入。")
                continue

            try:
                submit = self.session.post(
                    action_url,
                    data={**hidden, code_field: code},
                    headers={"Referer": action_url},
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True,
                )
            except (ConnectionError, Timeout) as exc:
                raise WafVerificationRequired(f"提交验证码失败：{exc}") from exc

            submit_soup = BeautifulSoup(submit.text, "html.parser")
            if not is_captcha_page(submit_soup, str(submit.url)):
                print("验证通过，继续下载。")
                self.waf_verified = True
                self._save_cookies()
                return

            print(f"验证码不正确（第 {attempt} 次），请重试。")
            # 服务端可能已刷新验证码：下一轮以新页面上的图片为准
            new_img = submit_soup.find("img", id="ui-captcha-image")
            new_form = submit_soup.find("form", id="captcha-form")
            if new_img is not None and new_img.get("src") and new_form is not None:
                image_url = requests.compat.urljoin(str(submit.url), new_img["src"])
                action_url = str(submit.url)
                hidden = {
                    inp["name"]: inp.get("value", "")
                    for inp in new_form.find_all("input")
                    if inp.get("name") and (inp.get("type") or "").lower() == "hidden"
                }
                new_code_input = new_form.find("input", id="GOEDGE_WAF_CAPTCHA_CODE")
                code_field = (
                    new_code_input.get("name")
                    if new_code_input and new_code_input.get("name")
                    else code_field
                )

        raise WafVerificationRequired("连续 3 次验证码错误，请稍后重新运行。")

    def _show_captcha(self, content: bytes) -> None:
        """把验证码图片落地，并尽量在终端中直接渲染出来。"""
        if content.startswith(b"\x89PNG"):
            ext = ".png"
        elif content.startswith(b"GIF8"):
            ext = ".gif"
        else:
            ext = ".jpg"
        fd, name = tempfile.mkstemp(prefix="captcha_", suffix=ext)
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        path = Path(name)

        rendered = False
        if os.environ.get("TERM_PROGRAM") == "iTerm.app":
            print(f"\033]1337;File=inline=1:{base64.b64encode(content).decode()}\a")
            rendered = True
        elif os.environ.get("KITTY_WINDOW_ID") or os.environ.get("TERM") == "xterm-kitty":
            self._print_kitty_image(content)
            rendered = True
        if not rendered:
            try:
                self._render_image(content)
            except ImageDecodeError:
                print("（无法在终端内显示图片，请在下方路径查看）")

        print(f"验证码图片：{path}")
        self._open_viewer(path)

    @staticmethod
    def _print_kitty_image(content: bytes) -> None:
        b64 = base64.b64encode(content).decode()
        chunk = 4096
        for i in range(0, len(b64), chunk):
            head = "\033_Ga=T,f=100;" if i == 0 else "\033_Gm=1;"
            print(f"{head}{b64[i:i + chunk]}\033\\", end="")
        print("\033_Gm=0\033\\")

    def _render_image(self, content: bytes) -> None:
        """在终端内渲染验证码图片：优先 Pillow，缺失时用内置 PNG 解码器。"""
        warnings = None
        try:
            from PIL import Image  # type: ignore
            import warnings as _warnings

            warnings = _warnings
        except ImportError:
            Image = None
        if Image is not None:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    img = Image.open(io.BytesIO(content)).convert("RGB")
                width, height = img.size
                pixels = list(img.getdata())
            except (OSError, ValueError) as exc:
                raise ImageDecodeError(str(exc)) from exc
        else:
            width, height, pixels = self._decode_png_rgb(content)
        self._print_ascii_image(width, height, pixels)

    @staticmethod
    def _luminance(pixel: tuple[int, int, int]) -> float:
        return 0.299 * pixel[0] + 0.587 * pixel[1] + 0.114 * pixel[2]

    @staticmethod
    def _print_ascii_image(width: int, height: int, pixels: list[tuple[int, int, int]]) -> None:
        """ASCII 字符画渲染：按字符格平均亮度取字符，任何终端都能看清。"""
        try:
            cap = shutil.get_terminal_size((100, 40)).columns
        except OSError:
            cap = 100
        cols = max(40, min(cap, 120))
        step_x = max(1, round(width / cols))
        rows = max(1, int(height / step_x / 0.5))  # 终端字符约 2:1 高宽比
        step_y = max(1, round(height / rows))
        ramp = " .:-=+*#%@"
        lines = []
        for y in range(0, height - step_y + 1, step_y):
            chars = []
            for x in range(0, width, step_x):
                light = NovelDownloader._luminance(pixels[y * width + x])
                chars.append(ramp[min(len(ramp) - 1, int(light / 256 * len(ramp)))])
            lines.append("".join(chars).rstrip())
        print("\n".join(lines))

    def _decode_png_rgb(self, content: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
        """内置 PNG 解码（仅 8bit RGB/RGBA 非隔行），作为无 Pillow 时的兜底。"""
        if not content.startswith(b"\x89PNG"):
            raise ImageDecodeError("不是 PNG 图片")
        pos, width, height, bit_depth, color_type, idat = 8, 0, 0, 0, 0, bytearray()
        try:
            while pos < len(content):
                (length,) = struct.unpack(">I", content[pos:pos + 4])
                ctype = content[pos + 4:pos + 8]
                data = content[pos + 8:pos + 8 + length]
                pos += 12 + length
                if ctype == b"IHDR":
                    width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", data)
                    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
                        raise ImageDecodeError("不支持的 PNG 格式")
                elif ctype == b"IDAT":
                    idat += data
                elif ctype == b"IEND":
                    break
            raw = zlib.decompress(bytes(idat))
            channels = 4 if color_type == 6 else 3
            stride = width * channels
            prev = bytearray(stride)
            pixels: list[tuple[int, int, int]] = []
            offset = 0
            for _ in range(height):
                filter_type = raw[offset]
                line = bytearray(raw[offset + 1:offset + 1 + stride])
                offset += stride + 1
                for i in range(stride):
                    a = line[i - channels] if i >= channels else 0
                    b = prev[i]
                    c = prev[i - channels] if i >= channels else 0
                    if filter_type == 1:
                        line[i] = (line[i] + a) & 0xFF
                    elif filter_type == 2:
                        line[i] = (line[i] + b) & 0xFF
                    elif filter_type == 3:
                        line[i] = (line[i] + (a + b) // 2) & 0xFF
                    elif filter_type == 4:
                        p = a + b - c
                        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                        line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
                for x in range(0, width * channels, channels):
                    pixels.append((line[x], line[x + 1], line[x + 2]))
                prev = line
        except ImageDecodeError:
            raise
        except (struct.error, zlib.error, IndexError, ValueError) as exc:
            raise ImageDecodeError(str(exc)) from exc
        return width, height, pixels

    def _open_viewer(self, path: Path) -> None:
        """用系统默认图片查看器打开验证码（只打开一次，避免连续弹窗）。"""
        if self._viewer_opened:
            return
        self._viewer_opened = True
        if sys.platform.startswith("linux"):
            opener = "xdg-open"
        elif sys.platform == "darwin":
            opener = "open"
        elif sys.platform.startswith("win"):
            opener = "start"
        else:
            return
        if not shutil.which(opener):
            return
        try:
            subprocess.Popen(
                [opener, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=(opener == "start"),
            )
        except OSError as exc:
            logging.debug("打开图片失败：%s", exc)

    # ---- 目录页解析 -----------------------------------------------------
    def get_chapter_list(self, index_url: str) -> list[Chapter]:
        """解析目录页（v0.31：不依赖具体站点模板）。

        对任意小说站：先用 site_adapter 的多种容器模板找章节链接，
        容器都不命中时退化为全页扫描；按 URL 去重（目录顶部常重复列出
        「最新章节」），同一章的分页链接合并到同一章；最后按「第X章」
        的数字排序，保证第 1 章真的是第一章。

        ``--all-pages`` 时沿目录页的「下一页」继续抓取并合并。
        """
        entries: list[site_adapter.TocEntry] = []
        visited: set[str] = set()
        page_url: str | None = index_url
        pages = 0
        while page_url and pages < MAX_TOC_PAGES:
            if page_url in visited:
                break
            visited.add(page_url)
            resp = self.fetch(page_url)
            soup = BeautifulSoup(resp.text, "html.parser")
            if pages == 0 and not self.settings.no_range_api:
                # 目录页若带「分章区间」下拉（1-100/101-200...），只解析静态页
                # 会漏掉后面的章节，这里改为走站点的分章接口。
                extra = self._fetch_range_chapters(soup, str(resp.url))
                entries.extend(extra)
            parsed = site_adapter.parse_toc_page(soup, str(resp.url))
            pages += 1
            entries.extend(parsed.entries)
            # 静态目录分页：--all-pages 时沿「下一页」继续
            page_url = parsed.next_page if self.settings.all_pages else None

        if pages > 1:
            logging.info("目录共抓取 %d 页。", pages)

        # 按 URL 去重（保留首次出现，即正文目录的顺序）
        unique: dict[str, site_adapter.TocEntry] = {}
        for entry in entries:
            unique.setdefault(entry.url, entry)
        entries = list(unique.values())

        if not entries:
            logging.warning(
                "未能从目录页识别出章节链接（%s）。请确认传入的是目录页，"
                "或该站目录需要登录/启用了反爬。可加 -v 查看解析细节。",
                index_url,
            )
            return []

        # 合并同一章的分页：优先按标题归一化后分组（同一章各页标题相同），
        # 其次按 URL 的 -N 后缀规则。
        grouped: dict[str, dict] = {}
        for entry in entries:
            key = normalised_title(entry.title)
            group = grouped.setdefault(key, {"title": entry.title, "urls": [], "number": chapter_number(entry.title)})
            if entry.url not in group["urls"]:
                group["urls"].append(entry.url)

        ordered = list(grouped.values())
        for group in ordered:
            urls = group["urls"]
            if len(urls) > 1:
                urls.sort(key=lambda u: chapter_page_number(u, tuple(urls)))

        # 有章节号的按序号排序；无章节号的（楔子/序章/番外）按出现顺序排在前面
        def sort_key(item: tuple[int, dict]) -> tuple:
            position, group = item
            return (0, position) if group["number"] is None else (1, group["number"])

        result = [
            Chapter(group["title"], tuple(group["urls"]), index)
            for index, (_, group) in enumerate(sorted(enumerate(ordered), key=sort_key), 1)
        ]
        logging.info("目录解析完成：共 %d 章。", len(result))
        return result


    def _fetch_range_chapters(self, soup: BeautifulSoup, page_url: str) -> list[site_adapter.TocEntry]:
        """抓取「分章区间」下拉里的全部章节（如 101-200、201-300...）。

        站点把后面的章节放在异步接口里（参数加密），只解析静态目录页只能拿到
        默认的 1-100 章。这里识别下拉菜单 → 找到对应站点的加密接口配置 →
        逐区间请求并合并。识别不到就返回空列表，流程退回静态目录。
        """
        menu = chapter_api.find_range_menu(soup)
        if menu is None:
            return []
        book_id, options = menu
        host = urlsplit(page_url).netloc
        api = chapter_api.api_for_host(host)
        if api is None:
            api = chapter_api.parse_api_from_js(self._site_js_text(page_url, soup))
        if api is None:
            logging.warning(
                "目录页有分章区间菜单（%s），但未识别到该站点的分章接口；"
                "只能下载默认区间。可用 --no-range-api 关闭此提示。",
                "、".join(opt.label for opt in options[:4]),
            )
            return []

        base = f"{urlsplit(page_url).scheme}://{host}"
        pages = [opt.page for opt in options]
        logging.info(
            "检测到分章区间菜单：%s，共 %d 个区间，开始抓取全部章节……",
            "、".join(opt.label for opt in options),
            len(pages),
        )

        def request_json(url: str, form: dict[str, str], headers: dict[str, str]) -> dict:
            return self.fetch_json(url, form, headers, referer=page_url)

        result = chapter_api.fetch_chapters(
            api, book_id, request_json=request_json, pages=pages, base_url=base, referer=page_url
        )
        for error in result.errors:
            logging.warning("%s", error)

        # 下拉项的最大页码未必覆盖全部章节：继续探测，直到没有新章节
        if not self.settings.no_range_api and result.chapters:
            extra_pages = chapter_api.discover_pages(
                api, book_id, request_json=request_json, base_url=base, referer=page_url,
                max_pages=max(max(pages) + 20, 60),
            )
            extra_pages = [p for p in extra_pages if p not in pages]
            if extra_pages:
                more = chapter_api.fetch_chapters(
                    api, book_id, request_json=request_json, pages=extra_pages,
                    base_url=base, referer=page_url,
                )
                result.chapters.extend(more.chapters)
                logging.info("菜单之外还有 %d 个区间，已一并抓取。", len(extra_pages))

        logging.info("分章接口返回 %d 章。", len(result.chapters))
        return [
            site_adapter.TocEntry(title, requests.compat.urljoin(page_url, href), order)
            for order, (title, href) in enumerate(result.chapters)
        ]

    def _site_js_text(self, page_url: str, soup: BeautifulSoup | None = None) -> str:
        """尽力读取目录页引用的站点 JS，用于自动识别加密接口参数。"""
        if soup is None:
            try:
                soup = BeautifulSoup(self.fetch(page_url).text, "html.parser")
            except (ConnectionError, WafVerificationRequired):
                return ""
        texts: list[str] = []
        for script in soup.find_all("script", src=True):
            src = script["src"]
            if not any(k in src.lower() for k in ("public", "common", "index", "main", "app")):
                continue
            try:
                js = self.fetch(requests.compat.urljoin(page_url, src))
            except (ConnectionError, WafVerificationRequired):
                continue
            if "AESCrypt" in js.text or "action=" in js.text:
                texts.append(js.text)
        return "\n".join(texts)

    def fetch_json(
        self, url: str, form: dict[str, str], headers: dict[str, str], *, referer: str = ""
    ) -> dict:
        """POST 一个 JSON 接口（供分章接口使用），带限速与重试。"""
        merged = {"Referer": referer} if referer else {}
        merged.update(headers)
        merged.setdefault("X-Requested-With", "XMLHttpRequest")
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 2):
            self.limiter.acquire()
            try:
                resp = self.session.post(url, data=form, headers=merged, timeout=REQUEST_TIMEOUT)
            except (ConnectionError, Timeout, ChunkedEncodingError) as exc:
                last_error = exc
                if attempt <= MAX_RETRIES:
                    self.settings.sleep(RETRY_BACKOFF ** attempt)
                    continue
                raise
            if resp.status_code in (429, 503) or resp.status_code >= 500:
                if self.settings.verify_throttle and resp.status_code in (429, 503):
                    self.limiter.report_throttled()
                last_error = _HttpStatusError(resp.status_code, url)
                if attempt <= MAX_RETRIES:
                    self.settings.sleep(min(SERVER_ERROR_BACKOFF * attempt, 8.0))
                    continue
                raise last_error
            self.limiter.report_success()
            try:
                return resp.json()
            except ValueError as exc:
                raise ValueError(f"接口返回的不是 JSON：{resp.text[:120]}") from exc
        raise last_error or ConnectionError(url)

    # ---- 章节页抓取 -----------------------------------------------------
    def get_chapter_content(self, chapter: Chapter) -> str:
        """下载一章的所有分页并拼接正文。

        分页发现（必须串行，因为依赖页内「下一页」链接）：
            -N 分页格式的章节会一路探测到页面没有正文为止。
        正文抓取（并发）：已发现的分页用线程池并行请求，分页越多提速越明显
        （小说站单章常有 10-50 页，串行抓取是大头）。
        """
        pages, first_soups = self._discover_pages(chapter)
        if not pages:
            return ""

        texts: dict[str, str] = {}
        for url, soup in first_soups.items():
            text = site_adapter.extract_content(soup)
            if text:
                texts[url] = text

        remaining = [url for url in pages if url not in texts]
        if remaining:
            workers = max(1, min(self.settings.workers, len(remaining), 8))
            if workers > 1:
                results = run_ordered(
                    list(enumerate(remaining)),
                    lambda _i, url: self._fetch_page_text(url, pages),
                    max_workers=workers,
                )
                for index, url in enumerate(remaining):
                    value = results.values.get(index)
                    if isinstance(value, str) and value:
                        texts[url] = value
            else:
                for url in remaining:
                    text = self._fetch_page_text(url, pages)
                    if text:
                        texts[url] = text

        if not texts:
            logging.warning("第 %s 章未找到正文：%s", chapter.index, chapter.urls[0])
            return ""
        return "\n\n".join(texts[url] for url in pages if url in texts)

    def _fetch_page_text(self, url: str, pages: list[str]) -> str:
        """抓取单个分页的正文（供并发调用）。"""
        resp = self.fetch(url, referer=pages[0])
        if resp.status_code in (404, 410):
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        return site_adapter.extract_content(soup)

    def _discover_pages(self, chapter: Chapter) -> tuple[list[str], dict[str, BeautifulSoup]]:
        """沿「下一页」发现本章全部分页。

        返回 (按顺序的分页 URL 列表, 已抓取的页面 soup 缓存)。
        """
        pages: list[str] = []
        soups: dict[str, BeautifulSoup] = {}
        visited: set[str] = set()
        expected = normalised_title(chapter.title)
        current: str | None = chapter.urls[0]
        position = 0

        while current and len(visited) < MAX_PAGES_PER_CHAPTER:
            if current in visited:
                break
            visited.add(current)
            resp = self.fetch(current, referer=chapter.urls[0])
            if resp.status_code in (404, 410):
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            text = site_adapter.extract_content(soup)
            if not text:
                break  # 没有正文：本章到头
            page_title = page_chapter_title(soup)
            if page_title and position == -1 and normalised_title(page_title) != expected:
                break  # 串到别的章节了
            pages.append(current)
            soups[current] = soup

            following = next_page_url(soup, current, chapter_urls=chapter.urls, position=position)
            if following is None:
                # 页面没有「下一页」：本章结束。
                # 注意：不能用 URL 数字后缀硬推 -N（站点对不存在的页可能返回
                # 残留页面而不是 404，硬推会把别的内容拼进来）。
                break
            if following in visited:
                break
            try:
                position = chapter.urls.index(following)
            except ValueError:
                position = -1
            current = following
        return pages, soups

    def _jitter(self, base: float) -> float:
        return base + random.uniform(0, JITTER) if base > 0 else 0.0

    # ---- 主流程 ---------------------------------------------------------
    def check_robots(self) -> bool:
        """检查 robots.txt 是否允许抓取目录页。"""
        parts = urlsplit(self.index_url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=REQUEST_TIMEOUT)
        except (ConnectionError, Timeout):
            return True
        if resp.status_code != 200:
            return True
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser.can_fetch(USER_AGENT, self.index_url)

    def iter_positions(self, total: int) -> Iterator[tuple[int, Chapter | None]]:
        """按「章号」遍历选定区间，产出 (章号, 章节或 None)。

        - 默认按章号（标题里的「第X章」）匹配：目录缺章时，第 96 章就是缺的那章，
          不会把后面的章节顶上来当第 96 章；
        - ``--by-order`` 时按目录顺序取第 start..end 条，适合标题没有章号的站点。
        """
        settings = self.settings
        if settings.by_order or not any(chapter_number(c.title) is not None for c in self.settings.chapters):
            last = min(settings.end or total, total)
            for number in range(settings.start, last + 1):
                pos = number - 1
                yield number, (settings.chapters[pos] if pos < total else None)
            return

        by_number: dict[int, Chapter] = {}
        for chapter in settings.chapters:
            number = chapter_number(chapter.title)
            if number is not None:
                by_number.setdefault(number, chapter)
        last = settings.end if settings.end is not None else max(by_number, default=0)
        for number in range(settings.start, last + 1):
            yield number, by_number.get(number)

    def run(self, dry_run: bool = False) -> int:
        settings = self.settings
        logging.info("目标目录：%s", self.index_url)
        logging.info("抓取范围：第 %s 章 ~ %s", settings.start, settings.end if settings.end else "末章")
        if settings.workers > 1:
            logging.info("并发线程：%d，请求间隔自适应（初始 %.1f 秒）", settings.workers, self.limiter.delay)

        if settings.obey_robots and not self.check_robots():
            raise RobotsDisallowed("站点 robots.txt 不允许抓取该页面；如确认可访问，请加 --ignore-robots。")

        chapters = self.get_chapter_list(self.index_url)
        if not chapters:
            return 2
        settings.chapters = chapters

        targets = dict(self.iter_positions(len(chapters)))
        if dry_run:
            for number, chapter in targets.items():
                if chapter is None:
                    print(f"{number:>4}. [目录中不存在]")
                else:
                    extra = f"  (分页 {len(chapter.urls)})" if len(chapter.urls) > 1 else ""
                    print(f"{number:>4}. {chapter.title}  {chapter.urls[0]}{extra}")
            return 0

        last = settings.end if settings.end is not None else len(chapters)
        report = DownloadReport(chapters=chapters)
        if last > len(chapters):
            logging.warning("目录只有 %d 章，第 %d-%d 章不存在。", len(chapters), len(chapters) + 1, last)

        out_path = Path(settings.output)
        done: dict[str, str] = {}
        if settings.resume:
            done = parse_existing_txt(out_path)
            if done:
                print(f"断点续传：输出文件中已有 {len(done)} 章，将跳过。", flush=True)

        pending: list[tuple[int, Chapter]] = []
        collected: dict[int, tuple[str, str]] = {}
        for number, chapter in targets.items():
            if chapter is None:
                report.missing.append(f"第 {number} 章")
                continue
            cached_body = done.get(chapter_key(chapter.title), "").strip() if settings.resume else ""
            if cached_body:
                collected[number] = (chapter.title, cached_body)
            else:
                pending.append((number, chapter))

        if not pending:
            logging.info("所有章节已在输出文件中。")

        body_by_number = self._download_chapters(pending, settings.workers)

        retry_pool: list[tuple[int, Chapter]] = []
        for number, chapter in pending:
            result = body_by_number.get(number)
            if isinstance(result, str) and result.strip():
                collected[number] = (chapter.title, result.strip())
            else:
                retry_pool.append((number, chapter))

        # 失败章节再串行重试一轮（避免并发重试再次触发限流）
        if retry_pool:
            logging.info("重试失败章节：%d 章", len(retry_pool))
            for position, (number, chapter) in enumerate(retry_pool, 1):
                # 已知的 4xx/5xx 分页不再浪费一轮重试
                if chapter.urls[0] in self._failed:
                    error = self._failed[chapter.urls[0]]
                    print(f"[重试 {position}/{len(retry_pool)}] 第 {number} 章 {chapter.title}  跳过：{error}", flush=True)
                    report.failed.append((chapter, str(error)))
                    continue
                settings.sleep(self._jitter(min(max(self.limiter.delay * 2, 0.5), 2.0)))
                print(f"[重试 {position}/{len(retry_pool)}] 第 {number} 章 {chapter.title}", end="", flush=True)
                try:
                    body = self.get_chapter_content(chapter)
                except WafVerificationRequired:
                    print()
                    raise
                except Exception as exc:  # noqa: BLE001
                    reason = f"{type(exc).__name__}: {str(exc)[:120]}"
                    print(f"  仍失败：{reason}", flush=True)
                    report.failed.append((chapter, reason))
                    continue
                if body.strip():
                    collected[number] = (chapter.title, body.strip())
                    print(f"  成功 {len(body):,} 字", flush=True)
                else:
                    print("  正文仍为空", flush=True)
                    report.failed.append((chapter, "正文为空"))

        ordered = [(number, *collected[number]) for number in sorted(collected)]
        write_book(out_path, title=out_path.stem.replace("_", " "), ordered=ordered, start=settings.start, end=last)
        summarize(report, ordered, out_path, len(pending), workers=settings.workers)
        if settings.cache_dir:
            logging.info(self.cache.stats())
        return 0 if not report.failed else 1

    def _download_chapters(self, pending: list[tuple[int, Chapter]], workers: int) -> dict[int, str | Exception]:
        """并发下载章节正文，返回 {章号: 正文(字符串) 或异常}。"""
        total = len(pending)
        if total == 0:
            return {}
        results: dict[int, str | Exception] = {}
        finished = [0]
        lock = threading.Lock()
        start = time.monotonic()

        step = max(1, min(PROGRESS_EVERY, total // 10 or 1))

        def report(number: int) -> None:
            with lock:
                finished[0] += 1
                count = finished[0]
                ok = sum(1 for v in results.values() if isinstance(v, str) and v.strip())
            if count % step and count != total:
                return
            elapsed = max(time.monotonic() - start, 1e-6)
            rate = count / elapsed
            print(
                f"---- 进度 {count}/{total}，成功 {ok} 章，{rate:.1f} 章/秒 ----",
                flush=True,
            )

        def worker(number: int, chapter: Chapter) -> str | Exception:
            try:
                return self.get_chapter_content(chapter)
            except WafVerificationRequired as exc:
                return exc
            except Exception as exc:  # noqa: BLE001 - 单章失败不影响其它章
                logging.debug("第 %d 章下载异常：%s: %s", number, type(exc).__name__, exc)
                return exc

        def on_result(number: int, chapter: Chapter, value: object, error: Exception | None) -> None:
            if error is not None:
                results[number] = error
            elif isinstance(value, BaseException):
                results[number] = value
            elif isinstance(value, str) and value.strip():
                results[number] = value
            report(number)

        print(f"开始并发下载 {total} 章（{min(workers, total)} 个线程）……", flush=True)
        run_ordered(pending, lambda number, chapter: worker(number, chapter), max_workers=workers, on_result=on_result)

        captcha = [v for v in results.values() if isinstance(v, WafVerificationRequired)]
        if captcha:
            raise WafVerificationRequired(str(captcha[0]))
        return results

# ---------------------------------------------------------------------------
# 输出与恢复
# ---------------------------------------------------------------------------


def chapter_page_number(url: str, chapter_urls: tuple[str, ...] = ()) -> int:
    """同一章内某分页的序号。

    目标站的章节 ID 本身可能含数字（t0yp00p-102.html），分页则是
    t0yp00p-102-2.html。因此只在同组的其它 URL 中有该 URL「去掉 -N 后的前缀」时，
    才把 -N 视为页码；否则视为第 1 页。
    """
    stem = re.sub(r"\.s?html?$", "", url, flags=re.IGNORECASE)
    match = re.match(r"^(?P<base>.+)-(?P<page>\d+)$", stem)
    if match:
        base = match.group("base")
        if any(
            other != url and re.sub(r"\.s?html?$", "", other, flags=re.IGNORECASE) == base
            for other in chapter_urls
        ):
            return int(match.group("page"))
    return 1


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def chinese_to_int(text: str) -> int | None:
    """把「十二」「一百零五」「三千零一」等中文数字转成整数；无法转换返回 None。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    section, digit = 0, None
    for char in text:
        if char in _CN_DIGITS:
            digit = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            section += (1 if digit is None else digit) * _CN_UNITS[char]
            digit = None
        else:
            return None
    return section + (digit or 0)


def chapter_number(title: str) -> int | None:
    """从章节标题中解析章号。"""
    match = re.search(r"第\s*([0-9零〇一二两三四五六七八九十百千]+)\s*[章节回卷]", title)
    return chinese_to_int(match.group(1)) if match else None


def chapter_key(title: str) -> str:
    """章节标题归一化，跨目录/输出文件匹配同一章。"""
    return re.sub(r"\s+", "", title)


PAGE_SUFFIX_RE = re.compile(r"[\s]*(?:[（(]\s*\d+\s*[)）]|[-_/]\s*\d+\s*页?|[（(]\s*[上下中]\s*[)）])$")


def normalised_title(title: str) -> str:
    """去掉分页后缀（如「（2）」「-2」「(上)」）后的标题，用于判定是否同一章。"""
    previous = None
    while previous != title:
        previous = title
        title = PAGE_SUFFIX_RE.sub("", title).strip()
    return chapter_key(title)


def format_book(title: str, ordered: list[tuple[int, str, str]], start: int, end: int) -> str:
    """把章节拼成完整 TXT 文本。"""
    parts = [title, f"共 {len(ordered)} 章（第 {start} 章 ~ 第 {end} 章）", "=" * 40, ""]
    for number, heading, body in ordered:
        text = body.strip()
        if not text.startswith(heading):
            text = f"{heading}\n\n{text}"
        parts.append(text)
    return "\n\n\n".join(parts) + "\n"


def write_book(path: Path, *, title: str, ordered: list[tuple[int, str, str]], start: int, end: int) -> None:
    """原子写出 TXT：先写临时文件再替换，中断时不会留下半截文件。"""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(format_book(title, ordered, start, end))
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def summarize(
    report: DownloadReport,
    ordered: list[tuple[int, str, str]],
    path: Path,
    requested: int,
    *,
    workers: int = 1,
) -> None:
    chars = sum(len(body) for _, _, body in ordered)
    print()
    print("=" * 62)
    print(f"完成：应有 {requested} 章，成功 {len(ordered)} 章，失败 {len(report.failed)} 章。")
    print(f"正文约 {chars:,} 字" + (f"（并发 {workers} 线程）" if workers > 1 else ""))
    print(f"输出文件：{path.resolve()}")
    if report.missing:
        print("目录中不存在：" + "、".join(report.missing[:10]))
    if report.failed:
        print("失败的章节（可用 --resume 重新运行，跳过已成功章节）：")
        for chapter, reason in report.failed[:20]:
            print(f"  - 第 {chapter.index} 章 {chapter.title}：{reason}")
    print("=" * 62)


def parse_existing_txt(path: Path) -> dict[str, str]:
    """解析已存在的输出文件，得到「章节标题 -> 正文」，用于 --resume。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    result: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is not None and "\n".join(buffer).strip():
            result[chapter_key(current)] = "\n".join(buffer).strip()

    pending: str | None = None  # 疑似章节标题的行，等到下一个非空行再确认
    pending_is_heading = False
    for line in lines:
        stripped = line.strip()
        if pending is not None:
            if stripped:
                if pending_is_heading:
                    flush()
                    current, buffer = pending, []
                elif current is not None:
                    buffer.append(pending)
                # 标题行本身也要写入正文，避免续传后标题丢失
                buffer.append(stripped)
                pending = None
                pending_is_heading = False
                continue
            if current is not None:
                buffer.append(line)  # 空行保持
            continue
        if not stripped:
            if current is not None:
                buffer.append(line)
            continue
        if is_chapter_heading(stripped):
            pending, pending_is_heading = stripped, True
            continue
        if current is not None:
            buffer.append(line)
        # current 为 None 时（文件头部说明），忽略
    if pending is not None:
        if pending_is_heading:
            flush()
            current, buffer = pending, []
        elif current is not None:
            buffer.append(pending)
    flush()
    return result


def is_chapter_heading(line: str) -> bool:
    """判断输出文件中的一行是否为章节标题。

    仅作为「续传」的辅助判断：正文段落也可能以「第X章」开头，因此只把
    **较短的整行**视为标题；正文段落一般远长于标题，不受影响。
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 60 or not CHAPTER_TITLE_RE.match(stripped):
        return False
    # 「正文」「楔子」等前缀的正文句子不应被当成标题
    if re.match(r"^\s*(?:正文|楔子|序章|序言|前言|引子)\s*$", stripped):
        return False
    return not is_ad_line(stripped)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从小说站点抓取章节并合并为 TXT 文件（v0.3：任意小说站 + 并发下载）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python novel_download.py --url https://example.com/book/123.html\n"
            "  python novel_download.py --url ... --start 1 --end 100 -o out.txt\n"
            "  python novel_download.py --url ... --all --workers 8 --cache-dir .cache\n"
        ),
    )
    parser.add_argument("--url", default=None, help="任意小说站的目录页地址（默认使用内置目标站）")
    parser.add_argument("--start", type=int, default=None, help=f"起始章节（默认 {DEFAULT_START}）")
    parser.add_argument("--end", type=int, default=None, help=f"结束章节，含（默认 {DEFAULT_END}）")
    parser.add_argument("--all", action="store_true", help="抓取目录中的全部章节（忽略 --start/--end）")
    parser.add_argument("--all-pages", action="store_true", help="目录有多页时抓取全部分页")
    parser.add_argument("--no-range-api", action="store_true", help="不抓取分章区间（下拉菜单）里的后续章节")
    parser.add_argument("--by-order", action="store_true", help="按目录顺序取第 N 条，而不是按「第X章」章号（默认按章号）")
    parser.add_argument("-o", "--output", default=None, help="输出文件路径")
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"并发下载线程数（默认 {DEFAULT_WORKERS}，范围 1-{MAX_WORKERS}）",
    )
    parser.add_argument("--delay", type=float, default=CHAPTER_DELAY, help=f"请求间隔秒数上限起点（默认 {CHAPTER_DELAY}，自动自适应）")
    parser.add_argument("--cache-dir", default=None, help="页面缓存目录（可选，重跑时跳过已抓页面）")
    parser.add_argument("--cookies", default="cookies.json", help="WAF 验证 Cookie 保存路径（默认 cookies.json）")
    parser.add_argument("--dry-run", action="store_true", help="只解析目录并打印章节列表，不下载正文")
    parser.add_argument("--resume", action="store_true", help="断点续传：跳过输出文件中已有正文的章节")
    parser.add_argument("--ignore-robots", action="store_true", help="忽略 robots.txt 限制")
    parser.add_argument("--base-url", default=None, help="站点根地址（若 --url 未给出时的默认站，测试用）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def build_settings(args: argparse.Namespace) -> Settings:
    if args.all:
        start, end = 1, None
        output = args.output or "小说_全部章节.txt"
    else:
        start = DEFAULT_START if args.start is None else args.start
        end = DEFAULT_END if args.end is None else args.end
        if start < 1:
            raise ValueError("--start 必须 >= 1")
        if end is not None and end < start:
            raise ValueError("--end 不能小于 --start")
        output = args.output or f"小说_{start}-{end if end is not None else '末'}章.txt"
    workers = max(1, min(MAX_WORKERS, args.workers))
    if args.workers != workers:
        logging.warning("--workers=%d 超出范围，已调整为 %d。", args.workers, workers)
    return Settings(
        start=start,
        end=end,
        output=output,
        cookies=args.cookies,
        delay=max(0.0, args.delay),
        workers=workers,
        cache_dir=args.cache_dir,
        all_pages=args.all_pages,
        by_order=args.by_order,
        no_range_api=args.no_range_api,
        obey_robots=not args.ignore_robots,
        resume=args.resume,
        index_url=args.url or INDEX_URL,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    global BASE_URL, INDEX_URL
    if args.base_url:
        BASE_URL = args.base_url.rstrip("/")
        INDEX_URL = f"{BASE_URL}/book/t0yp00p.html"
    try:
        settings = build_settings(args)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    downloader = NovelDownloader(settings)
    try:
        return downloader.run(dry_run=args.dry_run)
    except WafVerificationRequired as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 3
    except RobotsDisallowed as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 4
    except ConnectionError as exc:
        print(f"\n网络错误：{exc}", file=sys.stderr)
        print("提示：若浏览器能打开而本脚本不能，通常是该站对当前 IP 的网络层拦截。", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("\n已中断，未写出不完整的文件。", file=sys.stderr)
        return 130
    finally:
        downloader.session.close()


if __name__ == "__main__":
    sys.exit(main())
