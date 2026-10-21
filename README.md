# b站视频插件

这是从 `dsh-bili-scroll` 二次开发的个人资料库。你主动提交链接，归档**一个分P**的视频、元数据、真实字幕与截图；另外可以只导出收藏夹的视频标题和链接。

没有每日推荐、定时挑选、自动评论、私信或收藏。提供带认证的管理网页，支持本地资料增删、回收站、收藏夹分类，以及经确认后移动/删除本人 B站收藏条目。可显式启用带认证的远程 MCP，供 Codex / ChatGPT 读取字幕与实际截图并总结。不自动运行 Whisper 或上传完整媒体到云端。

## 环境和安装

支持 **macOS / Linux**，Python 3.10+（推荐 3.12）、Node.js 22+、FFmpeg 和 ffprobe。SQLite 使用 Python 标准库。Windows 的进程锁适配尚未实现。

先在项目目录运行只读检查：

```sh
node tools/library.mjs doctor
```

若只有旧 Python 或没有 Python，运行以下命令：它会复用已有 uv，或使用原项目的 uv 下载逻辑，在资料库内创建 Python 3.12 独立环境并安装 yt-dlp。不会更改系统 Python，不安装 Whisper / torch。

```sh
node tools/library.mjs setup
node tools/library.mjs doctor
```

FFmpeg 和 ffprobe 复用 PATH 中的安装，也可用私有工具路径，**不要求全局安装**：

```sh
export BILI_FFMPEG=/你的工具目录/ffmpeg
export BILI_FFPROBE=/你的工具目录/ffprobe
```

`setup` 只安装 Python 环境和 yt-dlp；FFmpeg/ffprobe 二进制由你放入工具目录，`doctor` 会检查能否执行。下载媒体时不重新编码，FFmpeg 使用一个线程，下载默认限速 2 MiB/s。不会改 VPN、系统代理或其他服务。

所有命令也可以由你自己的 Python 3.10+ 环境直接执行：`python -m bili_library ...`。Node 启动器支持 `BILI_LIBRARY_PYTHON` 指定解释器。`npm link` 后可使用短命令 `bili-library`，也可以始终使用 `node tools/library.mjs`。

## 数据目录

默认 `$DSH_HOME/bili-duty/library/`；没有设置 `DSH_HOME` 时为 `~/.dsh/bili-duty/library/`。可用环境变量覆盖：

```sh
export BILI_LIBRARY_HOME=/你的数据目录/bili-library
node tools/library.mjs init
```

Python CLI 另外支持全局 `--home /路径`；使用自动 `setup` 时应通过 `BILI_LIBRARY_HOME` 选择环境及数据位置。

```text
library/
  config.json
  index.sqlite3
  worker.lock
  secrets/
    bilibili.json          # B站账号凭证，0600
    models.json            # 预留模型密钥文件，第一阶段不创建、不启用
  videos/BVxxxxxxxxxx_CID/
    metadata.json
    video.mkv              # 或实际下载的 mp4/webm
    subtitles.json
    frames/index.json
    frames/*.jpg
  tmp/                     # 任务临时目录，正常失败/取消后清理
  exports/                 # 收藏夹导出
  .venv/                   # setup 创建的独立环境
```

配置、凭证和媒体都不写入插件包。POSIX 权限：资料库目录 0700、凭证及 JSON 文件 0600。模型密钥与 B站凭证独立存放，不放进源码、返回结果或日志。

## 提交、处理与查询

```sh
# 显式选择 P2；不带 p 参数仅处理 P1，不遍历合集
node tools/library.mjs submit 'https://www.bilibili.com/video/BVxxxxxxxxxx?p=2'
# BV 号和 b23.tv 短链接也支持
node tools/library.mjs submit 'https://b23.tv/Sh6MJ5i'

# 前台串行处理当前队列后退出；提交本身不开始下载
node tools/library.mjs worker --drain

node tools/library.mjs task
node tools/library.mjs task 任务ID
node tools/library.mjs list
node tools/library.mjs show BVxxxxxxxxxx_CID
node tools/library.mjs cancel 任务ID
node tools/library.mjs retry 任务ID
```

