# Novel Downloader / 小说自动下载器

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

A Python script that downloads serialized novels from online catalogs, stitches
multi-page chapters automatically, strips advertisements and unrelated links,
and merges the chosen chapters into a single `.txt` file.

**v0.2** targets one site. **v0.3** works on *any* novel site and is much faster.

### Features
- **Chapters beyond the default range**: many sites' catalog pages list only the
  first 100 chapters and load the rest through a "1-100 / 101-200 / ..." dropdown
  that calls an encrypted AJAX endpoint. The script detects that dropdown, reads the
  site's AES key/IV and endpoint, and fetches **every** range automatically — so
  `--start 101 --end 200` works even though those chapters are not on the page.
- **Any novel site (v0.31)**: pass any catalog URL with `--url`. The catalog and
  article body are located with multi-template candidates plus structural
  heuristics (text density, link density, paragraph count) — no per-site selectors
  to maintain. Multi-page catalogs are supported (`--all-pages`).
- **Parallel download (v0.32)**: chapters download on a thread pool (`--workers`,
  default 6). Measured on the local mock site, 100 chapters take **43 s at
  `--workers 1` vs 6 s at `--workers 8`**.
- **Adaptive throttling (v0.32)**: on HTTP 429/503 the request interval doubles
  automatically and recovers after sustained success. Throttling is per-thread, so
  concurrency still pays off while staying polite.
- **Page cache (v0.32)**: `--cache-dir` stores fetched pages; re-runs and `--resume`
  skip network requests entirely (404s are cached too, so "next page" probing costs
  one request).
- **Multi-page stitching**: follows the page's real "下一页" link and catalog-listed
  pages (`xxx-2.html`). It never guesses `-N` from the URL: verified against the live
  site, nonexistent pages return leftover content (not 404), which would silently
  corrupt the text.
- **Content cleaning**: keeps only article text; drops `<script>`, `<ins>` ads,
  inline ad images, navigation bars, promo lines, URLs and HTML comments.
- **WAF CAPTCHA** (if the site uses GoEdge WAF): the first run shows the CAPTCHA in
  your terminal (character art) and opens the image; the cookie is reused afterwards.
- **Resilient**: retries connection errors/timeouts/5xx, retries failed chapters,
  supports `--resume`, writes the output atomically (no half-written files).

### Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Usage
```bash
python novel_download.py                                      # built-in site, chapters 1-100
python novel_download.py --url https://example.com/book/1.html  # any novel site
python novel_download.py --url ... --start 1 --end 50           # a chapter range
python novel_download.py --url ... --all                        # every chapter
python novel_download.py --url ... --all-pages                  # multi-page catalog too
python novel_download.py --url ... --workers 8 --cache-dir .novel_cache
python novel_download.py --url ... --dry-run                    # list parsed chapters only
python novel_download.py --url ... --resume                     # skip chapters already saved
```

| Option | Meaning |
| --- | --- |
| `--url` | catalog page of any novel site (default: built-in site) |
| `--start` / `--end` / `--all` | chapter range (default 1–100) / all chapters |
| `--all-pages` | follow catalog "下一页" pages |
| `--no-range-api` | do not fetch chapters behind a range dropdown |
| `--by-order` | select by catalog position instead of chapter number |
| `--workers N` | parallel threads, 1–16 (default 6) |
| `--delay S` | initial per-thread request interval (default 0.8 s, adapts) |
| `--cache-dir DIR` | page cache for fast re-runs |
| `--resume` | skip chapters already present in the output file |
| `--dry-run` | parse and list chapters only |

Exit codes: `0` success, `1` some chapters failed, `2` bad arguments,
`3` CAPTCHA failed / not interactive, `4` robots.txt disallowed, `5` network error.

### Tests
Offline tests run against local mock sites (two different templates, plus a WAF CAPTCHA):
```bash
python -m unittest discover -s tests -v
```

---

<a name="中文"></a>
## 中文

从小说目录页抓取章节，自动拼接多页章节、过滤广告与无关链接，
把指定章节合并成一个 TXT 文件。

**v0.2** 只能抓一个固定站点；**v0.3** 支持任意小说站，并大幅提速。

### 功能
- **下载默认区间之外的章节**：很多小说站的目录页只列出前 100 章，其余章节由
  「1-100 / 101-200 / ...」下拉菜单通过加密的 AJAX 接口异步加载。脚本会识别这个下拉菜单，
  读取站点的 AES 密钥/IV 与接口地址，自动抓取**全部区间**——因此即使目录页上没有，
  `--start 101 --end 200` 也能正常下载。
- **任意小说站（v0.31）**：用 `--url` 传入任意小说站的目录页网址。目录与正文不再依赖
  写死的选择器，而是用「多模板候选 + 结构启发式」（文字密度、链接密度、段落数）自动识别。
  支持分页目录（`--all-pages`）。
