# -*- coding: utf-8 -*-
"""分章区间（下拉菜单 + 加密接口）功能测试。

覆盖：下拉菜单识别、AES 加密与站点 JS 一致、接口抓取、跨区间选章、
站点配置自动发现、失效降级。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chapter_api  # noqa: E402
import mock_server  # noqa: E402
import novel_download as nd  # noqa: E402
from mock_server import start_in_thread  # noqa: E402


class MenuDetectionTests(unittest.TestCase):
    """从目录页识别分章下拉菜单。"""

    def test_detects_real_markup(self):
        html = """
        <div class="selBox">
          <div class="btn"><span class="txt">1-100章</span></div>
          <div class="dropDown" data-aid="t0yp00p"><ul>
            <li data-p="1" class="cur">1-100章</li>
            <li data-p="2">101-200章</li>
            <li data-p="3">201-300章</li>
          </ul></div>
        </div>"""
        found = chapter_api.find_range_menu(BeautifulSoup(html, "html.parser"))
        self.assertIsNotNone(found)
        book_id, options = found
        self.assertEqual(book_id, "t0yp00p")
        self.assertEqual([o.page for o in options], [1, 2, 3])
        self.assertEqual(options[1].label, "101-200章")

    def test_book_id_from_links_when_missing(self):
        html = """
        <a href="/book/abc123_1.html">第1章</a>
        <div class="dropDown"><ul><li data-p="1">1-100</li><li data-p="2">101-200</li></ul></div>"""
        found = chapter_api.find_range_menu(BeautifulSoup(html, "html.parser"))
        self.assertIsNotNone(found)

    def test_no_menu_returns_none(self):
        html = '<div class="listmain"><a href="/1.html">第一章</a></div>'
        self.assertIsNone(chapter_api.find_range_menu(BeautifulSoup(html, "html.parser")))

    def test_single_option_is_not_a_menu(self):
        html = '<div class="dropDown"><ul><li data-p="1">1-100章</li></ul></div>'
        self.assertIsNone(chapter_api.find_range_menu(BeautifulSoup(html, "html.parser")))

    def test_plain_nav_menu_not_mistaken(self):
        """普通导航下拉（无页码属性）不应被当成分章菜单。"""
        html = '<div class="dropDown"><ul><li>首页</li><li>排行</li><li>书架</li></ul></div>'
        self.assertIsNone(chapter_api.find_range_menu(BeautifulSoup(html, "html.parser")))


class EncryptionTests(unittest.TestCase):
    """AES 参数与站点实现一致。"""

    def test_matches_browser_output(self):
        """与在真实站点页面里执行 chapterPageAjxa 得到的结果逐字节一致。"""
        api = chapter_api.api_for_host("www.shuhaoxs.com")
        self.assertIsNotNone(api)
        token = chapter_api.encrypt_payload(api, "t0yp00p", 3)
        self.assertEqual(token, "v95hOEdJeR834PDYcie8rrVTBorsiUA4jhbT+6zpW9I=")

    def test_roundtrip(self):
        api = chapter_api.api_for_host("shuhaoxs.com")
        token = chapter_api.encrypt_payload(api, "t0yp00p", 7)
        self.assertEqual(chapter_api.decrypt_payload(api, token), {"id": "t0yp00p", "page": 7})

    def test_host_matching(self):
        for host in ("shuhaoxs.com", "www.shuhaoxs.com", "m.shuhaoxs.com"):
            self.assertIsNotNone(chapter_api.api_for_host(host), host)
        self.assertIsNone(chapter_api.api_for_host("example.com"))

    def test_parse_config_from_unobfuscated_js(self):
        js = """
        this.iv = CryptoJS.enc.Utf8.parse('0123456789abcdef');
        const crypt = new AESCrypt('abcdefghijklmnopqrstuvwxyz012345');
        $.ajax('/index.php?action=loadChapterPage', {data: crypt});
        """
        api = chapter_api.parse_api_from_js(js)
        self.assertEqual(api.endpoint, "/index.php?action=loadChapterPage")
        self.assertEqual(api.key, b"abcdefghijklmnopqrstuvwxyz012345")
        self.assertEqual(api.iv, b"0123456789abcdef")

    def test_parse_returns_none_for_missing_config(self):
        self.assertIsNone(chapter_api.parse_api_from_js("var x = 1;"))


class RangeApiTests(unittest.TestCase):
    """通过模拟站点的加密接口抓取跨区间目录。"""

    def setUp(self) -> None:
        self.server, self.state, self.base = start_in_thread()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)
        env = {
            "NOVEL_RANGE_API": json.dumps(
                {
                    "endpoint": "/range/index.php?action=loadChapterPage",
                    "key": mock_server.RANGE_KEY.decode(),
                    "iv": mock_server.RANGE_IV.decode(),
                }
            )
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args: str) -> tuple[int, str]:
        argv = ["--url", f"{self.base}/range/book/{mock_server.RANGE_BOOK_ID}.html",
                "--cookies", str(self.workdir / "c.json"), "--delay", "0.01", "--ignore-robots", *args]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = nd.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_downloads_beyond_default_range(self):
        """默认页只有 1-100 章，接口应补齐 101 章之后的内容。"""
        out = self.workdir / "book.txt"
        code, log = self.run_cli("--start", "95", "--end", "110", "--output", str(out))
        self.assertEqual(code, 0, log)
        text = out.read_text(encoding="utf-8")
        for n in range(95, 111):
            self.assertIn(mock_server.range_chapter_title(n), text, f"缺少第 {n} 章")
        self.assertIn("检测到分章区间菜单", log)

    def test_last_range_reachable(self):
        out = self.workdir / "last.txt"
        code, log = self.run_cli("--start", "255", "--end", "260", "--output", str(out))
        self.assertEqual(code, 0, log)
        text = out.read_text(encoding="utf-8")
        for n in range(255, 261):
            self.assertIn(mock_server.range_chapter_title(n), text)

    def test_without_api_config_warns_and_falls_back(self):
        """识别不到接口配置时给出提示，只下载默认区间，不崩溃。"""
        with mock.patch.dict(os.environ, {"NOVEL_RANGE_API": ""}), \
                self.assertLogs(level="WARNING") as captured:
            code, log = self.run_cli("--dry-run", "--start", "101", "--end", "102")
        self.assertEqual(code, 0, log)
        self.assertTrue(
            any("未识别到该站点的分章接口" in line for line in captured.output),
            "应提示无法识别分章接口",
        )

    def test_no_range_api_flag_skips_api(self):
        code, log = self.run_cli("--no-range-api", "--dry-run", "--start", "1", "--end", "2")
        self.assertEqual(code, 0, log)
        self.assertNotIn("检测到分章区间菜单", log)


class CaptchaDetectionRegressionTests(unittest.TestCase):
    """回归：JS 文件里同时出现「验证码」「请输入」不能被误判为验证页。"""

    def test_js_with_captcha_words_is_not_captcha_page(self):
        js = "if(验证码){alert('请输入验证码')}"
        soup = BeautifulSoup(js, "html.parser")
        self.assertFalse(nd.is_captcha_page(soup, "https://x/templates/js/public.js"))

    def test_real_captcha_page_detected(self):
        html = """<html><head><title>身份验证</title></head><body>
        <form id="captcha-form"><p>请输入上面的验证码</p>
        <input type="text" name="GOEDGE_WAF_CAPTCHA_CODE"/>
        <img id="ui-captcha-image" src="/WAF/VERIFY/CAPTCHA?x=1"/></form></body></html>"""
        self.assertTrue(nd.is_captcha_page(BeautifulSoup(html, "html.parser"), "https://x/WAF/VERIFY/CAPTCHA?info=1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
