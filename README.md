# Novel Downloader / 小说自动下载器

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

A Python script that downloads serialized novels from an online catalog
(e.g. `https://www.shuhaoxs.com/book/t0yp00p.html`), stitches multi-page
chapters automatically, strips advertisements and unrelated links, and merges
the chosen chapters into a single `.txt` file.

### Features
- **Chapter range**: downloads chapters 1–100 by default (`--start` / `--end` / `--all`).
- **Multi-page stitching**: follows the page's "下一页" link, page links listed in
  the catalog (`xxx-2.html`), and — only for paged chapters — the next `-N` page,
  verifying the page title so it never strays into the following chapter.
- **Content cleaning**: keeps only the article text; drops `<script>`, `<ins>` ads,
  inline ad images, navigation bars, promo lines, URLs and HTML comments.
- **WAF CAPTCHA**: the site is protected by GoEdge WAF. On the first run the script
  shows the CAPTCHA in your terminal (character art) and opens the image with your
  default viewer; the verification cookie is stored in `cookies.json` and reused.
- **Resilient**: retries on connection errors/timeouts/5xx, retries failed chapters,
  supports `--resume`, and writes the output atomically (no half-written files).

### Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Usage
```bash
python novel_download.py                     # chapters 1-100 -> 小说_1-100章.txt
python novel_download.py --end 200           # chapters 1-200
python novel_download.py --start 50 --end 120 -o out.txt
python novel_download.py --all               # every chapter in the catalog
python novel_download.py --dry-run           # list parsed chapters only
python novel_download.py --resume            # skip chapters already in the output
```

Exit codes: `0` success, `1` some chapters failed, `2` bad arguments,
`3` CAPTCHA failed / not interactive, `4` robots.txt disallowed, `5` network error.

### Tests
Offline tests run against a local mock site that reproduces the WAF CAPTCHA and
the catalog/chapter structure:
```bash
python -m unittest discover -s tests -v
```

---

<a name="中文"></a>
## 中文

从小说目录页抓取章节，自动拼接多页章节、过滤广告与无关链接，
把指定章节合并成一个 TXT 文件。

### 功能
- **章节区间**：默认抓取第 1–100 章，可用 `--start` / `--end` / `--all` 调整。
- **多页拼接**：依次使用页内「下一页」链接、目录中列出的分页（`xxx-2.html`），
  以及（仅对分页格式的章节）`-N` 下一页探测，并用页面标题校验，不会串到下一章。
- **正文净化**：只保留正文，自动去掉 `<script>`、`<ins>` 广告、内联广告图、
  导航条（上一章/下一章/目录）、推广语、网址和 HTML 注释。
- **WAF 人机验证**：目标站由 GoEdge WAF 保护。首次运行会在终端显示验证码字符画
  （同时用系统图片查看器打开图片），输入一次后验证 Cookie 保存在 `cookies.json`，
  后续自动复用。
- **健壮性**：连接失败/超时/5xx 自动重试，失败章节重试一轮，支持 `--resume` 断点续传，
  输出文件原子写入（中断不会留下半截文件）。

### 安装
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 使用
```bash
python novel_download.py                     # 第 1-100 章 -> 小说_1-100章.txt
python novel_download.py --end 200           # 第 1-200 章
python novel_download.py --start 50 --end 120 -o out.txt
python novel_download.py --all               # 目录中的全部章节
python novel_download.py --dry-run           # 只解析目录并打印章节列表
python novel_download.py --resume            # 跳过输出文件中已有的章节
```

退出码：`0` 成功，`1` 部分章节失败，`2` 参数错误，`3` 验证码失败/非交互终端，
`4` robots.txt 不允许，`5` 网络错误。

### 关于 V0.1 不能运行的原因
1. 目标站有 GoEdge WAF 图形验证码，V0.1 直接 `requests.get` 只能拿到验证页（307 跳转），
   没有任何验证流程。
2. 依赖未安装（`bs4` 缺失）。
3. 正文选择器 `.content, #content` 与目录选择器写死，站点结构变化就抓不到内容。
4. 分页只靠 `-2.html` 猜测，遇到目录只列第一页或章节 ID 本身带数字时会漏页/串章。
5. 异常全部吞掉（`except: break`），失败时静默产出空内容或半截文件。

### 测试
离线测试运行在本地模拟站点上（还原 WAF 验证码与目录/章节结构）：
```bash
python -m unittest discover -s tests -v
```

### License

This project is open-source under the MIT License.