- **并发下载（v0.32）**：章节用线程池并发抓取（`--workers`，默认 6）。本地模拟站实测
  100 章 **`--workers 1` 约 43 秒，`--workers 8` 约 6 秒**。
- **自适应限速（v0.32）**：遇到 HTTP 429/503 自动把请求间隔翻倍，持续成功后逐步恢复。
  节流按线程独立计算，保证并发仍然有效，同时保持礼貌访问。
- **页面缓存（v0.32）**：`--cache-dir` 把已抓页面存盘，重跑与 `--resume` 时完全跳过网络请求
  （404 也会缓存，因此「下一页」探测只消耗一次请求）。
- **多页拼接**：只跟随页面真实的「下一页」链接与目录列出的分页（`xxx-2.html`）。
  不再按 URL 猜 `-N`：实测该站对不存在的分页返回的不是 404，而是别的内容，
  猜出来会把无关内容拼进正文。
- **正文净化**：只保留正文，自动去掉 `<script>`、`<ins>` 广告、内联广告图、
  导航条（上一章/下一章/目录）、推广语、网址和 HTML 注释。
- **WAF 人机验证**（若站点有 GoEdge WAF）：首次运行在终端显示验证码字符画
  （同时用系统图片查看器打开图片），输入一次后 Cookie 复用。
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
python novel_download.py                                      # 内置站点，第 1-100 章
python novel_download.py --url https://example.com/book/1.html  # 任意小说站
python novel_download.py --url ... --start 1 --end 50           # 指定章节区间
python novel_download.py --url ... --all                        # 全部章节
python novel_download.py --url ... --all-pages                  # 连目录分页一起抓
python novel_download.py --url ... --workers 8 --cache-dir .novel_cache
python novel_download.py --url ... --dry-run                    # 只解析目录
python novel_download.py --url ... --resume                     # 跳过已保存章节
```

| 参数 | 说明 |
| --- | --- |
| `--url` | 任意小说站的目录页（默认用内置站点） |
| `--start` / `--end` / `--all` | 章节区间（默认 1–100）/ 全部章节 |
| `--all-pages` | 跟随目录页「下一页」 |
| `--no-range-api` | 不抓取分章下拉菜单后的章节 |
| `--by-order` | 按目录顺序取章（默认按章号） |
| `--workers N` | 并发线程数，1–16（默认 6） |
| `--delay S` | 每线程初始请求间隔（默认 0.8 秒，自动调整） |
| `--cache-dir DIR` | 页面缓存目录，大幅加快重跑 |
| `--resume` | 跳过输出文件中已有的章节 |
| `--dry-run` | 只解析并列出章节 |

退出码：`0` 成功，`1` 部分章节失败，`2` 参数错误，`3` 验证码失败/非交互终端，
`4` robots.txt 不允许，`5` 网络错误。

### 关于 V0.1 不能运行的原因
1. 目标站有 GoEdge WAF 图形验证码，V0.1 直接 `requests.get` 只能拿到验证页（307 跳转），
   没有任何验证流程。
2. 依赖未安装（`bs4` 缺失）。
3. 正文选择器 `.content, #content` 与目录选择器写死，站点结构变化就抓不到内容。
4. 分页只靠 `-2.html` 猜测，遇到目录只列第一页或章节 ID 本身带数字时会漏页/串章。
5. 异常全部吞掉（`except: break`），失败时静默产出空内容或半截文件。

### 代码结构
| 文件 | 作用 |
| --- | --- |
| `novel_download.py` | 主流程：HTTP、验证码、目录/章节调度、TXT 输出 |
| `chapter_api.py` | 分章下拉菜单 + 加密章节列表接口 |
| `site_adapter.py` | v0.31：通用目录与正文识别（纯函数，可单独测试） |
| `concurrency.py` | v0.32：线程池执行器 + 自适应限速器 |
| `page_cache.py` | v0.32：页面磁盘缓存 |

### 分章区间接口说明
- 脚本内置了目标站的加密接口配置（密钥/IV 来自站点自身前端 JS 的 `AESCrypt`）。
- 站点更新密钥后若失效，可用环境变量覆盖，无需改代码：
  ```bash
  export NOVEL_RANGE_API='{"endpoint":"/index.php?action=loadChapterPage","key":"<32位密钥>","iv":"<16位IV>"}'
  ```
- 对未混淆的站点 JS，脚本能自动解析出密钥与接口；解析不到时会给出明确提示并只下载默认区间。

### 测试
离线测试运行在本地模拟站点上（两种不同模板 + WAF 验证码 + 限流场景）：
```bash
python -m unittest discover -s tests -v
```

### License

This project is open-source under the MIT License.
