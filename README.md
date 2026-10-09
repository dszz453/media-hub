# MediaHub · 跨平台视频转音频播放器

一个自建的轻量化 Web 服务：**搜索小红书 / 抖音 / YouTube 的视频 → 提取音轨 → 在线播放**，
界面参考网易云音乐 PC 端。

自己写 Dockerfile，一个容器搞定，数据全部落在一个 `./data` 目录里。

```
搜索 ──▶ 结果列表 ──▶ [转音频] ──▶ yt-dlp / ffmpeg ──▶ mp3 落盘 ──▶ 播放器 / 收藏 / 歌单
```

---

## 功能

| 模块 | 说明 |
|---|---|
| 三平台搜索 | 关键词一次搜全平台，也可只搜单平台 |
| 一键转音频 | 视频 → `mp3`（或 `m4a`），自动写入标题、作者、封面 |
| 在线播放 | HTTP Range 流式播放，支持拖动进度条、不占内存 |
| 播放器 | 底部播放条、播放队列抽屉、列表/单曲/随机三种模式、音量、快捷键 |
| 收藏与歌单 | 收藏夹、自定义歌单、最近播放 |
| 任务队列 | 后台并发转码，实时进度百分比，失败可重试 |
| 链接解析 | 直接粘贴分享链接，不用先搜索 |
| 去重缓存 | 同一个作品只转一次，重复请求直接命中已转好的文件 |

---

## 快速开始

```bash
git clone https://github.com/<你的用户名>/media-hub.git
cd media-hub
cp .env.example .env
# 按下面「配置 Cookie」一节把 XHS_COOKIE / DOUYIN_COOKIE 填进 .env
docker compose up -d --build
```

打开 <http://localhost:8000>。

日志：`docker compose logs -f`　　停止：`docker compose down`

> 首次构建需要拉取 ffmpeg，约 200MB。之后改业务代码只会重建最后一层，很快。

---

## 配置 Cookie（重要）

**小红书和抖音的接口都要求登录态**，不配 Cookie 这两个平台不可用。
YouTube 不需要 Cookie，但国内网络需要 `PROXY`。

### 小红书

1. 浏览器登录 <https://www.xiaohongshu.com>
2. `F12` → **Network** → 随便点一个请求 → 复制完整 `Cookie` 请求头
3. 填进 `.env`：

```ini
XHS_COOKIE=a1=xxxxxxxx; web_session=0400698xxxxxx; webId=xxxxxxxx
```

最少要有 `web_session`，建议 `a1` / `webId` 一起带上。

> 签名用的是 `xhs` 库自带的**纯 Python** `x-s` 实现，**不需要 playwright 或 Chromium**。
> 如果本地签名被风控拦截，可以改用浏览器级签名服务：
> ```bash
> docker compose --profile sign up -d
> ```
> 然后在 `.env` 里设 `XHS_SIGN_SERVER=http://xhs-sign:5005`。

### 抖音

1. 浏览器登录 <https://www.douyin.com>
2. `F12` → **Network** → 复制完整 `Cookie` 请求头（要包含 `ttwid`、`msToken`、`UIFID`）
3. 填进 `.env`：

```ini
DOUYIN_COOKIE=ttwid=1%7Cxxx; msToken=xxx; UIFID=xxx; ...
```

> 实测（2026-10）：不带 Cookie 时搜索接口返回 `status_code=2483 请先登录，再继续搜索吧`，
> 详情接口返回 `403 Blocked by ArgusSecurityPlugin Uifid Not Found`。
> 所以抖音**必须**登录 Cookie，这不是 bug。

### YouTube

```ini
PROXY=http://192.168.1.2:7890
```

留空则直连。想下载会员视频可以再配 `YTDLP_COOKIES_FILE`（Netscape 格式 `cookies.txt`）。

---

## 平台可用性

| 平台 | 搜索 | 转音频 | 是否需要 Cookie | 备注 |
|---|---|---|---|---|
| YouTube | ✅ | ✅ | 否 | 走 yt-dlp `ytsearch`，国内需代理 |
| 小红书 | ✅ | ✅ | **是** | 纯 Python 签名，无需浏览器 |
| 抖音 | ✅ | ✅ | **是** | 无 Cookie 会返回 2483 / 403 |

启动后打开「设置」页可以实时看到每个平台当前是否可用、不可用的原因是什么。

---

## 目录结构

```
media-hub/
├── Dockerfile              # python:3.11-slim + ffmpeg + tini，非 root 运行
├── docker-compose.yml      # 主服务 + 可选的小红书签名 sidecar
├── requirements.txt
├── .env.example
└── app/
    ├── main.py             # FastAPI 入口、生命周期、静态资源挂载
    ├── config.py           # 全部环境变量集中在这里
    ├── db.py               # SQLite：曲库 / 收藏 / 歌单 / 历史 / 搜索缓存
    ├── schemas.py          # 请求响应模型
    ├── providers/          # 三个平台的适配层
    │   ├── base.py         # MediaItem / Resolved / Provider 抽象
    │   ├── xhs.py          # 小红书（xhs SDK + 可插拔签名）
    │   ├── douyin.py       # 抖音（自建 HTTP + vendor 的 a_bogus 签名）
    │   └── youtube.py      # YouTube（yt-dlp）
    ├── services/
    │   ├── audio.py        # yt-dlp / ffmpeg / 直链下载 的封装
    │   └── tasks.py        # asyncio 任务队列 + worker
    ├── routers/api.py      # 全部 HTTP 接口
    ├── vendor/             # 第三方 vendored 代码（见 NOTICE.md）
    └── static/             # 前端：index.html / style.css / app.js
```

