#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小说章节下载器 / Novel chapter downloader.

从小说目录页抓取章节，自动拼接多页章节、过滤广告与无关链接，
合并为一个 TXT 文件。

V0.2 相对 V0.1 的主要改动：
1. 处理 GoEdge WAF 图形验证码：检测到验证页时在终端提示输入验证码，
   验证 Cookie 持久化到本地，一次验证后自动复用。
2. 正文选择器改为按正文长度择优选取（同时排除链接密集的外层容器），
   清理 script/style/ins/iframe 等杂质，逐行过滤广告、网址、导航。
3. 按目录顺序截取指定章节区间（默认第 1-100 章），失败章节自动重试，
   支持断点续传。
4. 连接失败、超时、5xx 自动重试；输出文件以原子方式写入，
   失败时不会留下空文件或半截文件。

用法：
    python novel_download.py                        # 第 1-100 章
    python novel_download.py --start 1 --end 100    # 显式指定区间
    python novel_download.py --all                  # 全部章节
    python novel_download.py --dry-run              # 只抓目录，打印章节列表
    python novel_download.py --resume               # 断点续传

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

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
BASE_URL = "https://www.shuhaoxs.com"
INDEX_URL = f"{BASE_URL}/book/t0yp00p.html"
DEFAULT_OUTPUT = "小说_1-100章.txt"

DEFAULT_START = 1
DEFAULT_END = 100

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = (10, 30)       # (连接超时, 读取超时) 秒
MAX_RETRIES = 3                  # 连接失败/5xx 重试次数
RETRY_BACKOFF = 2.0              # 重试退避秒数（指数增长）
CHAPTER_DELAY = 0.8              # 章节间基础间隔（秒）
JITTER = 0.4                     # 随机抖动上限（秒），避免请求节奏过于机械
MAX_PAGES_PER_CHAPTER = 50       # 单章最多翻页数，防御死循环
PROGRESS_EVERY = 10              # 每下载 N 章打印一次阶段进度

# 正文容器候选选择器：按优先级找到候选，再结合正文长度择优
CONTENT_SELECTORS = (
    "#content",
    "#chaptercontent",
    "#booktxt",
    "#txtContent",
    "#read-content",
    ".content",
    ".showtxt",
    ".read-content",
    ".book-content",
    ".novel-content",
    ".article-content",
    ".text",
    ".read-content > div",
    "article",
)

# 块级标签：get_text 时在这些元素前后断行
BLOCK_TAGS = frozenset(
    "p div br li dd dt h1 h2 h3 h4 h5 h6 tr table section article blockquote pre".split()
)

# 整个元素删除（连同子节点），这些标签从不属于正文
DROP_TAGS = (
    "script", "style", "ins", "iframe", "noscript", "svg", "canvas",
    "form", "button", "input", "select", "textarea", "nav", "aside",
    "audio", "video", "object", "embed", "link", "meta",
)
DROP_SELECTORS = (
    ".read-inline-img", ".ad", ".ads", ".advert", ".advertisement",
    ".gg", ".gg200x300", ".bdshare", ".share", ".bottem", ".page_chapter",
    ".report", ".readfoot", ".foot", ".footer", ".breadcrumb", ".crumbs",
)