任务状态：`queued`、`running`、`completed`、`failed`、`cancelled`；运行阶段见 `stage`。错误信息保存在任务中，CLI 执行失败返回非零退出码。任务中断后，下次 worker 将其标记失败，使用 `retry` 显式重试。worker 不常驻、不注册定时器。

同一 BV 的不同 CID 是不同资料。相同资料已有完整视频、元数据、字幕状态文件及初始截图时，重复提交命中 `stage: cached`，不重新联网下载；缺失的文件会补齐。字幕缺失也可以归档成功，`subtitle_status` 独立显示，不能据此声称已读完课程。

## 登录与字幕

可保留 DSH 插件的扫码登录和主依赖安装界面；重启 DSH 后设置项显示为“B站资料库”。默认数据位置下自动复用其 `bili_credential.json`。也可以导入原扫码登录生成的 JSON 文件：

```sh
node tools/library.mjs credentials-import /私有目录/bili_credential.json
node tools/library.mjs subtitles BVxxxxxxxxxx_CID --json
# 登录后仅刷新字幕，不重新下载视频
node tools/library.mjs subtitles BVxxxxxxxxxx_CID --refresh
```

凭证 JSON 使用 `sessdata`、`bili_jct`、`buvid3`、`dedeuserid` 四个字段。不在命令参数中输入 Cookie 值。

字幕只读取平台字幕轨，人工字幕优先于 AI 字幕；不会把弹幕、简介或平台摘要当字幕。统一格式：

```json
{
  "bvid": "BVxxxxxxxxxx",
  "cid": 123456,
  "status": "available",
  "source": "platform_manual",
  "language": "zh-CN",
  "segments": [{"start": 1.2, "end": 3.4, "text": "课程原文"}],
  "reason": null
}
```

无字幕记为 `missing`；接口失败记为 `unavailable`，附可读原因。`bili_library/transcription.py` 只定义未来云端转写接口；第一阶段即使自行放入密钥，也不会开始上传或转写。

## 截图与补帧

```sh
node tools/library.mjs frames BVxxxxxxxxxx_CID
node tools/library.mjs frames BVxxxxxxxxxx_CID --export /你的目录/frames
node tools/library.mjs frames-add BVxxxxxxxxxx_CID --at 00:01:23 --at 100.5
node tools/library.mjs frames-add BVxxxxxxxxxx_CID --from 00:02:00 --to 00:03:00 --every 10
```

初始最多 20 张，每次补帧最多 20 张，时间点必须小于视频时长。索引同时记录 `requested` 和解码帧的实际 `timestamp`，不是把请求时间伪装成实际时间。重复请求相同时间复用图片；区间包含起点以及落在间隔上的终点。

CLI 可以导出实际 JPEG 文件，管理网页可直接查看和保存截图。远程 MCP 返回实际图片内容及时间戳，而非要求客户端访问服务器路径。

## 导出收藏夹标题和链接

收藏夹 ID 是网页地址的 `fid`，例如 `https://space.bilibili.com/123/favlist?fid=456` 中的 `456`。登录后也可以列出自己的收藏夹：

```sh
node tools/library.mjs favorites folders
# 公开收藏夹也可以指定所有者 UID，不一定需要登录
node tools/library.mjs favorites folders --uid 123

node tools/library.mjs favorites export 456
node tools/library.mjs favorites export 'https://space.bilibili.com/123/favlist?fid=456'
node tools/library.mjs favorites export 456 --format csv --out /你的目录/收藏夹.csv
node tools/library.mjs favorites export 456 --format markdown --out /你的目录/收藏夹.md
```

逐页读取并按资源身份去重，输出标题、BV、规范视频链接及条目状态。影视／番剧条目保留接口给出的播放链接，标记 `download_supported: false`；失效和未知条目保留状态，不编造 BV 或链接；私有收藏夹需要所属账号有效凭证。接口错误或分页异常直接失败，不悄悄导出截断列表。最多 1000 页、300 秒；导出时收藏夹被修改仍可能造成列表变化，结果不是事务快照。