### 设计要点

- **Provider 抽象**：每个平台只回答「怎么搜」和「怎么拿到可下载的地址」两个问题。
  新增平台只要实现 `search()` 和 `resolve()`。
- **两种下载路径**：`mode=direct`（小红书/抖音拿到 CDN 直链 → 下载 → ffmpeg 抽音轨）
  和 `mode=ytdlp`（YouTube 整条交给 yt-dlp）。前者避免依赖 yt-dlp 的 extractor 覆盖度。
- **进程内任务队列**：单容器场景不需要 Redis/Celery，`asyncio.Queue` + `to_thread` 就够了。
  进程被强杀后重启会自动把 `queued/running` 的残留任务重新入队。
- **轻量化取舍**：抖音签名只 vendor 了 `f2` 里自包含的 `abogus.py`（+ `gmssl`），
  而不是依赖整个 `f2`（会带进 protobuf / cryptography / pillow / pyexecjs / black 等一堆包，
  还会锁死 httpx、pydantic 版本）。详见 `app/vendor/NOTICE.md`。

---

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 + ffmpeg / yt-dlp 版本 |
| GET | `/api/platforms` | 各平台可用状态 |
| GET | `/api/search?keyword=&platform=&limit=` | 搜索，`platform` 可为 `all/xhs/douyin/youtube` |
| POST | `/api/extract` | 提交转音频任务 |
| GET | `/api/jobs` | 进行中 + 失败的任务 |
| GET | `/api/tracks?status=&platform=` | 曲库列表 |
| GET | `/api/stream/{id}` | 音频流（支持 Range） |
| GET | `/api/download/{id}` | 下载音频文件 |
| GET | `/api/cover/{id}` | 封面（本地缓存） |
| GET | `/api/proxy?url=` | 通用图片代理（解决防盗链） |
| GET/POST/DELETE | `/api/favorites[/{id}]` | 收藏 |
| GET/POST/DELETE | `/api/playlists[/{id}]` | 歌单 |
| GET | `/api/history` | 最近播放 |
| POST | `/api/parse` | 粘贴链接直接解析 |
| GET | `/api/stats` | 统计信息 |

交互式文档：<http://localhost:8000/docs>

---

## 本地开发（不用 Docker）

需要本机有 `ffmpeg` 和 `yt-dlp`。

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export XHS_COOKIE="..."        # Windows: set / $env:
export DOUYIN_COOKIE="..."
uvicorn app.main:app --reload --port 8000
```

跑一遍冒烟测试（不需要 ffmpeg、不联网，验证应用能起来、接口正常）：

```bash
pip install -r requirements.txt
python tests/smoke_test.py
```

---

## 常见问题

**小红书提示「签名校验未通过」或返回 461 / 471**
说明触发了风控。先确认 Cookie 里的 `a1` 与服务端一致；再不行就改用签名 sidecar
（`docker compose --profile sign up -d` + `XHS_SIGN_SERVER=http://xhs-sign:5005`），
并注意别请求太频繁。

**抖音提示「抖音要求登录」**
Cookie 缺失或过期了，重新从浏览器复制一份。

**YouTube 报 `Tunnel connection failed` / 无法下载**
网络问题，配置 `PROXY`。yt-dlp 也可以不重新构建镜像就升级：

```bash
docker compose exec media-hub pip install -U yt-dlp
docker compose restart media-hub
```

**转出来的音频没有声音 / 报「该视频不含音轨」**
原视频本身就是无声的（比如纯音乐配图、直播回放片段）。

**磁盘占用越来越大**
音频都在 `./data/cache/` 下，直接删文件即可；或在「设置 → 任务队列」里逐条删除曲目
（会同时删掉本地文件）。

**想改音频格式/码率**
`.env` 里改 `AUDIO_FORMAT=mp3|m4a`、`AUDIO_BITRATE=192k`，重启容器。注意 m4a 不支持封面内嵌。

---

## 免责声明

本项目仅供个人学习与自用，用于下载**你有权访问的内容**。
请遵守各平台的服务条款与著作权法律，不要用于批量抓取、商业分发或任何侵权用途。
因使用本项目产生的任何后果由使用者自行承担。

---

## 致谢

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) —— 下载内核
- [ReaJason/xhs](https://github.com/ReaJason/xhs) —— 小红书 SDK 与纯 Python `x-s` 签名
- [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) —— `a_bogus` 签名算法（Apache-2.0，见 `app/vendor/NOTICE.md`）
- [JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader)、[marcobaobao/yt-dlp-webui](https://github.com/marcobaobao/yt-dlp-webui) —— 本项目的思路参考

## License

MIT
