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


class MissingCryptoDependency(RuntimeError):
    """没有任何可用的 AES 实现（正常情况下不会发生）。"""


class RangeApiError(RuntimeError):
    """识别到分章区间，但接口一个区间也没取到章节。"""


# ---------------------------------------------------------------------------
# AES-CBC 加密：多后端实现，缺 pycryptodome 也能工作
# ---------------------------------------------------------------------------
# 依次尝试：
#   1) pycryptodome  (pip install pycryptodome)
#   2) cryptography  (pip install cryptography；很多发行版自带)
#   3) 内置纯 Python AES（无任何依赖，速度稍慢但完全够用）
# 三者结果完全一致：AES-CBC + PKCS7 + Base64。


def _encrypt_pycryptodome(key: bytes, iv: bytes, plain: bytes) -> bytes:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plain, 16))


def _decrypt_pycryptodome(key: bytes, iv: bytes, data: bytes) -> bytes:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(data), 16)


def _encrypt_cryptography(key: bytes, iv: bytes, plain: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padded = _pkcs7_pad(plain)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _decrypt_cryptography(key: bytes, iv: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return _pkcs7_unpad(decryptor.update(data) + decryptor.finalize())


def _encrypt_pure(key: bytes, iv: bytes, plain: bytes) -> bytes:
    return _aes_cbc_encrypt(key, iv, _pkcs7_pad(plain))


def _decrypt_pure(key: bytes, iv: bytes, data: bytes) -> bytes:
    return _pkcs7_unpad(_aes_cbc_decrypt(key, iv, data))


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad_len = block - len(data) % block
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes, block: int = 16) -> bytes:
    if not data or len(data) % block:
        raise ValueError("bad PKCS7 data")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > block:
        raise ValueError("bad PKCS7 padding")
    return data[:-pad_len]


# ---- 纯 Python AES（FIPS-197）--------------------------------------------


def _build_sbox() -> tuple[list[int], list[int]]:
    sbox = [0] * 256
    inv = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q
        for shift in (1, 2, 3, 4):
            x ^= ((q << shift) | (q >> (8 - shift))) & 0xFF
        sbox[p] = x ^ 0x63
        inv[sbox[p]] = p
        if p == 1:
            break
    sbox[0] = 0x63
    inv[0x63] = 0
    return sbox, inv


_SBOX, _INV_SBOX = _build_sbox()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _gmul(a: int, b: int) -> int:
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _expand_key(key: bytes) -> list[list[int]]:
    nk = len(key) // 4
    rounds = {4: 10, 6: 12, 8: 14}[nk]
    words = [list(key[i * 4:(i + 1) * 4]) for i in range(nk)]
    for i in range(nk, 4 * (rounds + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [sum(words[r * 4:(r + 1) * 4], []) for r in range(rounds + 1)]


def _shift_rows(state: list[int], inverse: bool = False) -> list[int]:
    out = list(state)
    for r in range(1, 4):
        row = [state[c * 4 + r] for c in range(4)]
        row = (row[-r:] + row[:-r]) if inverse else (row[r:] + row[:r])
        for c in range(4):
            out[c * 4 + r] = row[c]
    return out


_MUL2 = [_gmul(i, 2) for i in range(256)]
_MUL3 = [_gmul(i, 3) for i in range(256)]
_MUL9 = [_gmul(i, 9) for i in range(256)]
_MUL11 = [_gmul(i, 11) for i in range(256)]
_MUL13 = [_gmul(i, 13) for i in range(256)]
_MUL14 = [_gmul(i, 14) for i in range(256)]


def _mix_columns(state: list[int], inverse: bool = False) -> list[int]:
    out = list(state)
    for c in range(4):
        a0, a1, a2, a3 = state[c * 4:c * 4 + 4]
        if inverse:
            out[c * 4:c * 4 + 4] = [
                _MUL14[a0] ^ _MUL11[a1] ^ _MUL13[a2] ^ _MUL9[a3],
                _MUL9[a0] ^ _MUL14[a1] ^ _MUL11[a2] ^ _MUL13[a3],
                _MUL13[a0] ^ _MUL9[a1] ^ _MUL14[a2] ^ _MUL11[a3],
                _MUL11[a0] ^ _MUL13[a1] ^ _MUL9[a2] ^ _MUL14[a3],
            ]
        else:
            out[c * 4:c * 4 + 4] = [
                _MUL2[a0] ^ _MUL3[a1] ^ a2 ^ a3,
                a0 ^ _MUL2[a1] ^ _MUL3[a2] ^ a3,
                a0 ^ a1 ^ _MUL2[a2] ^ _MUL3[a3],
                _MUL3[a0] ^ a1 ^ a2 ^ _MUL2[a3],
            ]
    return out


def _encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = [b ^ k for b, k in zip(block, round_keys[0])]
    for rnd in range(1, len(round_keys) - 1):
        state = [_SBOX[b] for b in state]
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = [b ^ k for b, k in zip(state, round_keys[rnd])]
    state = [_SBOX[b] for b in state]
    state = _shift_rows(state)
    state = [b ^ k for b, k in zip(state, round_keys[-1])]
    return bytes(state)


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = [b ^ k for b, k in zip(block, round_keys[-1])]
    for rnd in range(len(round_keys) - 2, 0, -1):
        state = _shift_rows(state, inverse=True)
        state = [_INV_SBOX[b] for b in state]
        state = [b ^ k for b, k in zip(state, round_keys[rnd])]
        state = _mix_columns(state, inverse=True)
    state = _shift_rows(state, inverse=True)
    state = [_INV_SBOX[b] for b in state]
    state = [b ^ k for b, k in zip(state, round_keys[0])]
    return bytes(state)


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    round_keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        prev = _encrypt_block(bytes(a ^ b for a, b in zip(data[i:i + 16], prev)), round_keys)
        out += prev
    return bytes(out)


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    round_keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, round_keys), prev))
        prev = block
    return bytes(out)