默认保存到 `exports/favorites-ID.json`，已有文件不覆盖，重复导出请指定新的 `--out`。CSV 防止标题触发电子表格公式，Markdown 转义标题。**CLI 导出不会提交下载任务或修改收藏状态。网页中可显式勾选条目加入下载队列，或经单独预览和确认执行 B站移动/删除。**

## 资源限制与网络策略

修改资料库 `config.json` 后新任务生效：

| 字段 | 默认值 |
|---|---:|
| `max_duration_seconds` | 3600 |
| `max_download_bytes` | 1073741824（音视频合计 1 GiB） |
| `task_timeout_seconds` | 1800 |
| `quota_bytes` | 10737418240（整个资料库含环境、临时文件） |
| `min_free_bytes` | 2147483648 |
| `initial_frames` / `max_extra_frames` | 20 / 20 |
| `download_rate_bytes` | 2097152 |

跨进程锁保证 worker、补帧和收藏夹导出不会并行执行。下载前检查预计体积与合并空间，下载期间检查实际字节和磁盘，worker 监控超时、取消及磁盘配额，终止整组子进程。不会自动删除已有资料。

只接受预期 B站 HTTPS 视频地址；所有 API、短链跳转和媒体请求都限制在 B站及指定 CDN 域名。连接前检查全部 DNS 结果并固定实际连接 IP，拒绝内网、回环、链路本地等地址。yt-dlp 经过任务内部的短期、带随机认证的回环 CONNECT 代理，以应用同一策略；该代理不提供资料 API、不监听公网。FFmpeg 只读本地媒体。

不继承 `HTTP_PROXY`、`HTTPS_PROXY` 等应用代理变量，避免绕过目标检查；系统 VPN 的正常路由不修改。仅能通过本地 HTTP/SOCKS 代理上网的配置当前不支持，会明确失败。

若系统DNS给B站返回`198.18.*`等Fake-IP，可显式设置`BILI_DNS_MODE=cloudflare`，仅为资料库使用带证书验证、固定公网引导的Cloudflare DoH（IPv4）。它仍检查所有返回IP并固定连接，不自动回退系统DNS、不修改VPN或全局DNS。见 [Linux DNS 配置与验收](deploy/DNS-linux.md)。

## 测试

```sh
python3 -m unittest discover -s tests -v
node selfcheck.mjs
# 有 FFmpeg/ffprobe 和 yt-dlp 的环境运行全部真实媒体测试
BILI_FFMPEG=/路径/ffmpeg BILI_FFPROBE=/路径/ffprobe \
  /你的Python环境/bin/python -m unittest discover -s tests -v
```

离线测试涵盖分P、缓存、任务互斥/取消/超时、字幕来源、收藏夹分页/去重/失效条目、内网及重定向拦截、凭证隔离和导出转义。真实媒体测试生成本地样本并验证 ffprobe、实际截图时间和重复归档缓存；不会下载陌生视频。

原项目来源、许可证与署名保留在 `LICENSE`、`NOTICE.md` 和 `CONTRIBUTORS.md`。


## 网页后台使用

远程 MCP（独立令牌 / OAuth、Mac Codex、ChatGPT Work、真实图片响应）：见 [MCP 部署与配置](deploy/MCP-linux.md)。默认关闭，不在服务器运行模型。

Linux 域名 HTTPS 访问（包含 Xray 端口冲突处理）：见 [部署指南](deploy/HTTPS-linux.md)。默认仍仅监听回环；反向代理前显式配置 `BILI_WEB_PUBLIC_ORIGIN`。

初始化环境后运行：

```sh
node tools/library.mjs web
# 或 npm run web
```

浏览器打开 `http://127.0.0.1:8765`。首次启动生成独立后台令牌，保存在资料库 `secrets/web-token`；在自己的终端读取并粘贴到登录框：

```sh
cat "${BILI_LIBRARY_HOME:-${DSH_HOME:-$HOME/.dsh}/bili-duty/library}/secrets/web-token"
```

