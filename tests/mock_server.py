# -*- coding: utf-8 -*-
"""本地模拟站点：还原 GoEdge WAF 验证码 + 小说目录/章节/广告结构。

用于在不依赖真实网站的情况下验证 novel_download.py 的完整流程：

    python tests/mock_server.py            # 默认监听 127.0.0.1:8765
    python novel_download.py --base-url http://127.0.0.1:8765 ...

模拟要素：
- 未通过验证码时，所有页面 307 跳转到 /WAF/VERIFY/CAPTCHA（与真实站点一致）；
- 验证码图片为动态生成的 PNG（code 字段在页面源码里，便于自动测试）；
- 目录页使用与目标站同款的 dl/dd 结构，含 120 章；
- 第 7 章为「-2/-3」后缀三页章节；第 9 章的第 2 页链接写在「下一页」；
- 章节页混入 script、ins 广告、底部推广、链接、HTML 注释、内联广告等。
"""

from __future__ import annotations

import html
import random
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

TOTAL_CHAPTERS = 120
BOOK_ID = "t0yp00p"
CHAPTER_CODES = [f"t0yp00p-{96 + i}" for i in range(TOTAL_CHAPTERS)]  # t0yp00p-96.html ...
SITE_NAME = "书海阁"

CN_NUMS = "零一二三四五六七八九"


def cn_number(n: int) -> str:
    """1 -> 一，15 -> 十五，20 -> 二十，105 -> 一百零五，120 -> 一百二十。"""
    if n < 10:
        return CN_NUMS[n]
    if n < 20:
        return "十" + (CN_NUMS[n % 10] if n % 10 else "")
    if n < 100:
        return CN_NUMS[n // 10] + "十" + (CN_NUMS[n % 10] if n % 10 else "")
    hundreds, rest = divmod(n, 100)
    if rest == 0:
        return CN_NUMS[hundreds] + "百"
    if rest < 10:
        return CN_NUMS[hundreds] + "百零" + CN_NUMS[rest]
    return CN_NUMS[hundreds] + "百" + cn_number(rest)


def chapter_title(n: int) -> str:
    return f"第{cn_number(n)}章 测试章节{n}"


def chapter_code(n: int) -> str:
    return CHAPTER_CODES[n - 1]


def paragraph(n: int, p: int) -> str:
    return (
        f"这是第{n}章的第{p}段正文。青云宗山门前的积雪尚未化尽，少年提剑而立，"
        f"远处传来晨钟之声。他抬头望向云海，心中默念「非淡泊无以明志，非宁静无以致远」。"
    )


def chapter_pages(n: int) -> dict[str, str]:
    """返回 {分页 URL 路径 -> HTML}，按分页顺序。"""
    code = chapter_code(n)
    total_pages = {7: 3, 9: 2}.get(n, 1)
    pages: dict[str, str] = {}
    for page in range(1, total_pages + 1):
        path = f"/book/{code}.html" if page == 1 else f"/book/{code}-{page}.html"
        pages[path] = chapter_html(n, page, total_pages)
    return pages


def chapter_html(n: int, page: int, total_pages: int) -> str:
    code = chapter_code(n)
    title = chapter_title(n)
    body = "\n".join(
        f"<br>\u3000\u3000{paragraph(n, p)}" for p in range((page - 1) * 3 + 1, page * 3 + 1)
    )
    next_link = ""
    if page == 1 and total_pages > 1:
        next_link = f'<a href="/book/{code}-2.html" id="pb_next">下一页</a>'
    if total_pages == 1:
        next_link = '<a href="/book/%s-2.html" id="pb_next">下一页</a>' % code
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)} - {SITE_NAME}</title>
<script src="/static/footer.js"></script>
</head>
<body>
<div class="header"><a href="/">首页</a> <a href="/book/{BOOK_ID}.html">目录</a></div>
<div class="bookname"><h1>{html.escape(title)}</h1>
  <div class="bottem1"><a href="/book/{BOOK_ID}.html">章节目录</a> | 下一页</div>
