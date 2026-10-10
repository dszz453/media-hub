# MediaHub · 跨平台视频转音频播放器

一个自建的轻量化 Web 服务：**搜索小红书 / 抖音 / YouTube 的视频 → 提取音轨 → 在线播放**，
界面参考网易云音乐 PC 端。

自己写 Dockerfile：主服务一个容器，扫码登录单独一个轻量浏览器容器，数据全部落在 `./data` 目录里。

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
| 扫码登录 | 网页内扫码登录小红书 / 抖音，凭据自动落盘，不用手动复制 Cookie |
| 去重缓存 | 同一个作品只转一次，重复请求直接命中已转好的文件 |

---

## 快速开始

```bash
git clone https://github.com/<你的用户名>/media-hub.git
cd media-hub
cp .env.example .env
docker compose up -d --build
```

打开 <http://localhost:8000>，左侧点「账号登录」，用手机 App 扫一扫即可。

日志：`docker compose logs -f`　　停止：`docker compose down`

> 首次构建会拉取 ffmpeg（约 200MB）和登录容器里的 Chromium（约 170MB）。
> 在路由器这类小机器上可能要十几分钟，之后就快了。

---

## 登录（扫码，推荐）

**小红书和抖音的接口都要求登录态**，但这两家的登录接口都**只认真实浏览器**：

- 小红书 web 的 `x-s` 签名算法持续升级。实测 `xhs==0.2.13` 的纯 Python 签名
  在 `/api/sns/web/v1/login/*` 上稳定返回 `406`（它用自定义字符表编码，
  而线上是 `XYS_` 前缀的新算法）—— 这一点已用真实 Chrome 抓包对比确认。
- 抖音的登录面板由风控 SDK 把守（`rc-verifycenter` / `rmc-nocaptcha`）。
  在 **headless** 浏览器里，风控 iframe 会直接 `ERR_ABORTED`，随后
  `/passport/web/get_qrcode/` 永远不返回，二维码区一直是个空白白框。
  换成 **有头模式**（Xvfb 虚拟屏）后风控才放行。

所以扫码交给独立的 `login-agent` 容器（内置 Chromium + Xvfb），主服务保持轻量：

```
网页「账号登录」──▶ 主服务 /api/login/* ──▶ login-agent (Chromium + Xvfb)
                       │                         │
                       │◀──── 二维码 data:URI ────┘（打开登录页，直接读 DOM）
                       │
                       │◀──── 轮询到登录成功 + Cookie
                       ▼
              data/cookies.json（落盘，优先级高于 .env）
```

> 二维码是**直接从 DOM 里读出来的**，不是截图。两家的二维码本来就是
> `<img src="data:image/png;base64,...">`（抖音 512×512、小红书 128×128），
> 读 `src` 拿到的就是无损原图；截图在小主机上又慢又不稳（实测能卡满 25 秒超时）。

操作步骤：

1. 打开 <http://localhost:8000> → 左侧「账号登录」
2. 点对应平台的「扫码登录」
3. 用手机 App 扫一扫并在手机上确认
4. 看到「登录成功」即可 —— 凭据已保存，重启容器也不用重扫

> **抖音要等得久一点，而且取决于机器性能**：
> 它首页是无限视频流，渲染进程一直被占满，实测在这台小主机上
> 每次页面查询就要 2~7 秒。完整流程是「加载首页 → 点「登录」→
> 面板渲染出来（15~40 秒）→ 风控 SDK 下发二维码 → 二维码渲染」，
> 全程 **60~150 秒**。小红书只要 10~20 秒。
> `LOGIN_TIMEOUT` 默认给了 360 秒，就是为了兜住抖音的最坏情况。

> **手动配置仍然支持**：把 Cookie 填到 `.env` 的 `XHS_COOKIE` / `DOUYIN_COOKIE`。
> 扫码得到的凭据优先级更高；想改回手动，在「账号登录」页点「退出登录」。

### 粘贴 Cookie 登录（扫码走不通时的备用通道）

账号页每张卡片上都有「粘贴 Cookie」按钮，贴进去点「校验并登录」即可。

**它会先真的搜一次，搜得动才保存。** 这一步是必须的：小红书会给**每个访客**
都发一个匿名 `web_session`，只看「有没有 cookie」会把访客会话当成登录态存下来，
表现为「账号页显示已登录，一搜就报登录已过期」。校验不通过会直接告诉你原因
（登录已过期 / 账号被风控 / Cookie 不完整），不会写进 `data/cookies.json`。

