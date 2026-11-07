# -*- coding: utf-8 -*-
"""通用小说站点适配器（v0.31）。

目标：不依赖某个站点的固定选择器，而是用「多模板候选 + 结构启发式」
从任意小说站的目录页和章节页中找出章节列表与正文。

设计要点：
- 目录页有大量章节链接，其中绝大多数标题形如「第X章 / 楔子 / 序章」；
- 正文容器通常是「文字多、链接少、块级子节点多」的元素；
- 广告/导航行有明显的词汇特征（网址、站点名、上一章/下一章等）。

所有函数都是纯函数（不发起网络请求），便于离线测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# 章节标题识别
# ---------------------------------------------------------------------------

# 「第X章 / 第X节 / 第X回 / 第X卷」，数字可以是阿拉伯数字或中文数字
CHAPTER_TITLE_RE = re.compile(
    r"^\s*(?:第[0-9零〇一二两三四五六七八九十百千万]+[章节回卷]|正文|楔子|序章|序言|前言|引子|尾声|番外)"
)

# 目录页常见容器（按优先级）；命中多个时取章节链接最多的
TOC_SELECTORS = (
    "#list",
    ".listmain",
    ".list-main",
    "#chapterlist",
    "#chapter-list",
    ".chapter-list",
    ".chapterlist",
    ".book-chapter",
    ".book_list",
    ".booklist",
    ".volume-wrap",
    ".volume-list",
    ".directory",
    ".catalog",
    "#catalog",
    ".wrapper_list",
    ".mulu",
    "dl",
    "ul.chapter",
    ".main_text",
)

# 正文容器常见选择器（按优先级）
CONTENT_SELECTORS = (
    "#content",
    "#chaptercontent",
    "#chapter-content",
    "#booktxt",
    "#txtContent",
    "#txtcontent",
    "#read-content",
    "#articlecontent",
    "#htmlContent",
    "#booktext",
    "#nr1",
    "#nr",
    ".content",
    ".showtxt",
    ".read-content",
    ".reader-content",
    ".book-content",
    ".novel-content",
    ".article-content",
    ".article-body",
    ".entry-content",
    ".post-content",
    ".item-content",
    ".txtnav",
    ".text",
    ".yd_text2",
    ".read-content > div",
    "article",
)

# 从正文中整体删除的元素（广告、脚本、导航等）
DROP_TAGS = (
    "script", "style", "ins", "iframe", "noscript", "svg", "canvas",
    "form", "button", "input", "select", "textarea", "nav", "aside",
    "audio", "video", "object", "embed", "link", "meta", "template",
)
DROP_SELECTORS = (
    ".read-inline-img", ".ad", ".ads", ".advert", ".advertisement",
    ".adsbygoogle", ".gg", ".gg200x300", ".bdshare", ".share", ".bottem",
    ".page_chapter", ".report", ".readfoot", ".foot", ".footer",
    ".breadcrumb", ".crumbs", ".hidden", "[style*='display:none']",
    "[style*='display: none']", ".tips", ".recommend", ".related",
)

# 分页链接文字
NEXT_PAGE_WORDS = frozenset(("下一页", "下一頁", "下页", "下頁", "后一页", "后頁"))

# 各章节页之间，正文页脚可能出现这些行，一律过滤
AD_KEYWORDS = (
    "本站首发", "首发于", "最快更新", "请记住本书", "请收藏本站", "求收藏",
    "求推荐票", "求月票", "求推荐", "手机版阅读网址", "手机阅读", "手机用户请",
    "章节错误", "点此举报", "举报后删除", "温馨提示", "手机浏览器", "电脑版",
    "加入书签", "推荐本书", "投推荐票", "笔趣", "书海阁", "八一中文",
    "新笔趣", "顶点小说", "本站网址", "永久免费", "无弹窗", "全文阅读",
    "本站域名", "最新章节", "书友群", "公众号", "二维码", "扫码",
)
AD_DOMAINS = (
    ".com", ".net", ".org", ".cc", ".cn", ".vip", ".la", ".tv", ".info",
    ".xyz", ".top", ".pw", ".me", ".io", ".us", ".co", "http://", "https://",
    "www.", "wap.", "m.",
)
NAV_WORDS = frozenset(
    "上一章 下一章 上一页 下一页 返回目录 目录 加入书签 章节目录 推荐本书".split()
)
URL_LINE_RE = re.compile(
    r"^(?:https?://|www\.|wap\.|m\.)\S*$|^[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,6}(?:/\S*)?$",
    re.IGNORECASE,
)
NAV_LINE_RE = re.compile(
    r"^[\s\-—_=*·•|/\\<>《》\[\]【】()（）,，.。:：]+$"
    r"|^(?:上一章|下一章|上一页|下一页|目录|返回目录|加入书签|章节目录|推荐本书|返回书页|返回列表)$",
    re.IGNORECASE,
)
NAV_SPLIT_RE = re.compile(r"[\s|/·•<>《》【】\[\]()（）\-—_]+")
AD_MAX_LINE_LEN = 80

BLOCK_TAGS = frozenset(
    "p div br li dd dt h1 h2 h3 h4 h5 h6 tr table section article blockquote pre".split()
)


# ---------------------------------------------------------------------------
# 基础文本工具
# ---------------------------------------------------------------------------


def normalize_text(raw: str) -> str:
    """统一换行、全角空格与行尾空白。"""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("\u3000", " ")
    return "\n".join(line.rstrip() for line in text.split("\n"))


def block_text(node: Tag) -> str:
    """按文本块取文字：块之间换行，<br> 保留为换行。"""
    parts: list[str] = []
    for text in node.strings:
        parent_name = text.parent.name if text.parent else ""
        if parent_name == "br":
            parts.append("\n")
        elif parent_name in BLOCK_TAGS:
            parts.append("\n" + str(text) + "\n")
        else:
            parts.append(str(text))
    return "".join(parts)


# 行内嵌的网址（常被塞在正文句子中间）
INLINE_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+|(?:[a-z0-9\-]+\.)+(?:com|net|org|cc|cn|vip|la|tv|info|xyz|top|pw|me|io|us|co)(?:/\S*)?",
    re.IGNORECASE,
)
# 推广/荐书类的整段文字
PROMO_PATTERNS = (
    re.compile(r"您收到了?一个新的?章节更新"),
    re.compile(r"阅读链接"),
    re.compile(r"推荐阅读《"),
    re.compile(r"使用.{0,12}APP.{0,20}下载"),
    re.compile(r"【\s*搜\s*】|【\s*索\s*】|德旗小说网|人人书库"),
)


def strip_inline_urls(text: str) -> str:
    """去掉句子中间夹带的网址。"""
    return INLINE_URL_RE.sub("", text)


def is_ad_line(line: str) -> bool:
    """判断一行是否为广告/导航/网址等非正文内容。"""
    if not line or NAV_LINE_RE.search(line):
        return True
    if URL_LINE_RE.match(line):
        return True
    # 推广模式与域名判断不受行长限制（广告常被塞进正文句子中间）
    lowered = line.lower()
    if any(pattern.search(line) for pattern in PROMO_PATTERNS):
        return True
    if len(line) > AD_MAX_LINE_LEN:
        return False
    if any(domain in lowered for domain in AD_DOMAINS):
        return True
    if any(keyword in lowered for keyword in AD_KEYWORDS):
        return True
    parts = [p for p in NAV_SPLIT_RE.split(line) if p]
    if parts and all(p in NAV_WORDS for p in parts):
        return True
    return False


def clean_content(raw_text: str) -> str:
    """把正文原始文本清理为干净的段落文本（广告过滤核心）。"""
    text = normalize_text(raw_text)
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
        # 正文句子中间夹带的网址（不是整行网址）就地剥离
        line = strip_inline_urls(line).strip()
        if not line:
            continue
        lines.append(line)
    return "\n".join(lines)


def strip_boilerplate(container: Tag) -> None:
    """删除正文容器内的广告/脚本/无关元素（就地修改）。"""
    for tag in container.find_all(DROP_TAGS):
        tag.decompose()
    for selector in DROP_SELECTORS:
        for tag in container.select(selector):
            tag.decompose()


# ---------------------------------------------------------------------------
# 目录页解析
# ---------------------------------------------------------------------------


@dataclass
class TocEntry:
    """目录中的一条章节链接。"""

    title: str
    url: str
    order: int


@dataclass
class TocPage:
    """一个目录页的解析结果。"""

    entries: list[TocEntry] = field(default_factory=list)
    next_page: str | None = None
    # 目录是否被「分卷/最新章节」结构弄得碎片化；供诊断日志使用
    containers_tried: int = 0


def _anchor_text(a: Tag) -> str:
    """链接文字：优先 title 属性（很多站点用它放完整章节名）。"""
    text = (a.get("title") or "").strip() or a.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _link_candidates(scope: Tag, base_url: str, skip_external: str | None = None) -> list[TocEntry]:
    """从容器中收集所有形如「第X章」的链接。"""
    entries: list[TocEntry] = []
    for order, a in enumerate(scope.find_all("a", href=True)):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        url = _urljoin(base_url, href)
        title = _anchor_text(a)
        if not CHAPTER_TITLE_RE.match(title):
            continue
        if skip_external and _host(url) != skip_external:
            continue
        entries.append(TocEntry(title, url, order))
    return entries


def _urljoin(base: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, href)


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.lower()


def _dedupe(entries: list[TocEntry]) -> list[TocEntry]:
    """按 URL 去重，保留首次出现。"""
    seen: set[str] = set()
    result: list[TocEntry] = []
    for entry in entries:
        if entry.url in seen:
            continue
        seen.add(entry.url)
        result.append(entry)
    return result


def _next_toc_page(soup: BeautifulSoup, base_url: str, page_url: str) -> str | None:
    """目录页的「下一页」：优先带 rel=next 或 class 含 next 的链接。"""
    for a in soup.find_all("a", href=True):
        rel = " ".join(a.get("rel") or []).lower()
        classes = " ".join(a.get("class") or []).lower()
        text = a.get_text(strip=True)
        if rel == "next" or "next" in classes.split() or text in ("下一页", "下页", "后一页"):
            candidate = _urljoin(base_url, a["href"].strip())
            if candidate != page_url:
                return candidate
    return None


def parse_toc_page(
    soup: BeautifulSoup,
    page_url: str,
    *,
    min_chapter_links: int = 3,
) -> TocPage:
    """解析一页目录：先试已知容器，再退化为全页扫描。

    规则：优先选择「章节链接数量最多」的容器；如果整页章节链接很多
    （常见于无容器标记的站点），直接使用全页扫描结果。
    """
    base_url = page_url
    host = _host(page_url)

    best: list[TocEntry] = []
    containers_tried = 0
    for selector in TOC_SELECTORS:
        for scope in soup.select(selector):
            containers_tried += 1
            entries = _dedupe(_link_candidates(scope, base_url, skip_external=host))
            if len(entries) > len(best):
                best = entries

    whole_page = _dedupe(_link_candidates(soup, base_url, skip_external=host))

    # 全页扫描明显更多时（目录没有容器标记），用全页结果
    if len(whole_page) > len(best) * 1.2:
        chosen = whole_page
    elif len(best) >= min_chapter_links:
        chosen = best
    else:
        chosen = whole_page or best

    # 去掉站内「最新章节」区块造成的重复
    chosen = _dedupe(chosen)

    return TocPage(
        entries=chosen,
        next_page=_next_toc_page(soup, base_url, page_url),
        containers_tried=containers_tried,
    )


# ---------------------------------------------------------------------------
# 章节页解析
# ---------------------------------------------------------------------------


def page_chapter_title(soup: BeautifulSoup) -> str:
    """从章节页提取所属章节的标题。"""
    node = soup.select_one(
        ".bookname h1, .bookname h2, .content h1, #nr_title, .title h1, h1, #title, .title, .chapter-title"
    )
    if node is None:
        title = soup.find("title")
        return title.get_text(" ", strip=True) if title else ""
    return node.get_text(" ", strip=True)


def _link_density(node: Tag, text_len: int) -> float:
    link_len = sum(len(a.get_text(strip=True)) for a in node.find_all("a"))
    return link_len / text_len if text_len else 1.0


def _candidate_score(node: Tag) -> int | None:
    """正文容器打分：文字越多越好，块级段落越多越好，链接越少越好。

    返回 None 表示该节点不可能是正文（文字过少或链接密度过高）。
    """
    text_len = len(re.sub(r"\s", "", node.get_text()))
    if text_len < 8:  # 极短元素不可能是正文容器（广告/按钮）
        return None
    if _link_density(node, text_len) > 0.5:
        return None
    blocks = len(node.find_all(("p", "br", "div"), recursive=True))
    return text_len + min(blocks, 200) * 5


def find_content_node(soup: BeautifulSoup) -> Tag | None:
    """定位正文容器：先按常见选择器，再按结构启发式。"""
    best_node: Tag | None = None
    best_score = 0
    for selector in CONTENT_SELECTORS:
        for node in soup.select(selector):
            score = _candidate_score(node)
            if score is not None and score > best_score:
                best_score, best_node = score, node
    if best_node is not None:
        return best_node

    # 结构启发式：寻找「最多文字、最少链接」的块级容器
    for node in soup.find_all(("div", "article", "section", "main", "td")):
        score = _candidate_score(node)
        if score is not None and score > best_score:
            best_score, best_node = score, node
    return best_node


def extract_content(soup: BeautifulSoup) -> str:
    """从章节页面中提取正文；找不到正文返回空字符串。"""
    node = find_content_node(soup)
    if node is not None:
        strip_boilerplate(node)
        text = clean_content(block_text(node))
        if len(text) >= 8:
            return text

    # 兜底：整个 body 清理后取较长段落
    body = soup.body or soup
    strip_boilerplate(body)
    text = clean_content(block_text(body))
    lines = [line for line in text.split("\n") if len(line) >= 4]
    joined = "\n".join(lines)
    return joined if len(joined) >= 8 else ""


def extract_next_page(soup: BeautifulSoup, page_url: str) -> str | None:
    """章节页内的「下一页」链接。"""
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        rel = " ".join(a.get("rel") or []).lower()
        if text in NEXT_PAGE_WORDS or rel == "next":
            candidate = _urljoin(page_url, a["href"].strip())
            if candidate != page_url:
                return candidate
    return None