# ---- 后端选择 -------------------------------------------------------------

_BACKENDS: list[tuple[str, object, object]] | None = None


def _backends() -> list[tuple[str, object, object]]:
    """可用的 (名称, 加密, 解密)，按优先级排序。内置实现保证至少有一项。"""
    global _BACKENDS
    if _BACKENDS is not None:
        return _BACKENDS
    found: list[tuple[str, object, object]] = []
    try:  # 自检：能真正跑通才采用该后端
        if _encrypt_pycryptodome(b"k" * 16, b"i" * 16, b"probe" * 4):
            found.append(("pycryptodome", _encrypt_pycryptodome, _decrypt_pycryptodome))
    except Exception:  # noqa: BLE001
        pass
    try:
        if _encrypt_cryptography(b"k" * 16, b"i" * 16, b"probe" * 4):
            found.append(("cryptography", _encrypt_cryptography, _decrypt_cryptography))
    except Exception:  # noqa: BLE001
        pass
    found.append(("pure-python", _encrypt_pure, _decrypt_pure))
    _BACKENDS = found
    return found


def crypto_backend() -> str:
    """当前使用的加密后端名称。"""
    return str(_backends()[0][0])


def crypto_available() -> bool:
    """是否具备加密能力（内置纯 Python 实现，正常情况下恒为 True）。"""
    return bool(_backends())


def require_crypto() -> None:
    """确保加密能力可用。"""
    if not crypto_available():  # pragma: no cover
        raise MissingCryptoDependency("没有可用的 AES 实现")


def encrypt_payload(api: EncryptedApi, book_id: str, page: int) -> str:
    """按站点规则加密请求参数（AES-CBC + PKCS7 + Base64）。"""
    plain = json.dumps(
        {api.id_field: book_id, api.page_field: page},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    _, encrypt, _ = _backends()[0]
    return base64.b64encode(encrypt(api.key, api.iv, plain)).decode("ascii")  # type: ignore[operator]


def decrypt_payload(api: EncryptedApi, token: str) -> dict:
    """反向解密（用于测试与排查）。"""
    _, _, decrypt = _backends()[0]
    raw = decrypt(api.key, api.iv, base64.b64decode(token))  # type: ignore[operator]
    return json.loads(raw.decode("utf-8"))


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
        token = encrypt_payload(api, book_id, page)  # 缺依赖时抛出，由调用方处理
        try:
            payload = request_json(url, {api.data_field: token}, headers)
        except Exception as exc:  # noqa: BLE001 - 网络/解析错误都记录后继续
            result.errors.append(f"区间 page={page} 请求失败：{type(exc).__name__}: {str(exc)[:120]}")
            continue
        result.pages_fetched += 1
        if payload.get("code") not in (0, "0", None):
            msg = str(payload.get("msg") or payload)
            hint = ""
            if any(k in msg.lower() for k in ("decrypt", "decryption")):
                # 服务端解密失败：几乎总是加密参数（密钥/IV）与站点不一致
                hint = (
                    "（服务端无法解密请求参数：站点的加密密钥/IV 可能已更新。"
                    "可用环境变量 NOVEL_RANGE_API 覆盖配置，见 README）"
                )
            result.errors.append(f"区间 page={page} 返回错误：{msg}{hint}")
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