粘贴姿势随便，下面几种都认：

- 纯 cookie 串：`a1=...; web_session=...`
- 从 DevTools → Application → Cookies 表格里整块复制（Tab 分隔）
- 带名字的：`Cookie: a1=...; web_session=...`
- 整段 cURL：`curl '...' -H 'cookie: a1=...; ...'`

### 怎么复制才对（重要）

**第一步：先确认自己真的登录了。**

打开 <https://www.xiaohongshu.com>，看右上角 —— 必须是**你的头像**。
如果是「登录」按钮，说明你根本没登录，这时候复制出来的一定是访客会话。

想更确定的话，在控制台里跑这一句（小红书自己的接口，只读）：

```js
fetch('/api/sns/web/v2/user/me', {credentials: 'include'})
  .then(r => r.json())
  .then(r => console.log(r?.data?.guest === false
      ? '✅ 已登录，可以复制了'
      : '❌ 未登录（访客），请先登录', r));
```

看到 `✅ 已登录` 再往下走；看到 `guest: true` 就别复制了，复制也登不上。

**第二步：从哪里复制。**

方式 A（推荐，最不容易漏）：

```
F12 → Application → 左侧 Cookies → https://www.xiaohongshu.com
→ 表格里全选（Ctrl+A）→ 复制
```

方式 B：

```
F12 → Network → 刷新页面 → 点列表里第一个请求
→ Request Headers → 找到 cookie: 那一行 → 复制冒号后面的整串
```

**第三步：贴进来点「校验并登录」。** 校验不过会直接告诉你原因，不会写坏凭据。

> ⚠️ **绝对不要在控制台里用 `document.cookie` 复制。**
> `web_session` 是 **HttpOnly** Cookie，JavaScript 读不到它。
> 用 `document.cookie` 复制出来的串里**没有 web_session**，
> 看着挺长、字段挺全，其实是访客会话 —— 一定会报「登录已过期」。
> 抖音同理（`sessionid` / `UIFID` 也是 HttpOnly），请从 Network 的请求头里整串复制。

> 小红书这份 Cookie 里**必须包含** `a1`、`web_session`、`webId` 三个字段。
> 缺 `web_session` 就不用试了。

### 手动配置 Cookie（可选）

- **小红书**：浏览器登录 <https://www.xiaohongshu.com> → `F12` → Network →
  任意请求 → 复制完整 `Cookie` 请求头，填到 `XHS_COOKIE`。
  最少要有 `web_session`，建议 `a1` / `webId` 一起带上。
- **抖音**：浏览器登录 <https://www.douyin.com> → `F12` → Network → 复制完整
  `Cookie`（要包含 `ttwid`、`msToken`、`UIFID`），填到 `DOUYIN_COOKIE`。

> 实测（2026-10）：抖音不带 Cookie 时搜索返回 `status_code=2483 请先登录`，
> 详情返回 `403 Blocked by ArgusSecurityPlugin Uifid Not Found`。这不是 bug。

### YouTube

```ini
PROXY=http://192.168.1.2:7890
```

留空则直连。想下载会员视频可以再配 `YTDLP_COOKIES_FILE`（Netscape 格式 `cookies.txt`）。

---

## 平台可用性

| 平台 | 搜索 | 转音频 | 是否需要登录 | 备注 |
|---|---|---|---|---|
| YouTube | ✅ | ✅ | 否 | 走 yt-dlp `ytsearch`，国内需代理 |
| 小红书 | ✅ | ✅ | **是** | 在「账号登录」页扫码 |
| 抖音 | ✅ | ✅ | **是** | 在「账号登录」页扫码 |

启动后打开「设置」页可以实时看到每个平台当前是否可用、不可用的原因是什么。

---

## 目录结构

