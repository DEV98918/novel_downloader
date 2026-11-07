# -*- coding: utf-8 -*-
"""novel_download.py 的离线测试（基于 tests/mock_server.py 的模拟站点）。

运行：
    ./venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import builtins
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_server  # noqa: E402
import novel_download as nd  # noqa: E402
from mock_server import start_in_thread  # noqa: E402


class MockSiteTestCase(unittest.TestCase):
    """每个测试启动一个独立的模拟站点。"""

    def setUp(self) -> None:
        self.server, self.state, self.base = start_in_thread()
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)
        # 模拟用户在终端里输入正确的验证码
        patcher = mock.patch.object(builtins, "input", side_effect=lambda *_: self.state.captcha_code)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_settings(self, **overrides) -> nd.Settings:
        params = dict(
            start=1,
            end=100,
            output=str(self.workdir / "out.txt"),
            cookies=str(self.workdir / "cookies.json"),
            delay=0,
            obey_robots=False,
            resume=False,
            sleep=lambda _s: None,
        )
        params.update(overrides)
        return nd.Settings(**params)

    def run_cli(self, *args: str) -> tuple[int, str]:
        argv = [
            "--base-url", self.base,
            "--cookies", str(self.workdir / "cookies.json"),
            "--delay", "0",
            "--ignore-robots",
            *args,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = nd.main(argv)
        return code, out.getvalue() + err.getvalue()


class UnitTests(unittest.TestCase):
    def test_clean_content_removes_ads_keeps_text(self):
        raw = """
        　　「非淡泊无以明志，非宁静无以致远。」少年提剑而立。
        本站首发于书海阁网，希望读者收藏。
        手机版阅读网址：m.shuhaoxs.com
        上一章 | 返回目录 | 下一章
        www.shuhaoxs.com
        http://ad.example.com/x
        章节错误，点此举报
        　　他抬头望向云海，心中一片宁静。
        """
        cleaned = nd.clean_content(raw)
        self.assertIn("非淡泊无以明志", cleaned)
        self.assertIn("他抬头望向云海", cleaned)
        for ad in ("本站首发", "手机版阅读网址", "上一章", "www.shuhaoxs.com", "ad.example.com", "举报"):
            self.assertNotIn(ad, cleaned)

    def test_clean_content_keeps_long_paragraph_with_incidental_word(self):
        line = "他们一路疾驰，终于在天亮前抵达了那座小城，" * 3 + "城门上写着「www」三个字（" + "x" * 40 + "）"
        self.assertFalse(nd.is_ad_line(line))

    def test_chapter_title_regex(self):
        for title in ("第一章 起点", "第12章 abc", "第一百零五章 结局", "正文 楔子", "序章"):
            self.assertTrue(nd.CHAPTER_TITLE_RE.match(title), title)
        for title in ("最新章节", "作品相关", "作者的话", "首页"):
            self.assertFalse(nd.CHAPTER_TITLE_RE.match(title), title)

    def test_next_page_urls(self):
        soup = nd.BeautifulSoup('<a href="/b/c-2.html">下一页</a>', "html.parser")
        # 页内「下一页」链接最可靠
        self.assertEqual(nd.next_page_url(soup, "http://x/b/c.html", chapter_urls=(), position=-1), "http://x/b/c-2.html")

        soup2 = nd.BeautifulSoup("<p>无链接</p>", "html.parser")
        # 目录列出的分页
        paged = ("http://x/b/c.html", "http://x/b/c-2.html", "http://x/b/c-3.html")
        self.assertEqual(nd.next_page_url(soup2, paged[0], chapter_urls=paged, position=0), paged[1])
        self.assertEqual(nd.next_page_url(soup2, paged[1], chapter_urls=paged, position=1), paged[2])
        # 已是最后一页：触发后缀探测（position == len）
        self.assertEqual(nd.next_page_url(soup2, paged[2], chapter_urls=paged, position=3), "http://x/b/c-4.html")
        # 没有分页格式的章节不做推测，避免抓到下一章
        self.assertIsNone(nd.next_page_url(soup2, "http://x/b/t0yp00p-102.html", chapter_urls=("http://x/b/t0yp00p-102.html",), position=1))
        self.assertIsNone(nd.next_page_url(soup2, "http://x/b/c.html", chapter_urls=("http://x/b/c.html",), position=1))

    def test_page_ordering_keeps_chapter_id_intact(self):
        urls = ("http://x/b/t0yp00p-102-2.html", "http://x/b/t0yp00p-102.html")
        self.assertEqual(
            [u.rsplit("/", 1)[-1] for u in sorted(urls, key=lambda u: nd.chapter_page_number(u, urls))],
            ["t0yp00p-102.html", "t0yp00p-102-2.html"],
        )

    def test_chapter_number_parsing(self):
        self.assertEqual(nd.chapter_number("第十二章 测试"), 12)
        self.assertEqual(nd.chapter_number("第一百零五章 结局"), 105)
        self.assertIsNone(nd.chapter_number("序章"))

    def test_format_and_parse_roundtrip(self):
        ordered = [(1, "第一章 A", "正文一"), (2, "第二章 B", "正文二")]
        text = nd.format_book("小说 1-100章", ordered, 1, 2)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.txt"
            path.write_text(text, encoding="utf-8")
            parsed = nd.parse_existing_txt(path)
        self.assertEqual(set(parsed), {"第一章A", "第二章B"})
        self.assertIn("正文一", parsed["第一章A"])


class EndToEndTests(MockSiteTestCase):
    def test_full_download_first_100_chapters(self):
        out = self.workdir / "book.txt"
        code, log = self.run_cli("--start", "1", "--end", "100", "--output", str(out))
        self.assertEqual(code, 0, log)

        text = out.read_text(encoding="utf-8")
        # 第 1-100 章齐全且顺序正确
        for n in range(1, 101):
            self.assertIn(mock_server.chapter_title(n), text, f"缺少第 {n} 章")
        positions = [text.index(mock_server.chapter_title(n)) for n in range(1, 101)]
        self.assertEqual(positions, sorted(positions))
        # 第 101 章不在输出中
        self.assertNotIn(mock_server.chapter_title(101), text)

        # 多页章节完整拼接（3 页各 3 段 + 末尾 1 段）
        for p in range(1, 4):
            self.assertIn(mock_server.paragraph(7, p), text)
        self.assertIn(mock_server.paragraph(7, 99), text, "最后一页应该被拼接进来")
        # 第 9 章通过「下一页」链接发现第 2 页
        self.assertIn(mock_server.paragraph(9, 4), text)

        # 广告与无关内容被过滤
        for junk in ("本站首发", "手机版阅读网址", "请记住本书", "章节错误，点此举报",
                     "ad.example.net", "bad.example.com", "ca-pub-1234", "document.write",
                     "Copyright", "示例站", "广告位", "猜你喜欢", "下一页"):
            self.assertNotIn(junk, text, f"广告残留：{junk}")
        self.assertNotIn("var chapterId", text)

        # 正文段落没被 <br> 切断
        self.assertIn(mock_server.paragraph(1, 1), text)

        # 每章标题都出现（100 个）
        self.assertEqual(len(re.findall(r"^第[零一二三四五六七八九十百]+章 测试章节\d+$", text, re.M)), 100)

        # WAF 验证码：一次通过后 Cookie 已保存
        cookies = json.loads((self.workdir / "cookies.json").read_text(encoding="utf-8"))
        self.assertTrue(any(c["name"] == "WAF_PASS" for c in cookies["cookies"]))

    def test_second_run_reuses_cookie(self):
        out = self.workdir / "book.txt"
        code, log = self.run_cli("--start", "1", "--end", "5", "--output", str(out))
        self.assertEqual(code, 0, log)

        # 第二次运行：不应再出现验证页面
        self.state.requests.clear()
        code, log = self.run_cli("--start", "1", "--end", "5", "--output", str(out))
        self.assertEqual(code, 0, log)
        self.assertFalse(any("VERIFY" in p for p in self.state.requests), "Cookie 未被复用")

    def test_wrong_captcha_then_correct(self):
        # 第一次输错；之后输入服务端刷新的新验证码
        state = self.state

        def answer(*_args):
            if not hasattr(answer, "calls"):
                answer.calls = 0
            answer.calls += 1
            return "0000" if answer.calls == 1 else state.captcha_code

        with mock.patch.object(builtins, "input", side_effect=answer):
            code, log = self.run_cli("--start", "1", "--end", "2", "--output", str(self.workdir / "b.txt"))
        self.assertEqual(code, 0, log)
        self.assertIn("验证码不正确", log)

    def test_three_wrong_captcha_fails_cleanly(self):
        with mock.patch.object(builtins, "input", side_effect=lambda *_: "9999"):
            code, log = self.run_cli("--start", "1", "--end", "2", "--output", str(self.workdir / "b.txt"))
        self.assertEqual(code, 3, log)
        self.assertFalse((self.workdir / "b.txt").exists())

    def test_range_beyond_catalog_warns_and_continues(self):
        code, log = self.run_cli("--start", "118", "--end", "121", "--output", str(self.workdir / "c.txt"))
        self.assertEqual(code, 0, log)
        text = (self.workdir / "c.txt").read_text(encoding="utf-8")
        self.assertIn(mock_server.chapter_title(118), text)
        self.assertIn(mock_server.chapter_title(120), text)

    def test_server_error_retried(self):
        self.state.fail_once.add("/book/t0yp00p-96.html")  # 第 1 章
        code, log = self.run_cli("--start", "1", "--end", "3", "--output", str(self.workdir / "d.txt"))
        self.assertEqual(code, 0, log)
        text = (self.workdir / "d.txt").read_text(encoding="utf-8")
        self.assertIn(mock_server.paragraph(1, 1), text)

    def test_permanently_failing_chapter_reported_not_crashing(self):
        self.state.sleep_pages.add("/book/t0yp00p-98.html")  # 第 3 章永远 503
        code, log = self.run_cli("--start", "1", "--end", "5", "--output", str(self.workdir / "e.txt"))
        self.assertEqual(code, 1, log)
        self.assertIn("失败的章节", log)
        text = (self.workdir / "e.txt").read_text(encoding="utf-8")
        self.assertIn(mock_server.chapter_title(1), text)
        self.assertIn(mock_server.chapter_title(5), text)
        self.assertIn("失败的章节", log)

    def test_resume_skips_existing(self):
        out = self.workdir / "r.txt"
        code, log = self.run_cli("--start", "1", "--end", "1", "--output", str(out))
        self.assertEqual(code, 0, log)
        code, log = self.run_cli("--start", "1", "--end", "3", "--output", str(out), "--resume")
        self.assertEqual(code, 0, log)
        self.assertIn("已存在，跳过", log)
        text = out.read_text(encoding="utf-8")
        for n in (1, 2, 3):
            self.assertEqual(len(re.findall("^" + re.escape(mock_server.chapter_title(n)) + "$", text, re.M)), 1, f"第 {n} 章应只出现一次标题")

    def test_dry_run_lists_chapters(self):
        code, log = self.run_cli("--start", "1", "--end", "3", "--dry-run")
        self.assertEqual(code, 0, log)
        for n in (1, 2, 3):
            self.assertIn(mock_server.chapter_title(n), log)
        # 只请求目录页，不请求任何章节正文
        self.assertFalse([p for p in self.state.requests if p.startswith("/book/t0yp00p-")])

    def test_network_failure_returns_error_code(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = nd.main([
                "--base-url", "http://127.0.0.1:9",  # 无服务端口
                "--cookies", str(self.workdir / "x.json"),
                "--delay", "0", "--ignore-robots", "--start", "1", "--end", "2",
                "--output", str(self.workdir / "x.txt"),
            ])
        self.assertEqual(code, 5)
        self.assertFalse((self.workdir / "x.txt").exists())


class RenderTests(unittest.TestCase):
    def test_captcha_ansi_render(self):
        png = mock_server.make_png()
        dl = nd.NovelDownloader(nd.Settings(cookies=str(Path(tempfile.gettempdir()) / "no.json")))
        width, height, pixels = dl._decode_png_rgb(png)
        self.assertEqual((width, height), (120, 40))
        self.assertEqual(len(pixels), 120 * 40)
        out = io.StringIO()
        with redirect_stdout(out):
            dl._render_image(png)
        rendered = out.getvalue()
        self.assertTrue(rendered.strip(), "验证码应被渲染成字符画")
        self.assertLessEqual(max(len(line) for line in rendered.splitlines()), 121)
        self.assertGreaterEqual(len(rendered.splitlines()), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
