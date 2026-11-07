# -*- coding: utf-8 -*-
"""v0.3 新增能力测试：任意小说站（v0.31）与并发提速（v0.32）。

运行：
    ./venv/bin/python -m unittest tests.test_v03 -v
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import concurrency  # noqa: E402
import mock_server  # noqa: E402
import novel_download as nd  # noqa: E402
import site_adapter  # noqa: E402
from mock_server import start_in_thread  # noqa: E402


class TocParsingTests(unittest.TestCase):
    """v0.31：目录解析不依赖具体站点模板。"""

    def test_parses_dl_template(self):
        html = """
        <div id="list"><dl>
          <dt>最新章节</dt><dd><a href="/b/3.html">第三章 甲</a></dd>
          <dd><a href="/b/1.html">第一章 乙</a></dd>
          <dd><a href="/b/2.html">第二章 丙</a></dd>
        </dl></div>"""
        toc = site_adapter.parse_toc_page(BeautifulSoup(html, "html.parser"), "http://x/book/1.html")
        self.assertEqual([e.title for e in toc.entries], ["第三章 甲", "第一章 乙", "第二章 丙"])
        self.assertEqual({e.url.split("/")[-1] for e in toc.entries}, {"1.html", "2.html", "3.html"})

    def test_parses_ul_template_with_title_attr(self):
        html = """
        <div class="volume"><ul class="chapter">
          <li><a href="c/1.html" title="第一章 完整标题">第一章 简短</a></li>
          <li><a href="c/2.html" title="第二章 完整标题">第二章 简短</a></li>
          <li><a href="c/3.html" title="第三章 完整标题">第三章 简短</a></li>
        </ul></div>"""
        toc = site_adapter.parse_toc_page(BeautifulSoup(html, "html.parser"), "http://x/book/9.html")
        self.assertEqual(toc.entries[0].title, "第一章 完整标题")
        self.assertEqual(site_adapter._urljoin("http://x/book/9.html", ""), "http://x/book/9.html")

    def test_falls_back_to_whole_page_without_container(self):
        links = "".join(f'<a href="/{i}.html">第{i}章 测试</a> ' for i in range(1, 11))
        html = f"<body><div class='random'>{links}</div></body>"
        toc = site_adapter.parse_toc_page(BeautifulSoup(html, "html.parser"), "http://x/")
        self.assertEqual(len(toc.entries), 10)

    def test_ignores_nav_links_and_external_hosts(self):
        html = """
        <div id="list"><a href="/">首页</a><a href="/rank">排行榜</a>
        <a href="http://other.com/1.html">第一章 外链</a>
        <a href="/1.html">第一章 本站</a><a href="/2.html">第二章 本站</a>
        <a href="/3.html">第三章 本站</a></div>"""
        toc = site_adapter.parse_toc_page(BeautifulSoup(html, "html.parser"), "http://x/book.html")
        urls = {e.url for e in toc.entries}
        self.assertEqual(len(toc.entries), 3)
        self.assertTrue(all("other.com" not in u for u in urls))

    def test_detects_next_toc_page(self):
        html = '<div id="list"><a href="/c/1.html">第一章</a><a href="/c/2.html">第二章</a><a href="/c/3.html">第三章</a></div><a rel="next" href="/book/2.html">下一页</a>'
        toc = site_adapter.parse_toc_page(BeautifulSoup(html, "html.parser"), "http://x/book/1.html")
        self.assertEqual(toc.next_page, "http://x/book/2.html")


class ContentExtractionTests(unittest.TestCase):
    """v0.31：正文提取不依赖具体选择器，并过滤广告。"""

    def test_extracts_by_heuristic_when_no_known_selector(self):
        body = "　　" + "青云宗山门前的积雪尚未化尽，少年提剑而立。" * 4
        html = f"""
        <body>
          <div class="nav"><a href="/">首页</a><a href="/a">目录</a><a href="/b">排行</a></div>
          <div class="xyz-content-unknown"><p>{body}</p><p>{body}</p></div>
          <div class="footer"><a href="/">返回</a></div>
        </body>"""
        text = site_adapter.extract_content(BeautifulSoup(html, "html.parser"))
        self.assertIn("少年提剑而立", text)
        self.assertNotIn("首页", text)

    def test_link_dense_container_rejected(self):
        links = "".join(f'<a href="/{i}.html">第{i}章 标题</a> ' for i in range(1, 30))
        html = f"<body><div class='list'>{links}</div><div class='content'><p>{'真正的正文内容，' * 30}</p></div></body>"
        text = site_adapter.extract_content(BeautifulSoup(html, "html.parser"))
        self.assertIn("真正的正文内容", text)
        self.assertNotIn("章 标题", text)

    def test_strips_ads_inside_content(self):
        html = """
        <div id="content">
          <p>正文第一段，内容真实。</p>
          <script>var x = 1;</script>
          <ins class="adsbygoogle">广告位</ins>
          <p>请记住本书首发域名：www.example.com</p>
          <p>正文第二段，内容也真实。</p>
        </div>"""
        text = site_adapter.extract_content(BeautifulSoup(html, "html.parser"))
        self.assertIn("正文第一段", text)
        self.assertIn("正文第二段", text)
        for junk in ("var x", "广告位", "www.example.com", "请记住本书"):
            self.assertNotIn(junk, text)


class AltSiteEndToEndTests(unittest.TestCase):
    """v0.31：用一个结构完全不同的站点做端到端验证（含 --url 参数）。"""

    def setUp(self) -> None:
        self.server, self.state, self.base = start_in_thread()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)

    def run_cli(self, *args: str) -> tuple[int, str]:
        argv = ["--cookies", str(self.workdir / "c.json"), "--delay", "0.02", "--ignore-robots", *args]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = nd.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_arbitrary_site_via_url(self):
        out = self.workdir / "alt.txt"
        code, log = self.run_cli(
            "--url", f"{self.base}/alt/book/{mock_server.ALT_BOOK}/",
            "--start", "1", "--end", str(mock_server.ALT_TOTAL), "--output", str(out),
        )
        self.assertEqual(code, 0, log)
        text = out.read_text(encoding="utf-8")
        for n in range(1, mock_server.ALT_TOTAL + 1):
            self.assertIn(mock_server.alt_chapter_title(n), text)
        positions = [text.index(mock_server.alt_chapter_title(n)) for n in range(1, mock_server.ALT_TOTAL + 1)]
        self.assertEqual(positions, sorted(positions))
        for junk in ("another-site", "Copyright", "最新网址", "原创发布"):
            self.assertNotIn(junk, text)

    def test_dry_run_on_arbitrary_site(self):
        code, log = self.run_cli(
            "--url", f"{self.base}/alt/book/{mock_server.ALT_BOOK}/", "--dry-run", "--end", "3",
        )
        self.assertEqual(code, 0, log)
        self.assertIn(mock_server.alt_chapter_title(1), log)
        self.assertIn(mock_server.alt_chapter_title(3), log)
        self.assertNotIn(mock_server.alt_chapter_title(4), log)

    def test_resume_on_arbitrary_site(self):
        out = self.workdir / "alt2.txt"
        url = f"{self.base}/alt/book/{mock_server.ALT_BOOK}/"
        code, _ = self.run_cli("--url", url, "--start", "1", "--end", "2", "--output", str(out))
        self.assertEqual(code, 0)
        code, log = self.run_cli("--url", url, "--start", "1", "--end", "4", "--output", str(out), "--resume")
        self.assertEqual(code, 0, log)
        text = out.read_text(encoding="utf-8")
        for n in range(1, 5):
            self.assertEqual(len(re.findall("^" + re.escape(mock_server.alt_chapter_title(n)) + "$", text, re.M)), 1)


class ConcurrencyTests(unittest.TestCase):
    """v0.32：并发下载与自适应限速。"""

    def setUp(self) -> None:
        self.server, self.state, self.base = start_in_thread()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)

    def _download(self, workers: int, chapters: int = 12) -> float:
        out = self.workdir / f"w{workers}.txt"
        argv = [
            "--url", f"{self.base}/alt/book/{mock_server.ALT_BOOK}/",
            "--cookies", str(self.workdir / f"c{workers}.json"),
            "--delay", "0.05", "--ignore-robots",
            "--start", "1", "--end", str(chapters), "--output", str(out),
            "--workers", str(workers),
        ]
        start = time.monotonic()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = nd.main(argv)
        elapsed = time.monotonic() - start
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn(mock_server.alt_chapter_title(chapters), text)
        return elapsed

    def test_parallel_is_faster_than_serial(self):
        serial = self._download(1)
        parallel = self._download(6)
        self.assertLess(parallel, serial * 0.8, f"并发未提速：串行 {serial:.2f}s，并发 {parallel:.2f}s")

    def test_workers_clamped_to_range(self):
        settings = nd.build_settings(nd.build_parser().parse_args(["--workers", "999"]))
        self.assertEqual(settings.workers, nd.MAX_WORKERS)
        settings = nd.build_settings(nd.build_parser().parse_args(["--workers", "0"]))
        self.assertEqual(settings.workers, 1)

    def test_run_ordered_isolates_errors(self):
        def worker(i, item):
            if item == "bad":
                raise ValueError("boom")
            if item == "none":
                return None
            return item.upper()

        items = list(enumerate(["a", "bad", "b", "none", "c"]))
        result = concurrency.run_ordered(items, worker, max_workers=3)
        self.assertEqual(result.values[0], "A")
        self.assertEqual(result.values[2], "B")
        self.assertIn(3, result.values)  # None 也算“完成”
        self.assertIn(1, result.errors)

    def test_run_ordered_serial_mode(self):
        result = concurrency.run_ordered([(i, i) for i in range(5)], lambda i, x: x * 2, max_workers=1)
        self.assertEqual(result.values, {i: i * 2 for i in range(5)})

    def test_rate_limiter_backoff_and_recovery(self):
        clock = [0.0]
        limiter = concurrency.RateLimiter(
            delay=0.2, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)
        )
        limiter.report_throttled()
        self.assertGreaterEqual(limiter.delay, 0.4)
        before = limiter.delay
        for _ in range(60):
            limiter.report_success()
        self.assertLess(limiter.delay, before, "连续成功后应逐步恢复速度")

    def test_429_triggers_backoff_but_recovers(self):
        """服务器返回 429 时应降速重试，并最终成功。"""
        self.state.fail_once.add(f"/alt/read/{mock_server.ALT_BOOK}/3.html")
        out = self.workdir / "throttle.txt"
        argv = [
            "--url", f"{self.base}/alt/book/{mock_server.ALT_BOOK}/",
            "--cookies", str(self.workdir / "t.json"), "--delay", "0.02",
            "--ignore-robots", "--start", "1", "--end", "4", "--output", str(out),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = nd.main(argv)
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn(mock_server.alt_chapter_title(3), text)


class ChapterSelectionTests(unittest.TestCase):
    """按「章号」选章：目录缺章时不能把后面的章节顶上来。"""

    def make_downloader(self, titles: list[str]) -> nd.NovelDownloader:
        settings = nd.Settings(start=1, end=6, cookies="/nonexistent.json", delay=0)
        settings.chapters = [nd.Chapter(t, (f"http://x/{i}.html",), i + 1) for i, t in enumerate(titles)]
        return nd.NovelDownloader(settings)

    def test_by_number_skips_missing_chapter(self):
        titles = ["第一章 a", "第二章 b", "第四章 d", "第五章 e", "第九章 j"]
        dl = self.make_downloader(titles)
        picked = dict(dl.iter_positions(len(titles)))
        self.assertEqual(picked[1].title, "第一章 a")
        self.assertIsNone(picked[3])          # 第 3 章缺失，不能变成第四章
        self.assertEqual(picked[4].title, "第四章 d")
        self.assertIsNone(picked[6])          # 第 6 章缺失

    def test_by_order_mode(self):
        titles = ["第一章 a", "第二章 b", "第四章 d"]
        settings = nd.Settings(start=1, end=3, cookies="/nonexistent.json", delay=0, by_order=True)
        settings.chapters = [nd.Chapter(t, (f"http://x/{i}.html",), i + 1) for i, t in enumerate(titles)]
        picked = dict(nd.NovelDownloader(settings).iter_positions(3))
        self.assertEqual(picked[3].title, "第四章 d")  # 按目录第 3 条取

    def test_titles_without_numbers_fall_back_to_order(self):
        titles = ["楔子", "序章", "正文一"]
        dl = self.make_downloader(titles)
        picked = dict(dl.iter_positions(3))
        self.assertEqual(picked[1].title, "楔子")
        self.assertEqual(picked[3].title, "正文一")


class CacheTests(unittest.TestCase):
    """v0.32：页面缓存。"""

    def setUp(self) -> None:
        self.server, self.state, self.base = start_in_thread()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)
        self.cache_dir = self.workdir / "cache"

    def _run(self, output: str) -> tuple[int, int]:
        self.state.requests.clear()
        argv = [
            "--url", f"{self.base}/alt/book/{mock_server.ALT_BOOK}/",
            "--cookies", str(self.workdir / "c.json"), "--delay", "0.01",
            "--ignore-robots", "--start", "1", "--end", "5",
            "--output", str(self.workdir / output), "--cache-dir", str(self.cache_dir),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = nd.main(argv)
        return code, len(self.state.requests)

    def test_second_run_hits_cache(self):
        code, first_requests = self._run("a.txt")
        self.assertEqual(code, 0)
        self.assertGreater(first_requests, 0)
        code, second_requests = self._run("b.txt")
        self.assertEqual(code, 0)
        self.assertEqual(second_requests, 0, "第二次运行应完全命中缓存")
        self.assertTrue((self.cache_dir).is_dir())

    def test_cache_disabled_by_default(self):
        cache = nd.PageCache(None)
        self.assertFalse(cache.enabled)
        self.assertIsNone(cache.get("http://x/1"))
        cache.put("http://x/1", "<html/>")  # 不应报错


if __name__ == "__main__":
    unittest.main(verbosity=2)
