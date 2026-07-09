# Music Downloader

一个基于 Python + Tkinter 的本地桌面音乐下载工具，支持多平台搜索与下载。

## 概览

| 项目 | 内容 |
| --- | --- |
| 语言 | Python 3.10+ |
| UI 框架 | Tkinter |
| 支持平台 | 网易云音乐 / QQ 音乐 / YouTube |
| 文件格式 | MP3（自动转换） |
| 运行方式 | 双击 `.pyw` 文件，无需终端 |

## 核心功能

### 1. 多平台下载

按优先级自动尝试下载源：

1. 网易云音乐
2. QQ 音乐
3. YouTube

### 2. Search 模式

- 输入歌曲名，或者 `歌名 + 歌手`
- 支持用 `|` 或换行分隔多首歌
- 支持中英韩混合输入

### 3. Playlist 模式

- 粘贴网易云或 QQ 音乐歌单链接
- 自动读取歌单内歌曲

### 4. Track List 面板

- 点击 Download 后展开歌曲列表
- 默认全选，可手动调整后再下载
- 支持单击、Ctrl+单击、Shift+单击、双击等选择方式

### 5. 下载状态实时显示

- 下载中：蓝色高亮
- 成功：绿色，可点击播放
- 失败：红色背景
- 已存在跳过：灰色，可点击播放
- 右上角显示失败数量

### 6. 文件命名

统一命名为：

```text
歌名-歌手.mp3
```

### 7. 重复下载检测

下载前会自动检查目标路径是否已有同名文件，若存在则跳过，并支持直接播放。

### 8. Cookie 管理

- 网易云、QQ 音乐、YouTube 都需要登录 Cookie 才能正常使用
- Cookie 保存为本地 `.txt` 文件，启动时自动检查
- 缺失或无效时会在状态栏提示

## 项目结构

```text
music/
├── music_ui.pyw          # 主程序，双击运行
├── netease_cookie.txt    # 网易云 Cookie
├── qq_cookie.txt         # QQ 音乐 Cookie
├── youtube_cookies.txt   # YouTube Cookie
└── downloads/            # 默认下载目录
```

## 环境依赖

```bash
pip install requests pycryptodome yt-dlp
```

此外还建议安装：

- `ffmpeg`：用于音频格式转换
- `Node.js`：yt-dlp 的 JS challenge solver 依赖
- `Deno`：yt-dlp 的 EJS runtime 依赖

## 隐私说明

Cookie 文件属于本地私密信息或运行缓存，默认不纳入 GitHub 仓库。

## 运行

直接双击 `music_ui.pyw` 即可启动桌面界面。