# 广告/推广行判定使用的常量
AD_MAX_LINE_LEN = 80          # 超过该长度的行不做关键字判定，避免误伤正文句子
AD_DOMAINS = (                # 命中即视为广告（网址/站点名）
    ".com", ".net", ".org", ".cc", ".cn", ".vip", ".la", ".tv", ".info",
    ".xyz", ".top", ".pw", ".me", ".io", ".us", ".co", "http://", "https://",
    "www.", "wap.", "m.",
)
NAV_WORDS = frozenset(         # 纯导航行判定用词
    "上一章 下一章 上一页 下一页 返回目录 目录 加入书签 章节目录 推荐本书".split()
)
AD_KEYWORDS = (                # 命中即视为广告的推广词
    "本站首发", "首发于", "最快更新", "请记住本书", "请收藏本站", "求收藏",
    "求推荐票", "求月票", "手机版阅读网址", "手机阅读", "手机用户请",
    "章节错误", "点此举报", "举报后", "温馨提示", "手机浏览器", "电脑版",
    "加入书签", "推荐本书", "投推荐票", "笔趣", "书海阁", "书号小说",
    "八一中文", "新笔趣", "顶点小说",
)
# 看起来像网址的整行
URL_LINE_RE = re.compile(
    r"^(?:https?://|www\.|wap\.|m\.)\S*$|^[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,6}(?:/\S*)?$",
    re.IGNORECASE,
)
# 纯符号行，或「上一章 | 下一章 | 目录」类导航行
NAV_LINE_RE = re.compile(
    r"^[\s\-—_=*·•|/\\<>《》\[\]【】()（）,，.。:：]+$"
    r"|^(?:上一章|下一章|上一页|下一页|目录|返回目录|加入书签|章节目录|推荐本书)$",
    re.IGNORECASE,
)
NAV_SPLIT_RE = re.compile(r"[\s|/·•<>《》【】\[\]()（）\-—_]+")

# 目录链接文本：匹配「第X章」「第X节」「正文」「楔子」等
CHAPTER_TITLE_RE = re.compile(
    r"^\s*(?:第[0-9零一二三四五六七八九十百千万两〇]+[章节回卷]|正文|楔子|序章|序言|前言|引子)"
)
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
    if title and any(k in title.get_text() for k in ("身份验证", "安全验证", "人机验证")):
        return True
    body_text = soup.get_text(" ")
    return "验证码" in body_text and "请输入" in body_text


def block_text(node) -> str:
    """按文本块取文字：块与块之间换行，块内 <br> 转换行（避免把段落切碎）。"""
    parts: list[str] = []
    for text in node.strings:
        parent_name = text.parent.name if text.parent else ""
        if parent_name in ("br",):
            parts.append("\n")
            continue
        block = parent_name in BLOCK_TAGS
        value = str(text)
        if block:
            parts.append("\n" + value + "\n")
        else:
            parts.append(value)
    return "".join(parts)


def strip_boilerplate(container) -> None:
    """删除正文容器内的广告/脚本/无关元素（就地修改）。"""
    for tag in container.find_all(DROP_TAGS):
        tag.decompose()
    for selector in DROP_SELECTORS:
        for tag in container.select(selector):
            tag.decompose()


def is_ad_line(line: str) -> bool:
    """判断一行是否为广告/导航/网址等非正文内容。"""
    if not line or NAV_LINE_RE.search(line):
        return True
    if URL_LINE_RE.match(line):
        return True
    if len(line) > AD_MAX_LINE_LEN:
        return False
    lowered = line.lower()
    if any(domain in lowered for domain in AD_DOMAINS):
        return True
    if any(keyword in lowered for keyword in AD_KEYWORDS):
        return True
    # 「上一章 | 下一章 | 目录」这类只含导航词的短行
    parts = [p for p in NAV_SPLIT_RE.split(line) if p]
    if parts and all(p in NAV_WORDS for p in parts):
        return True
    # 一行里塞了多个链接（<a> 被 get_text 拼接后）
    if len(re.findall(r"(https?://|www\.)", lowered)) >= 2:
        return True
    return False