```
media-hub/
├── Dockerfile              # 主服务：python:3.11-slim + ffmpeg + tini，非 root 运行
├── docker-compose.yml      # 主服务 + 扫码登录代理
├── requirements.txt
├── .env.example
├── login-agent/            # 扫码登录代理（Chromium，独立容器）
│   ├── Dockerfile
│   └── app/
│       ├── main.py         # /health /platforms /session/*
│       ├── browser.py      # Chromium 池（常驻一个实例）
│       ├── platforms.py    # 各平台登录页与二维码选择器
│       └── sessions.py     # 扫码会话生命周期
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
| GET | `/api/login/status` | 各平台登录状态 |
| POST | `/api/login/{platform}/start` | 开始扫码，返回二维码 |
| GET | `/api/login/{platform}/poll/{sid}` | 轮询扫码结果 |
| POST | `/api/login/{platform}/cancel/{sid}` | 取消扫码 |
| DELETE | `/api/login/{platform}` | 退出登录 |

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

**「小红书」弹窗里显示的却是抖音的二维码（或反过来）**
这是前端的一个**竞态**，已经修掉了。原因：建会话请求要等很久
（抖音实测 190 秒），如果用户在这期间关掉弹窗、又去点另一个平台，
旧请求返回时会把**旧平台**的二维码画进**新平台**的弹窗。

修法是给 `loginState` 加了一个「代号」`gen`：`startLogin()` 和
`closeLoginModal()` 各自 `++gen`，`await` 回来后代号对不上就整个丢弃，
迟到的响应还会顺手把它建出来的会话取消掉。`pollLogin()` 同样做了校验。

> 如果你用的是**升级前**打开的页面，请 `Ctrl+F5` 强制刷新一次，
> 否则浏览器里跑的还是旧的 `app.js`。

**点「扫码登录」报「同时进行的扫码会话已达上限」**
也是已修的问题。老代码在会话名额（`MAX_SESSIONS=2`）满了之后直接抛 409，
而旧会话一旦没被取消就会赖着不走，于是所有扫码都失败。
现在建新会话前会先 `_make_room()`：关掉同平台的 pending 残留，
还满就挤掉最老的，不会再把人挡在门外。

**点「扫码登录」一直卡在「正在打开登录页…」**
首次启动要加载 Chromium 内核，慢的话 20~30 秒。抖音更慢（要过风控 +
面板渲染），60~150 秒都算正常。可以看登录容器的日志：

```bash
docker compose logs -f login-agent
```

**抖音二维码位置是个空白白框 / 一直不出二维码**
说明风控没放行。检查两点：

1. 容器必须跑在**有头模式**（`login-agent/app/browser.py` 里 `headless=False`），
   并且有一块虚拟屏。虚拟屏由 `login-agent/docker-entrypoint.sh` 显式拉起
   （**不要用 `xvfb-run`**：它靠 Xvfb 发 SIGUSR1 判定就绪，而本镜像的 Xvfb
   从不发这个信号，会导致容器 Up 但 8100 端口永远没服务、日志全空）。
   日志里应该能看到 `[entrypoint] Xvfb 就绪 DISPLAY=:99` 和
   `chromium 已就绪（有头模式）`。
2. 日志里出现 `抖音登录面板已就绪（共点击 N 次）` 才算点开成功。
   如果最后是 `准备阶段用尽 100s，抖音登录面板仍未出现`，多半是页面结构变了，
   用 `docker compose logs login-agent` 看实际情况。

**拿到的二维码是「小红书」logo 而不是二维码**
说明选图过滤被绕过了。`platforms.py` 里的 `_QR_DATA_JS` 只认
「屏幕尺寸 ≥110px + 宽高比 0.8~1.25 + class/id/alt 不含 logo/icon/avatar」
的 `data:image`。小红书登录页的 logo 也是 data:image（68×32），
过滤条件就是为它加的，别删。

**扫码后提示「二维码已过期」**
二维码有效期约 5 分钟，点弹窗里的「刷新二维码」重来一次。

**小红书搜索报「签名校验未通过」或返回 461 / 471**
说明触发了风控（通常是出口 IP 被判定有风险）。换个网络或稍后再试。
注意这**不是登录问题** —— 扫码登录走真实浏览器，签名由浏览器自己算。

**抖音提示「抖音要求登录」**
登录态过期了，在「账号登录」页重新扫一次码。

**登录容器太吃内存，想省一点**
不需要扫码时可以停掉它，主服务照常播放已下载的音频：

```bash
docker compose stop login-agent    # 要用扫码时再 start
```

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
- [Playwright](https://github.com/microsoft/playwright) —— 扫码登录的浏览器自动化
- [ReaJason/xhs](https://github.com/ReaJason/xhs) —— 小红书 SDK（其纯 Python `x-s` 实现已被上游风控淘汰，现仅作兜底）
- [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) —— `a_bogus` 签名算法（Apache-2.0，见 `app/vendor/NOTICE.md`）
- [JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader)、[marcobaobao/yt-dlp-webui](https://github.com/marcobaobao/yt-dlp-webui) —— 本项目的思路参考

## License

MIT
