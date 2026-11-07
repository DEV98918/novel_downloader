# -*- coding: utf-8 -*-
"""分章区间的目录接口（v0.31+）。

不少小说站的目录页只列出默认区间（如「1-100章」），其余章节要通过页面上
的分章下拉菜单（1-100 / 101-200 / ...）由 JS 异步加载。以 shuhaoxs 为例：

- 下拉结构：``div.dropDown[data-aid="BOOKID"] > ul > li[data-p="N"]``；
- 点击后前端把 ``{id: 书籍ID, page: N}`` 用 AES-CBC 加密，POST 到
  ``/index.php?action=loadChapterPage``，返回 JSON 章节列表；
- 加密参数（密钥/IV）就写在站点自己的 JS 里（``AESCrypt`` / ``chapterPageAjxa``）。

本模块负责：
1. 从目录页 HTML 中识别分章下拉菜单；
2. 识别站点提供的「加密接口」配置（自动从站点 JS 中读取，或由内置规则提供）；
3. 依次抓取全部区间并解析为章节列表。

模块不依赖网络库，HTTP 请求由调用方以回调形式传入，便于离线测试。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

JsonRequest = Callable[[str, dict[str, str], dict[str, str]], dict]
"""请求回调：request(url, form_data, extra_headers) -> 解析后的 JSON。"""


@dataclass
class RangeOption:
    """分章下拉菜单中的一项。"""

    page: int
    label: str


@dataclass
class EncryptedApi:
    """一个「加密参数 + 接口地址」的目录接口描述。"""

    endpoint: str
    key: bytes
    iv: bytes
    # 明文结构里的字段名，默认为 {"id": ..., "page": ...}
    id_field: str = "id"
    page_field: str = "page"
    # 加密后 POST 的字段名
    data_field: str = "data"
    # 章节列表在 JSON 中的字段名与章节字段名
    items_field: str = "data"
    name_field: str = "chaptername"
    url_field: str = "chapterurl"


@dataclass
class ChapterApiResult:
    """接口抓取结果。"""

    chapters: list[tuple[str, str]] = field(default_factory=list)  # (标题, URL)
    pages_fetched: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 识别分章下拉菜单
# ---------------------------------------------------------------------------

_DROPDOWN_SELECTORS = (
    ".selBox .dropDown",
    ".dropDown",
    ".chapter-page .dropdown",
    ".pageS .dropDown",
    "select.chapter-range",
)


def find_range_menu(soup: BeautifulSoup) -> tuple[str, list[RangeOption]] | None:
    """从目录页识别分章下拉菜单，返回 (书籍ID, 选项列表)；没有则返回 None。

    只认「选项数量 >= 2 且每项带页码」的菜单，避免把普通导航误判为分章菜单。
    """
    for selector in _DROPDOWN_SELECTORS:
        for node in soup.select(selector):
            options = _parse_options(node)
            if len(options) < 2:
                continue
            book_id = (node.get("data-aid") or "").strip() or _book_id_from_soup(soup)
            if book_id:
                return book_id, options
    return None


def _parse_options(node) -> list[RangeOption]:
    """解析下拉项：<li data-p="1">1-100章</li> 或 <option value="1">1-100章</option>。"""
    options: list[RangeOption] = []
    for li in node.select("li[data-p], li[data-page], li[data-id]"):
        raw = li.get("data-p") or li.get("data-page") or li.get("data-id")
        label = li.get_text(" ", strip=True)
        try:
            page = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        options.append(RangeOption(page, label))
    if options:
        return options
    for opt in node.select("option[value]"):
        try:
            page = int(str(opt.get("value")).strip())
        except (TypeError, ValueError):
            continue
        options.append(RangeOption(page, opt.get_text(" ", strip=True)))
    return options


def _book_id_from_soup(soup: BeautifulSoup) -> str:
    """从 URL / 页面结构推断书名 ID，例如 /book/t0yp00p.html -> t0yp00p。"""
    for a in soup.find_all("a", href=True):
        m = re.search(r"/(?:book|chapter)/([A-Za-z0-9]+?)(?:[_-]\d+)?\.html", a["href"])
        if m:
            return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# 站点加密接口配置
# ---------------------------------------------------------------------------

# 已知站点配置：密钥/IV 来自站点自身 JS（AESCrypt）
KNOWN_APIS: dict[str, EncryptedApi] = {
    "shuhaoxs.com": EncryptedApi(
        endpoint="/index.php?action=loadChapterPage",
        key=b"1MjESVfMv07us8KcnXmjGx0Ohnqo2VHJ",
        iv=b"2915951533410855",
    ),
}

_KEY_RE = re.compile(r"new\s+AESCrypt\(\s*['\"](?P<key>[^'\"]{16,64})['\"]")
_IV_RE = re.compile(
    r"iv\s*=\s*CryptoJS\.enc\.Utf8\.parse\(\s*['\"](?P<iv>[^'\"]{16})['\"]"
)
_ENDPOINT_RE = re.compile(r"""['"](?P<url>/[A-Za-z0-9_\-/\.]*\?action=[A-Za-z0-9_]+)['"]""")