def clean_content(raw_text: str) -> str:
    """把正文原始文本清理为干净的段落文本（广告过滤核心）。"""
    text = normalize_text(raw_text)
    # 去掉内联脚本残留，以及残留的 <br>/其它内联标签
    text = re.sub(r"function\s+\w+\s*\([^)]*\)\s*\{[^{}]*\}\s*", "\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[a-zA-Z][^>]{0,80}>", "", text)

    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"[ \t]{2,}", " ", line)
        if is_ad_line(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def extract_content(soup: BeautifulSoup) -> str:
    """从章节页面中提取正文；找不到正文返回空字符串。"""
    container = None
    best_score = -1
    for selector in CONTENT_SELECTORS:
        for node in soup.select(selector):
            text_len = len(re.sub(r"\s", "", node.get_text()))
            if text_len < 50:
                continue
            link_len = sum(len(a.get_text(strip=True)) for a in node.find_all("a"))
            # 跳过链接文字占一半以上的外层布局容器
            if link_len > 0.5 * text_len:
                continue
            if text_len > best_score:
                best_score, container = text_len, node

    if container is not None:
        strip_boilerplate(container)
        text = clean_content(block_text(container))
        if text:
            return text

    # 兜底：整个 body 去杂质后，取连续的长段落
    body = soup.body or soup
    strip_boilerplate(body)
    text = clean_content(block_text(body))
    lines = [line for line in text.split("\n") if len(line) >= 4]
    joined = "\n".join(lines)
    return joined if len(joined) >= 100 else ""


def page_chapter_title(soup: BeautifulSoup) -> str:
    """从章节页提取当前页所属章节的标题。"""
    node = soup.select_one(".bookname h1, h1, #title, .title, .read-article h1, .content h1")
    if node is None:
        title = soup.find("title")
        return title.get_text(" ", strip=True) if title else ""
    return node.get_text(" ", strip=True)


def split_page_url(url: str) -> tuple[str, str, str]:
    """把 URL 拆成 (去掉扩展名的主体, 扩展名, 已有分页后缀)。"""
    match = re.match(r"^(?P<stem>.+?)(?P<ext>\.s?html?)$", url, re.IGNORECASE)
    if not match:
        return url, "", ""
    return match.group("stem"), match.group("ext"), ""


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
    """找章节下一分页：优先页面「下一页」链接，其次目录分页，最后数字后缀。

    position 为 current_url 在 chapter_urls 中的下标；不在其中时为 -1。
    目录只列了第一页时，按 chapter-N.html 规律推测下一页（第 1 章第 1 页的
    常见结构），直到探测不到正文为止。
    """
    # 1) 页面上的「下一页」链接（最可靠）
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True) in ("下一页", "下一頁", "下页", "下頁", "后一页", "后頁"):
            candidate = requests.compat.urljoin(current_url, a["href"])
            if candidate != current_url:
                return candidate
    # 2) 目录页已列出的分页
    if 0 <= position < len(chapter_urls) - 1:
        return chapter_urls[position + 1]
    # 3) 数字后缀：仅对本章已列出 -N 分页的情况生效（避免把 t0yp00p-102.html
    #    误推成 t0yp00p-103.html —— 那是下一章）。
    if not chapter_urls:
        return None
    stem, ext, _ = split_page_url(current_url)
    if not ext:
        return None
    if not any(_has_page_suffix(u) for u in chapter_urls):
        return None
    suffix = _PAGE_SUFFIX_URL_RE.match(stem)
    if suffix is None:
        return f"{stem}-2{ext}"
    page = int(suffix.group("page"))
    if page >= MAX_PAGES_PER_CHAPTER:
        return None
    return f"{suffix.group('base')}-{page + 1}{ext}"


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
    obey_robots: bool = True
    resume: bool = False
    sleep: Callable[[float], None] = time.sleep
    chapters: list[Chapter] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 下载器
# ---------------------------------------------------------------------------