后台本身不打印令牌。登录会话有效 8 小时，退出或重启后重新登录。每个资料库只允许运行一个网页后台。Ctrl+C 停止前台运行的网页；它只会请求取消自己启动的下载 worker，不会停止另一个终端启动的 CLI worker。

- **视频资料库**：提交链接、手动处理队列、搜索、下载原视频、查看字幕及实际截图、按时间点/区间补帧。
- **下载任务**：按最近尝试时间从新到旧显示，包含提交、开始、结束时间与本次耗时（浏览器本机时区）；取消或重新排队后仍需点击处理队列。最多显示最近200条，批量清理覆盖所有已结束记录。
- **下载诊断日志**：点击每条任务的“查看日志”。记录提取、格式检查、传输、校验、字幕和截图阶段，区分HTTP状态码、登录权限、格式、DNS/非公网地址、TLS、空间、大小及超时等错误，并记录yt-dlp版本、是否读取凭证和下载限制。只保存固定诊断信息，不记录Cookie、令牌、原始请求头、媒体地址或原始异常；无法分类的错误明确标记为未知。
- **记录与日志清理**：可经确认单条删除或批量清除已结束的下载记录、任务诊断日志和后台操作记录；排队/运行中的记录保留；有下载或媒体操作持锁时需等结束后清理。删除下载记录同时删除其诊断日志，单独删除日志保留任务错误摘要；不会删除视频、字幕、截图或修改B站收藏。后台操作结果是独立摘要，需要单独清理。SQLite释放的页会供后续记录复用，不自动执行阻塞服务的VACUUM。
- **升级兼容**：已有数据库自动增加时间与日志字段；旧任务未记录的时间显示为“—”，旧错误不能补回诊断详情，重新排队后产生新日志。每个任务最多保留最近200条诊断事件。
- **回收站**：移除视频时连同字幕和截图一起移入，可恢复；永久删除需要再次确认，之后释放空间。
- **收藏夹整理**：同步 ID/链接或导入 CLI JSON，搜索、分类筛选、重复收藏筛选、批量标签与备注、关键词分类预览、导出 JSON/CSV/Markdown。同步不会覆盖已有本地标注。只下载你勾选并确认加入队列的普通 BV 视频，每个默认 P1。
- **设置与配置**：修改资源限制和本地分类关键词规则，上传 B站凭证 JSON，检查工具及磁盘状态。没有模型 API 的伪配置项。

本地移除与远程取消收藏是两个独立动作。JSON/CSV 导出包含分类、标签、备注；Markdown 用于阅读标题和链接。网页导出按所选收藏夹和分类筛选，搜索关键词不限制导出范围。

### B站账号上的移动 / 删除

默认关闭，启动前显式启用：

```sh
BILI_REMOTE_WRITES=1 node tools/library.mjs web
```

1. 在设置页上传有效的 `bili_credential.json`，或者复用 DSH 的扫码登录凭证。
2. 同步源收藏夹，在列表中勾选视频，点击“移动 / 删除 B站收藏”。
3. 读取当前账号自己的收藏夹。移动时选择另一个已有收藏夹；新建收藏夹请先在 B站完成。
4. 生成预览：后台验证当前登录账号、源/目标归属、条目是否仍存在，并保存修改前的清单。
5. 核对具体标题及目标，输入提示的确认文字，如“移动3条”，再点击执行。
6. 后台执行一次请求后，重新读取源/目标列表复核；只有核对通过的条目才记为成功。结果不确定或部分未确认时不自动重试，先在 B站或同步结果中检查。

一次最多 50 条普通 BV 视频，只能操作自己的收藏夹；影视/番剧条目暂不支持远程写操作。操作预览 10 分钟有效、只可执行一次。修改前清单保存在 `exports/before-remote-预览ID.json`，用于人工核对，它不是自动回滚机制。删除表示从源收藏夹取消收藏，不删除 UP 主视频，也不删除本地归档。

