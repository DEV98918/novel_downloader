# Novel Downloader / 小说自动下载器

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

A robust Python script designed to automatically download serialized novels from online catalog sources (such as `shuhaoxs.com`), featuring automatic multi-page chapter stitching, rate-limit management, and clean text output.

### Features
- **Multi-page Stitching**: Automatically detects and concatenates multi-page chapters (e.g., `-2.html`, `-3.html`) into a single cohesive text.
- **Rate Limit Protection**: Built-in request delays to prevent server-side rate-limiting and bans (`ApiError` prevention).
- **Clean Output**: Automatically strips unwanted HTML tags, scripts, and advertisements, outputting clean plain text.

### Installation & Setup

1. Clone the repository and navigate to the project folder:
   ```
   git clone [https://github.com/your-username/novel-downloader.git](https://github.com/your-username/novel-downloader.git)
   cd novel-downloader
   ```
2. Create and activate a Python virtual environment:

```
python3 -m venv venv
source venv/bin/activate
```

3. Install dependencies:

```
pip install requests beautifulsoup4
```
### Usage

1. Open download.py and update the INDEX_URL with your target novel's catalog page URL.

2. Run the script:
```bash

python download.py
```

3. The complete novel text will be saved as a clean .txt file in the directory.
## 中文

一个基于 Python 编写的现代化小说自动下载脚本，专为抓取连载小说而设计。支持多页章节自动拼接、请求频率控制（防封禁）以及正文净化。

### 功能特点

- **多页章节拼接**：针对单章内容被拆分多个页面的网站（如 `-2.html`, `-3.html` 等），能够自动循环追踪并无缝拼接为完整章节。
- **智能限流防护**：内置请求延迟设置，有效避免触发服务器端防刷频限制（`ApiError`）。
- **内容净化**：自动过滤网页脚本、广告及无关标签，输出排版整洁的纯文本。

### 安装与配置

1. 克隆仓库并进入项目目录：


   ```
   git clone [https://github.com/你的用户名/novel-downloader.git](https://github.com/你的用户名/novel-downloader.git)
   cd novel-downloader
   ```

2. 创建并激活 Python 虚拟环境：



   ```
   python3 -m venv venv
   source venv/bin/activate
   ```

3. 安装依赖库：



   ```
   pip install requests beautifulsoup4
   ```

### 使用方法

1. 打开 `download.py`，将 `INDEX_URL` 修改为你想要下载的小说目录页网址。

2. 运行脚本：

   ```
   python download.py
   ```

3. 下载完成后，程序会自动在同目录下生成格式整洁的 `.txt` 小说全集文件。

### License

This project is open-source under the MIT License.