class NovelDownloader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        # 重试在 fetch() 里手动控制，便于验证码流程插入
        adapter = HTTPAdapter(max_retries=Retry(total=0))
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.waf_verified = False
        self._cookies_path = Path(settings.cookies).expanduser()
        self._viewer_opened = False
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
        """GET 页面，自动重试，并处理 WAF 验证码页。"""
        headers = {"Referer": referer} if referer else {}
        last_error = ""
        while _retries <= MAX_RETRIES:
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

            if resp.status_code >= 500 and _retries < MAX_RETRIES:
                _retries += 1
                logging.warning("服务器错误 %s，%s 秒后重试 %d/%d：%s", resp.status_code, RETRY_BACKOFF ** _retries, _retries, MAX_RETRIES, url)
                self.settings.sleep(RETRY_BACKOFF ** _retries)
                continue

            resp.encoding = self._detect_encoding(resp)
            soup = BeautifulSoup(resp.text, "html.parser")
            if is_captcha_page(soup, str(resp.url)):
                logging.info("检测到人机验证页面。")
                self._handle_verification(resp, soup)
                return self.fetch(url, referer=referer, _retries=_retries)
            if not self.waf_verified:
                self.waf_verified = True
                self._save_cookies()
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
        """从目录页解析章节列表。

        规律（与目标站一致）：
        - 目录页顶部常把「最新章节」重复列出一次，因此按 URL 去重；
        - 分页链接形如 xxx-2.html，与 xxx.html 属于同一章：合并到同一章上；
        - 章节按「第X章」中的数字排序，保证「第1章」确实是第一章，
          而不是页面上排在前面的「最新章节」。
        """
        resp = self.fetch(index_url)
        soup = BeautifulSoup(resp.text, "html.parser")
        base_url = str(resp.url)

        anchors: list = []
        for scope in soup.select("#list, .listmain, dl, .chapter-list, .book-chapter, .list, .book_list"):
            anchors.extend(scope.find_all("a", href=True))
        if not anchors:
            anchors = soup.find_all("a", href=True)

        def collect(candidates) -> dict[str, str]:  # url -> 标题
            found: dict[str, str] = {}
            for a in candidates:
                href = a["href"].strip()
                if href.startswith(("javascript:", "#", "mailto:", "tel:")):
                    continue
                url = requests.compat.urljoin(base_url, href)
                if not CHAPTER_URL_RE.match(url):
                    continue
                title = a.get_text(" ", strip=True)
                if not CHAPTER_TITLE_RE.match(title):
                    continue
                found.setdefault(url, title)
            return found

        found = collect(anchors) or collect(soup.find_all("a", href=True))

        # 合并分页：优先按「标题」归组（同一章的多页链接标题相同，
        # 目标站的分页链接形如 t0yp00p-96-2.html，章节 ID 本身也含数字，
        # 因此不能靠 URL 里的数字后缀判断）。
        chapters: dict[str, dict] = {}
        for url, title in found.items():
            key = normalised_title(title) or url
            entry = chapters.setdefault(key, {"title": title, "urls": [], "number": chapter_number(title)})
            entry["urls"].append(url)

        ordered = list(chapters.values())
        for entry in ordered:
            urls = entry["urls"]
            if len(urls) > 1:
                urls.sort(key=lambda u: chapter_page_number(u, tuple(urls)))

        # 有章节号的按序号排序；没有章节号的 (楔子/序章等) 按出现顺序排在前面
        def sort_key(item: tuple[int, dict]) -> tuple:
            position, entry = item
            return (0, position) if entry["number"] is None else (1, entry["number"])

        result = [
            Chapter(entry["title"], tuple(entry["urls"]), index)
            for index, (_, entry) in enumerate(sorted(enumerate(ordered), key=sort_key), 1)
        ]

        logging.info("目录解析完成：共 %d 章。", len(result))
        if not result:
            logging.warning("未能从目录页解析出任何章节，目录结构可能已变化。")
        return result


    # ---- 章节页抓取 -----------------------------------------------------
    def get_chapter_content(self, chapter: Chapter) -> str:
        """下载一章的所有分页并拼接正文。

        分页顺序：页内「下一页」链接 → 目录中同章的其它分页 → （仅当本章
        目录项带 -N 分页格式时）探测 -N+1，并用页面标题确认仍属于本章。
        """
        contents: list[str] = []
        visited: set[str] = set()
        expected = normalised_title(chapter.title)
        current: str | None = chapter.urls[0]
        position = 0

        while current and len(visited) <= MAX_PAGES_PER_CHAPTER:
            if current in visited:
                break
            visited.add(current)
            resp = self.fetch(current, referer=chapter.urls[0])
            soup = BeautifulSoup(resp.text, "html.parser")
            page_title = page_chapter_title(soup)
            # 推测出来的分页/「下一页」链接若标题已属于另一章，说明本章到头了
            if page_title and position == -1 and normalised_title(page_title) != expected:
                break
            text = extract_content(soup)
            if text:
                contents.append(text)
            elif not contents:
                logging.warning("第 %s 章未找到正文：%s", chapter.index, current)

            following = next_page_url(soup, current, chapter_urls=chapter.urls, position=position)
            if following is None and position != -1 and chapter.urls and _has_page_suffix(chapter.urls[-1]):
                following = next_page_url(soup, current, chapter_urls=chapter.urls, position=len(chapter.urls))
            if following is None or following in visited:
                break
            try:
                position = chapter.urls.index(following)
            except ValueError:
                position = -1
            current = following
            self.settings.sleep(self._jitter(min(self.settings.delay, 0.5)))

        return "\n\n".join(contents)

    def _jitter(self, base: float) -> float:
        return base + random.uniform(0, JITTER) if base > 0 else 0.0

    # ---- 主流程 ---------------------------------------------------------
    def check_robots(self) -> bool:
        """检查 robots.txt 是否允许抓取目录页。"""
        parts = urlsplit(INDEX_URL)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=REQUEST_TIMEOUT)
        except (ConnectionError, Timeout):
            return True
        if resp.status_code != 200:
            return True
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser.can_fetch(USER_AGENT, INDEX_URL)

    def iter_positions(self, total: int) -> Iterator[tuple[int, Chapter | None]]:
        """按目录顺序遍历选定区间；超出目录范围的返回 (序号, None)。"""
        last = min(self.settings.end or total, total)
        for number in range(self.settings.start, last + 1):
            pos = number - 1
            yield number, (self.settings.chapters[pos] if pos < total else None)

    def run(self, dry_run: bool = False) -> int:
        settings = self.settings
        logging.info("目标目录：%s", INDEX_URL)
        logging.info("抓取范围：第 %s 章 ~ %s", settings.start, settings.end if settings.end else "末章")

        if settings.obey_robots and not self.check_robots():
            raise RobotsDisallowed("站点 robots.txt 不允许抓取该页面；如确认可访问，请加 --ignore-robots。")

        chapters = self.get_chapter_list(INDEX_URL)
        if not chapters:
            logging.error("目录解析为空，未获取到任何章节。")
            return 2
        settings.chapters = chapters

        targets = dict(self.iter_positions(len(chapters)))
        if dry_run:
            for number, chapter in targets.items():
                if chapter is None:
                    print(f"{number:>4}. [目录中不存在]")
                else:
                    print(f"{number:>4}. {chapter.title}  {chapter.urls[0]}" + (f"  (分页 {len(chapter.urls)})" if len(chapter.urls) > 1 else ""))
            return 0

        last = settings.end if settings.end is not None else len(chapters)
        report = DownloadReport(chapters=chapters)
        if last > len(chapters):
            logging.warning("目录只有 %d 章，第 %d-%d 章不存在。", len(chapters), len(chapters) + 1, last)

        out_path = Path(settings.output)
        done: dict[str, str] = {}
        if settings.resume:
            done = parse_existing_txt(out_path)
            logging.info("断点续传：输出文件中已有 %d 章，将跳过。", len(done))

        collected: dict[int, tuple[str, str]] = {}
        retry_pool: list[tuple[int, Chapter]] = []
        pending = [(n, c) for n, c in targets.items() if c is not None]
        for missing_number, chapter in targets.items():
            if chapter is None:
                report.missing.append(f"第 {missing_number} 章（目录不存在）")

        for step, (number, chapter) in enumerate(pending, 1):
            prefix = f"[{step}/{len(pending)}] 第 {number} 章 {chapter.title}"
            if settings.resume and done.get(chapter_key(chapter.title), "").strip():
                collected[number] = (chapter.title, done[chapter_key(chapter.title)])
                print(f"{prefix}  (已存在，跳过)", flush=True)
                continue
            print(prefix, end="", flush=True)
            try:
                body = self.get_chapter_content(chapter)
            except WafVerificationRequired:
                print()
                raise
            except ConnectionError as exc:
                print("  网络失败", flush=True)
                logging.error("第 %d 章下载失败：%s", number, exc)
                retry_pool.append((number, chapter))
                continue
            except Exception as exc:  # noqa: BLE001 - 单章异常不中断整本书
                print("  解析异常", flush=True)
                logging.error("第 %d 章异常（%s: %s）：%s", number, type(exc).__name__, str(exc)[:120], chapter.urls[0])
                retry_pool.append((number, chapter))
                continue

            if body.strip():
                collected[number] = (chapter.title, body.strip())
                print(f"  {len(body):,} 字", flush=True)
            else:
                print("  正文为空", flush=True)
                retry_pool.append((number, chapter))

            if step % PROGRESS_EVERY == 0:
                chars = sum(len(b) for _, b in collected.values())
                print(f"---- 进度 {step}/{len(pending)}，已获取 {len(collected)} 章 / 约 {chars:,} 字 ----", flush=True)
            settings.sleep(self._jitter(settings.delay))

        # 失败章节再试一轮
        if retry_pool:
            logging.info("重试失败章节：%d 章", len(retry_pool))
            for number, chapter in retry_pool:
                settings.sleep(self._jitter(max(settings.delay * 2, 2.0)))
                print(f"[重试] 第 {number} 章 {chapter.title}", end="", flush=True)
                try:
                    body = self.get_chapter_content(chapter)
                except WafVerificationRequired:
                    print()
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"  仍失败：{type(exc).__name__}: {str(exc)[:120]}", flush=True)
                    report.failed.append((chapter, f"{type(exc).__name__}: {str(exc)[:120]}"))
                    continue
                if body.strip():
                    collected[number] = (chapter.title, body.strip())
                    print(f"  成功 {len(body):,} 字", flush=True)
                else:
                    print("  正文仍为空", flush=True)
                    report.failed.append((chapter, "正文为空"))

        ordered = [(number, *collected[number]) for number in sorted(collected)]
        write_book(out_path, title=out_path.stem.replace("_", " "), ordered=ordered, start=settings.start, end=last)
        summarize(report, ordered, out_path, len(pending))
        return 0 if not report.failed else 1


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