def api_for_host(host: str) -> EncryptedApi | None:
    """按主机名（含子域）查找已知的加密接口配置。

    除内置配置外，还支持通过环境变量 ``NOVEL_RANGE_API`` 提供自定义配置
    （JSON：{"endpoint":..., "key":..., "iv":...}），便于适配其它站点与测试。
    """
    host = (host or "").lower().split(":")[0]
    for domain, api in KNOWN_APIS.items():
        if host == domain or host.endswith("." + domain):
            return api
    return api_from_env()


def api_from_env() -> EncryptedApi | None:
    """从环境变量 NOVEL_RANGE_API 读取接口配置。"""
    import os

    raw = os.environ.get("NOVEL_RANGE_API")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return EncryptedApi(
            endpoint=str(data["endpoint"]),
            key=str(data["key"]).encode(),
            iv=str(data["iv"]).encode(),
        )
    except (ValueError, KeyError, TypeError) as exc:
        logging.warning("环境变量 NOVEL_RANGE_API 格式错误（%s），已忽略。", exc)
        return None


def parse_api_from_js(js_text: str) -> EncryptedApi | None:
    """从站点 JS 源码中解析出 AESCrypt 参数与接口地址。

    站点把自己的加密参数写在前端 JS 里（如 ``public.js``/``AESCrypt``），
    因此可以参考站点 JS 自动生成配置。识别失败时返回 None。
    """
    key_match = _KEY_RE.search(js_text)
    iv_match = _IV_RE.search(js_text)
    if not key_match or not iv_match:
        return None
    endpoint = ""
    for m in _ENDPOINT_RE.finditer(js_text):
        if "action=" in m.group("url"):
            endpoint = m.group("url")
            break
    if not endpoint:
        return None
    try:
        key = key_match.group("key").encode()
        iv = iv_match.group("iv").encode()
    except UnicodeEncodeError:
        return None
    if len(key) not in (16, 24, 32) or len(iv) != 16:
        return None
    return EncryptedApi(endpoint=endpoint, key=key, iv=iv)


# ---------------------------------------------------------------------------
# 加密与请求
# ---------------------------------------------------------------------------


def encrypt_payload(api: EncryptedApi, book_id: str, page: int) -> str:
    """按站点规则加密请求参数（AES-CBC + PKCS7 + Base64）。"""
    from Crypto.Cipher import AES  # 延迟导入：只有用到加密接口时才需要
    from Crypto.Util.Padding import pad

    plain = json.dumps(
        {api.id_field: book_id, api.page_field: page},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    cipher = AES.new(api.key, AES.MODE_CBC, api.iv)
    return base64.b64encode(cipher.encrypt(pad(plain, 16))).decode("ascii")


def decrypt_payload(api: EncryptedApi, token: str) -> dict:
    """反向解密（用于测试与排查）。"""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    raw = AES.new(api.key, AES.MODE_CBC, api.iv).decrypt(base64.b64decode(token))
    return json.loads(unpad(raw, 16).decode("utf-8"))


def fetch_chapters(
    api: EncryptedApi,
    book_id: str,
    *,
    request_json: JsonRequest,
    pages: list[int],
    base_url: str = "",
    referer: str = "",
) -> ChapterApiResult:
    """依次请求各分章区间，汇总章节列表。"""
    result = ChapterApiResult()
    seen: set[str] = set()
    url = base_url.rstrip("/") + api.endpoint
    headers = {"Referer": referer} if referer else {}
    for page in pages:
        if page <= 0:
            continue
        try:
            token = encrypt_payload(api, book_id, page)
        except ImportError:
            result.errors.append("缺少加密依赖 pycryptodome，无法请求分章接口")
            break
        try:
            payload = request_json(url, {api.data_field: token}, headers)
        except Exception as exc:  # noqa: BLE001 - 网络/解析错误都记录后继续
            result.errors.append(f"区间 page={page} 请求失败：{type(exc).__name__}: {str(exc)[:120]}")
            continue
        result.pages_fetched += 1
        if payload.get("code") not in (0, "0", None):
            result.errors.append(f"区间 page={page} 返回错误：{payload.get('msg')}")
            continue
        items = payload.get(api.items_field) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get(api.name_field) or "").strip()
            href = str(item.get(api.url_field) or "").strip()
            if not title or not href or href in seen:
                continue
            seen.add(href)
            result.chapters.append((title, href))
    return result


def discover_pages(
    api: EncryptedApi,
    book_id: str,
    *,
    request_json: JsonRequest,
    base_url: str = "",
    referer: str = "",
    max_pages: int = 100,
) -> list[int]:
    """探测接口实际有多少个区间：连续请求直到某页没有新章节。"""
    url = base_url.rstrip("/") + api.endpoint
    headers = {"Referer": referer} if referer else {}
    seen: set[str] = set()
    pages: list[int] = []
    for page in range(1, max_pages + 1):
        try:
            token = encrypt_payload(api, book_id, page)
            payload = request_json(url, {api.data_field: token}, headers)
        except ImportError:
            raise
        except Exception as exc:  # noqa: BLE001
            logging.debug("探测分章区间 page=%d 失败：%s", page, exc)
            break
        items = payload.get(api.items_field) or []
        fresh = [
            i for i in items
            if isinstance(i, dict) and str(i.get(api.url_field) or "") not in seen
        ]
        if not fresh:
            break
        for item in fresh:
            seen.add(str(item.get(api.url_field)))
        pages.append(page)
    return pages