</div>
<div class="content read-content">
<script>var chapterId = {n};</script>
<p>{body}</p>
<!-- 广告位：chapter-bottom -->
<ins class="adsbygoogle" data-ad-client="ca-pub-1234">广告</ins>
<div class="read-inline-img"><a href="https://ad.example.net/download">点击下载APP，阅读更流畅</a></div>
<p style="text-align:center">本站首发于{html.escape(SITE_NAME)}网，希望读者收藏。</p>
<p>{SITE_NAME}网最快更新{html.escape(title)}，请记住本书首发域名：www.shuhaoxs.com</p>
<p>手机版阅读网址：m.shuhaoxs.com</p>
<p>章节错误，点此举报</p>
<p>\u3000\u3000{paragraph(n, 99)}</p>
<script>document.write('<a href="https://bad.example.com">猜你喜欢</a>')</script>
</div>
<div class="bottem2">
  <a href="/book/{code}.html">上一章</a> | <a href="/book/{BOOK_ID}.html">返回目录</a> | {next_link}
</div>
<div class="footer">Copyright <a href="https://www.example.com">示例站</a></div>
</body>
</html>"""


def index_html() -> str:
    dds = []
    for n in range(1, TOTAL_CHAPTERS + 1):
        code = chapter_code(n)
        detail = f"<dd><a href=\"/book/{code}.html\">{html.escape(chapter_title(n))}</a></dd>"
        dds.append(detail)
        if n == 7:  # 多页章节：目录同时列出 3 个分页
            dds.append(f"<dd><a href=\"/book/{code}-2.html\">{html.escape(chapter_title(n))}（2）</a></dd>")
            dds.append(f"<dd><a href=\"/book/{code}-3.html\">{html.escape(chapter_title(n))}（3）</a></dd>")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>测试小说 - {SITE_NAME}</title></head>
<body>
<div class="bookinfo"><h1>测试小说</h1><p>作者：测试作者</p></div>
<div id="list">
<dl>
<dt>《测试小说》最新章节</dt>
<dd><a href="/book/{chapter_code(120)}.html">{html.escape(chapter_title(120))}</a></dd>
<dt>《测试小说》正文卷</dt>
{"".join(dds)}
</dl>
</div>
<div class="footer"><a href="/">首页</a></div>
</body></html>"""


def captcha_html(info: str, captcha_id: str, wrong: bool = False) -> str:
    """GoEdge WAF 验证页：验证码只存在于图片中（与真实站点一致）。"""
    prompt = '<p id="ui-captcha-image-prompt">验证码错误，请重试</p>' if wrong else '<p id="ui-captcha-image-prompt">loading ...</p>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><title>身份验证</title><meta charset="UTF-8"/>