def summarize(report: DownloadReport, ordered: list[tuple[int, str, str]], path: Path, requested: int) -> None:
    chars = sum(len(body) for _, _, body in ordered)
    print()
    print("=" * 62)
    print(f"完成：应有 {requested} 章，成功 {len(ordered)} 章，失败 {len(report.failed)} 章。")
    print(f"正文约 {chars:,} 字")
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
    parser = argparse.ArgumentParser(description="从小说站点抓取章节并合并为 TXT 文件。")
    parser.add_argument("--start", type=int, default=DEFAULT_START, help=f"起始章节（默认 {DEFAULT_START}）")
    parser.add_argument("--end", type=int, default=DEFAULT_END, help=f"结束章节，含（默认 {DEFAULT_END}）")
    parser.add_argument("--all", action="store_true", help="抓取目录中的全部章节（忽略 --start/--end）")
    parser.add_argument("-o", "--output", default=None, help="输出文件路径")
    parser.add_argument("--delay", type=float, default=CHAPTER_DELAY, help=f"章节请求间隔秒数（默认 {CHAPTER_DELAY}）")
    parser.add_argument("--cookies", default="cookies.json", help="WAF 验证 Cookie 保存路径（默认 cookies.json）")
    parser.add_argument("--dry-run", action="store_true", help="只解析目录并打印章节列表，不下载正文")
    parser.add_argument("--resume", action="store_true", help="断点续传：跳过输出文件中已有正文的章节")
    parser.add_argument("--ignore-robots", action="store_true", help="忽略 robots.txt 限制")
    parser.add_argument("--base-url", default=None, help="站点根地址（默认使用脚本内置目标站，测试用）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def build_settings(args: argparse.Namespace) -> Settings:
    if args.all:
        start, end = 1, None
        output = args.output or "小说_全部章节.txt"
    else:
        if args.start < 1:
            raise ValueError("--start 必须 >= 1")
        if args.end is not None and args.end < args.start:
            raise ValueError("--end 不能小于 --start")
        start, end = args.start, args.end
        output = args.output or f"小说_{start}-{end if end is not None else '末' }章.txt"
    return Settings(
        start=start,
        end=end,
        output=output,
        cookies=args.cookies,
        delay=max(0.0, args.delay),
        obey_robots=not args.ignore_robots,
        resume=args.resume,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    if args.base_url:
        global BASE_URL, INDEX_URL
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


if __name__ == "__main__":
    sys.exit(main())