远程写操作依据社区维护的 [B站收藏夹接口资料](https://github.com/bilibili-plugins/bilibili-api-collect/blob/master/docs/fav/action.md)适配；接口变化或风控可能导致失败。实现通过模拟接口测试覆盖归属、确认、单次执行、结果复核和不确定状态；开发验收没有对真实账号执行移动/删除。

## 完整配置方案

### 一、本机独立使用（macOS / Linux）

不需要先启动 DSH。在项目目录执行，FFmpeg/ffprobe 路径替换为自己的工具：

```sh
export BILI_LIBRARY_HOME="$HOME/.dsh/bili-duty/library"
export BILI_FFMPEG=/绝对路径/ffmpeg
export BILI_FFPROBE=/绝对路径/ffprobe
node tools/library.mjs setup
node tools/library.mjs doctor
node tools/library.mjs web
```

默认 Python 环境在数据目录 `.venv/`，uv 管理的 Python 下载与缓存也放进数据目录。`setup` 只安装 Python 和 yt-dlp，不自动安装 FFmpeg，不安装 Whisper。

### 二、复用 DSH 登录

默认数据目录下，网页和 CLI 会优先读取 `library/secrets/bilibili.json`，没有时读取其父目录的 `bili_credential.json`。DSH 仍可负责扫码登录，资料库后台独立运行。

如果自定义了资料库位置，用 CLI `credentials-import` 或网页上传 JSON，避免把凭证复制到源码目录。主依赖环境、资料库环境、原本地 Whisper 环境彼此独立；资料库不会自动调用 Whisper。

### 三、固定启动环境文件

仓库提供 `tools/library.env.example`。复制到仓库根目录的 `library.env`（已忽略，不提交 Git），编辑为本机路径：

```sh
cp tools/library.env.example library.env
chmod 600 library.env
# 编辑后载入，启动；新终端需要重新载入
set -a
. ./library.env
set +a
node tools/library.mjs web
```

只载入自己维护的配置文件。启动器不会自动读取 `.env`，这样不会意外执行陌生文件里的内容。

| 环境变量 | 默认 / 用途 | 生效方式 |
|---|---|---|
| `BILI_LIBRARY_HOME` | 覆盖资料库目录 | 启动前设置 |
| `DSH_HOME` | 未指定资料库目录时，取其 `bili-duty/library` 子目录 | 启动前设置 |
| `BILI_LIBRARY_PYTHON` | 覆盖 CLI/网页解释器；默认资料库 `.venv`，再退回 `python3` | 启动前设置 |
| `BILI_FFMPEG` | FFmpeg 可执行文件绝对路径，默认 PATH | 重启后台 |
| `BILI_FFPROBE` | ffprobe 可执行文件绝对路径，默认 PATH | 重启后台 |
| `BILI_WEB_PORT` | 网页端口，默认 `8765` | 重启后台；`web --port` 优先 |
| `BILI_WEB_TOKEN` | 可选，至少24字符；优先于令牌文件 | 重启后台；空字符串不合法 |
| `BILI_REMOTE_WRITES` | 仅精确值 `1` 启用 B站收藏夹移动/删除，默认关闭 | 重启后台 |
| `BILI_DNS_MODE` | `system`（默认）或 `cloudflare`（资料库独立IPv4 DoH） | 重启后台；CLI启动时读取 |

没有 `BILI_WEB_HOST` 开放监听项：当前只绑定 `127.0.0.1`。远程浏览器访问需显式配置 `BILI_WEB_PUBLIC_ORIGIN` 并经HTTPS反向代理；MCP另需显式启用和认证，详见部署指南。

### 四、Linux 服务器 + Mac 远程访问

服务器安装环境并保持回环监听：

```sh
# 服务器项目目录内；数据目录需归当前运行用户所有
export BILI_LIBRARY_HOME=/srv/bili-library
export BILI_FFMPEG=/usr/bin/ffmpeg
export BILI_FFPROBE=/usr/bin/ffprobe
node tools/library.mjs setup
node tools/library.mjs web --port 8765
```

Mac 上建立 SSH 隧道，浏览器仍使用匹配白名单的相同端口：

```sh
ssh -N -L 8765:127.0.0.1:8765 用户名@服务器
```

然后打开 `http://127.0.0.1:8765`，使用服务器数据目录中的后台令牌登录。服务器防火墙无需放行 8765；如果本机端口占用，请让服务端端口及隧道本地端口一起改为同一值，例如 8877。

1核1GB机器建议：不运行本地 ASR，保持单任务，初始截图 8～12 张，下载限速 524288 字节/秒。网页无需 Node 前端构建或 npm 依赖，运行时使用 Python 标准库 HTTP 服务和原 SQLite；Node 只负责启动引导。

### 五、资源配置完整示例

资料库 `config.json`，或者直接在网页设置页修改。下例偏向低资源机器：

```json
{
  "max_duration_seconds": 3600,
  "max_download_bytes": 536870912,
  "task_timeout_seconds": 1800,
  "quota_bytes": 10737418240,
  "min_free_bytes": 2147483648,
  "initial_frames": 10,
  "max_extra_frames": 20,
  "download_rate_bytes": 524288
}
```

数值均为整数；时间用秒、大小用字节，截图计数 1～20。网页改动在后台空闲时保存，新操作读取新配置；设置页保存时若下载器持锁，会提示等待。修改工具路径、端口、后台令牌和远程写开关需重启，资源限制和分类规则不需重启。

磁盘统计包括环境、缓存、导出及回收站。移入回收站不会释放空间，永久删除才释放；超出配额时先清理回收站或调整配额。不自动删除资料。

### 六、本地分类规则

规则存放在资料库 `organize-rules.json`。第一次未保存时使用内置规则，网页设置页可编辑：

```json
[
  {"category":"线性代数","keywords":["矩阵","线性代数","特征值"]},
  {"category":"计算机基础","keywords":["408","数据结构","计组","计网"]},
  {"category":"AI 工具","keywords":["DeepSeek","Codex","DSH"]}
]
```

按顺序、不区分英文大小写匹配标题，第一条命中生效。没有命中的保持未分类；已有分类始终保留。单独编辑或批量编辑可以清空/修改分类、标签、备注。此功能不调用模型，也不根据推测的内容修改 B站收藏夹。

### 七、凭证、后台令牌与未来模型密钥

| 文件 | 内容 | 当前是否使用 |
|---|---|---|
| `secrets/web-token` | 网页登录令牌 | 使用；若设置 `BILI_WEB_TOKEN` 则以环境值为准 |
| `secrets/mcp-token` | 独立MCP访问和授权确认令牌 | 显式启用MCP后生成，不复用网页令牌 |
| `secrets/mcp-oauth.sqlite3` | OAuth客户端及哈希后的令牌/授权码 | 启用MCP后使用，持久化授权，不提交Git |
| `secrets/bilibili.json` | B站扫码登录四字段 | 字幕、私有收藏夹、本人收藏夹修改使用 |
| 父目录 `bili_credential.json` | 原 DSH 登录凭证 | 没有上述独立 B站凭证时复用 |
| `secrets/models.json` | 未来云端模型密钥预留 | 不创建、不读取，不会因放入文件而自动启用 |

后台上传凭证仅保存，不回显；访问令牌不要填成 B站 Cookie。轮换后台令牌时停止服务，在私有环境设置新的随机 `BILI_WEB_TOKEN` 后重启，旧会话随重启失效；也可以停服后在本机替换 `secrets/web-token` 内容并保持0600权限。任何令牌都不要放进网页 URL、Git、公开截图或命令日志。

### 八、保持运行与备份

前台启动是默认方案，关闭终端/Ctrl+C 后网页停止。长期运行可由现有进程管理器管理：工作目录设为本仓库，执行 `node tools/library.mjs web`，以普通用户运行，环境变量使用上述方案，不需要管理员权限。进程管理器只维持网页服务，不会自动开始下载队列或扫描收藏夹。

为避免自动重启打断远程操作，远程修改执行期间不要重启服务。中断中的远程计划会记为结果不确定，必须先同步核对，不能直接再次执行同一预览。

备份时停止网页和 CLI worker，再复制整个数据目录；恢复时使用独立目录，设置 `BILI_LIBRARY_HOME` 指向它。不要在 SQLite 或视频正在写入时直接复制。备份含账号凭证，应保存在私有位置。跨机器迁移后检查 Python 环境和媒体工具路径；虚拟环境不能保证跨平台搬迁可用，必要时在新机器重新 `setup`。

### 九、常见问题

- **登录后仍无字幕**：凭证可能过期，或视频本身无字幕。重新登录后单独刷新字幕；当前不进行云端转写。
- **后台说已有任务在运行**：网页与 CLI 共用跨进程锁，等待当前操作结束，或取消相应下载任务。
- **移动/删除按钮无法执行**：检查 `BILI_REMOTE_WRITES=1`、有效 CSRF 凭证、源/目标均属于自己、仅选普通 BV 视频且不超过50条。
- **远程结果不确定**：可能网络在写请求之后断开；先同步源、目标核对，已保存修改前清单，不自动重试。
- **首次开网页看不到验收资料**：验收在 `/tmp/bili-library-acceptance`；正式默认目录是独立资料库。可以导入导出的 JSON，或显式选择原数据目录。临时目录可能被系统清理。
- **视频没有在线播放**：当前提供原文件下载；MKV 等封装不保证浏览器播放，不额外转码消耗 CPU。
- **代理网络无法访问**：程序不使用应用 HTTP/SOCKS 代理变量；系统 VPN/TUN 路由不变。不能通过关闭公网 IP 检查来绕过这一限制。
- **Windows**：本版本尚未适配 POSIX 进程锁和进程组，使用 macOS 或 Linux。

### 十、可选后台常驻模板（不自动安装）

仓库 `deploy/` 提供两份模板，均需要你替换绝对路径；当前开发没有替你启用登录启动或系统服务。

**Linux 用户级 systemd**：模板 `deploy/bili-library.service.example`。先确认 `node` 的实际绝对路径，再替换 `WorkingDirectory`、`ExecStart`、`EnvironmentFile`。环境文件按 `KEY=value` 写，使用绝对路径，不能依赖 `$HOME` 等 shell 展开。令牌保留在资料库私有文件，不需要写进 unit。初始化资料库和环境后，将修改后的 unit 保存到 `~/.config/systemd/user/bili-library.service`：

```sh
systemctl --user daemon-reload
systemctl --user enable --now bili-library.service
systemctl --user status bili-library.service
# 停止并取消自动启动
systemctl --user disable --now bili-library.service
```

需要退出SSH后仍提供网页/MCP时，在服务器为运行服务的同一用户显式启用linger：`sudo loginctl enable-linger "$(id -un)"`，再用`loginctl show-user "$(id -un)" -p Linger`确认`Linger=yes`。否则用户管理器可能随最后一次登录退出而停止，Nginx返回502。此操作也让该用户管理器在开机时启动；模板只维护网页进程，不会自动启动下载或整理任务。

**macOS LaunchAgent**：模板 `deploy/personal.bili-library.plist.example`。替换 `__NODE_BIN__`、`__PROJECT_DIR__`、`__DATA_DIR__`、`__FFMPEG__`、`__FFPROBE__`，保证资料库已初始化；保存为 `~/Library/LaunchAgents/personal.bili-library.plist` 后：

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/personal.bili-library.plist"
# 停止并取消本次登录会话内的加载
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/personal.bili-library.plist"
```

删除该 plist 可取消以后登录时的自动加载。不要同时用前台、LaunchAgent 和其他进程管理器运行同一个资料库，跨进程锁会拒绝重复后台。

本地整理的 JSON 导出可再次导入，保留分组及标签、备注；导入整理结果按合并处理，不会因为只导出某个分类就隐藏原有其他条目。原 CLI 全量收藏夹快照的导入/同步仍按该收藏夹完整清单更新。


网页历史记录浏览器回归（可选，需开发机已有 Playwright 与 Chrome）：

```sh
BILI_TEST_PYTHON=/你的开发Python路径 node tests/browser_history.cjs
```

使用临时本机资料库验证名称、日志查看/删除、历史删除、重试排序及活动任务保护，不读取生产账号或真实视频。