<script>var isValidated=!1;window.addEventListener("load",function(){{document.getElementById("GOEDGE_WAF_CAPTCHA_CODE").focus();}});</script>
</head>
<body><form method="POST" id="captcha-form" action="/WAF/VERIFY/CAPTCHA?info={info}">
\t<input type="hidden" name="GOEDGE_WAF_CAPTCHA_ID" value="{captcha_id}"/>
\t<div class="ui-image">{prompt}
\t\t<img id="ui-captcha-image" src="/WAF/VERIFY/CAPTCHA?info={info}&GOEDGE_WAF_CAPTCHA_ID={captcha_id}-img" alt=""/>
\t</div>
\t<div class="ui-input"><p class="ui-prompt">请输入上面的验证码</p>
\t\t<input type="text" name="GOEDGE_WAF_CAPTCHA_CODE" id="GOEDGE_WAF_CAPTCHA_CODE" size="10" maxlength="6" class="input"/>
\t</div>
\t<div class="ui-button"><button type="submit">提交验证</button></div>
</form>
<address>请求ID: mock</address></body></html>"""


def make_png(width: int = 120, height: int = 40, seed: int = 0) -> bytes:
    """生成一张带噪声的 PNG（模拟验证码图片），用于测试终端渲染。"""
    rng = random.Random(seed)
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            if x % 12 < 2 or y % 14 < 2:
                raw.extend([30, 30, 30] if (x + y) % 7 else [200, 200, 255])
            else:
                raw.extend([245, 245, rng.randint(230, 255)] if rng.random() > 0.05 else [40, 40, 40])

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


class MockState:
    def __init__(self, total: int = TOTAL_CHAPTERS) -> None:
        self.total = total
        self.lock = threading.Lock()
        self.pass_token = "MOCKPASS"
        # 与真实站点一致：验证码在服务端生成、只出现在图片里；
        # 测试通过 state.captcha_code 获取当前验证码用于模拟用户输入。
        self.captcha_code = "1234"
        self.challenge = "cid"
        self.requests: list[str] = []
        self.fail_once: set[str] = set()  # 这些路径第一次返回 503
        self.sleep_pages: set[str] = set()  # 这些路径每次都返回 503


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: MockState
    verbose = False

    def log_message(self, fmt, *args):  # noqa: D401
        if self.verbose:
            print("mock:", self.command, self.path)

    # ---- helpers -------------------------------------------------------
    def send_html(self, body: str, status: int = 200, extra_headers: dict | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        return self.state.pass_token in self.headers.get("Cookie", "")

    def redirect_to_captcha(self, path: str) -> None:
        info = "mockinfo"
        location = "/WAF/VERIFY/CAPTCHA?" + urlencode({"info": info, "from": path})
        self.send_response(307)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- routing -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        self.state.requests.append(path)

        if path == "/robots.txt":
            body = "User-agent: *\nAllow: /\nDisallow: /WAF/\n"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/WAF/VERIFY/CAPTCHA"):
            info = (query.get("info") or ["mockinfo"])[0]
            captcha_id = (query.get("GOEDGE_WAF_CAPTCHA_ID") or [self.state.challenge])[0]
            if captcha_id.endswith("-img") or self.headers.get("Accept", "").startswith("image/"):
                data = make_png(seed=abs(hash(captcha_id)) & 0xFFFF)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_html(captcha_html(info, captcha_id))
            return

        if not self.authorized():
            self.redirect_to_captcha(path)
            return

        if path in self.state.sleep_pages:
            self.send_html("service unavailable", status=503)
            return
        if path in self.state.fail_once:
            self.state.fail_once.discard(path)
            self.send_html("temporarily unavailable", status=503)
            return

        if path == f"/book/{BOOK_ID}.html":
            self.send_html(index_html())
            return

        for n in range(1, self.state.total + 1):
            if path in chapter_pages(n):
                self.send_html(chapter_pages(n)[path])
                return

        self.send_html("<html><head><title>404</title></head><body>页面不存在</body></html>", status=404)

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {k: v[0] for k, v in parse_qs(raw).items()}
        if not parts.path.startswith("/WAF/VERIFY/CAPTCHA"):
            self.send_html("not found", status=404)
            return
        code = form.get("GOEDGE_WAF_CAPTCHA_CODE", "")
        if code == self.state.captcha_code:
            target = (form.get("REDIRECT_TO") or (parse_qs(parts.query).get("from") or ["/"])[0]) or "/"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Set-Cookie", f"WAF_PASS={self.state.pass_token}; Path=/; HttpOnly")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        query = parse_qs(parts.query)
        info = (query.get("info") or ["mockinfo"])[0]
        self.state.challenge = "cid-new"
        self.state.captcha_code = "5678"
        self.send_html(captcha_html(info, self.state.challenge, wrong=True))


def start_in_thread(state: MockState | None = None, port: int = 0) -> tuple[ThreadingHTTPServer, MockState, str]:
    state = state or MockState()
    MockHandler.verbose = __name__ == "__main__"
    handler = type("BoundMockHandler", (MockHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, state, base_url


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server, state, base = start_in_thread(port=port)
    print(f"模拟站点已启动：{base}/book/{BOOK_ID}.html （验证码：{state.captcha_code}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
