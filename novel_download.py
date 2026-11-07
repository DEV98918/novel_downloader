import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# 目标小说目录页地址
INDEX_URL = "https://www.shuhaoxs.com/book/t0yp00p.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": INDEX_URL
}

def get_chapter_list(url):
    """获取所有章节的标题和第一页链接"""
    res = requests.get(url, headers=HEADERS)
    res.encoding = res.apparent_encoding or 'utf-8'
    soup = BeautifulSoup(res.text, 'html.parser')
    
    chapters = []
    for a in soup.select('dl dd a, .listmain a, #list a, .box_con dl dd a'):
        href = a.get('href')
        title = a.text.strip()
        if href and title:
            full_url = urljoin(url, href)
            chapters.append((title, full_url))
    return chapters

def get_chapter_content(base_chapter_url):
    """根据发现的 URL 规律，自动循环请求第 1 页、第 2 页、第 3 页...直到页面不存在"""
    full_text_list = []
    
    # 去除尾部的 .html，方便拼接多页后缀
    if base_chapter_url.endswith('.html'):
        base_prefix = base_chapter_url[:-5]
    else:
        base_prefix = base_chapter_url

    page = 1
    while True:
        # 构造每一页的 URL：第 1 页为 t0yp00p-96.html，第 2 页为 t0yp00p-96-2.html
        if page == 1:
            target_url = f"{base_prefix}.html"
        else:
            target_url = f"{base_prefix}-{page}.html"
            
        try:
            res = requests.get(target_url, headers=HEADERS, timeout=10)
            # 如果页面返回 404 或其他错误，说明该章节的分页已经读完
            if res.status_code != 200:
                break
                
            res.encoding = res.apparent_encoding or 'utf-8'
            soup = BeautifulSoup(res.text, 'html.parser')
            
            content_div = soup.select_one('.content, #content, #chaptercontent')
            if not content_div:
                break
                
            for s in content_div.select('script, style, .read-inline-img'):
                s.decompose()
                
            text = content_div.get_text(separator='\n')
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            
            if not lines:
                break
                
            full_text_list.append('\n'.join(lines))
            page += 1
            time.sleep(0.1) # 短暂间隔
        except Exception as e:
            break
            
    return '\n\n'.join(full_text_list)

def download_book():
    print("正在获取小说目录...")
    chapters = get_chapter_list(INDEX_URL)
    print(f"共找到 {len(chapters)} 章，开始下载（基于 URL 规律自动抓取多页）...")

    with open("小说全集_完美版.txt", "w", encoding="utf-8") as f:
        for index, (title, url) in enumerate(chapters, 1):
            print(f"[{index}/{len(chapters)}] 正在下载: {title}")
            content = get_chapter_content(url)
            
            if content:
                f.write(f"\n\n=== {title} ===\n\n")
                f.write(content)
            else:
                print(f"警告：{title} 内容获取为空。")
            
            time.sleep(0.5) # 章节间延迟，防止触发限流

    print("下载完成！已生成包含所有分页的完整小说：小说全集_完美版.txt")

if __name__ == "__main__":
    download_book()
