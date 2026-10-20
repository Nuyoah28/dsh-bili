# 远程 MCP：Linux / Mac Codex / ChatGPT Work

服务地址：`https://bili.alaner.space/mcp`。同一个 Python 网页进程、同一个 SQLite 和串行控制器；不增加常驻模型、Node 或第二个下载 worker。默认关闭，启用后所有 MCP 工具都需要认证。公网只放行 Nginx 的 HTTPS，后端仍是 `127.0.0.1:8765`。

## 一、更新并启用服务器

先把本次修改提交、推送到自己的 Git。以下命令在原来运行用户级服务的 Linux 用户下执行（你当前部署是 root）：

```bash
cd ~/apps/dsh-bili
git pull --ff-only
mkdir -p ~/.config/systemd/user/bili-library.service.d
printf '[Service]\nEnvironment=BILI_MCP_ENABLED=1\nEnvironment=BILI_WEB_PUBLIC_ORIGIN=https://%s\n' bili.alaner.space > ~/.config/systemd/user/bili-library.service.d/mcp.conf
systemctl --user daemon-reload
systemctl --user restart bili-library
systemctl --user is-active bili-library
```

应显示 `active`。如果已有 `https.conf`，保留它且确保里面的域名相同，不包含 Markdown 方括号或末尾 `/`。
无需新增生产依赖，不需要在1GB服务器安装 MCP SDK；服务运行只使用 Python 标准库和已有 yt-dlp。

原来的 Nginx `location /` 已覆盖 `/mcp`、`/oauth/`、`/.well-known/`，不必新增监听端口。需保留：

```nginx
proxy_pass http://127.0.0.1:8765;
proxy_set_header Host $http_host;
proxy_buffering off;
```

Nginx 默认会转发 Authorization。若你另行配置过清除该头，需恢复转发；不要清除 Origin，不要将 Host 改成 localhost。不要为这些路径添加网页 Cookie 登录拦截。HTTP 80 仍只用于跳转和证书续期；API认证只使用HTTPS。

检查匿名访问（这是预期的401，不是报错）：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "https://"bili.alaner.space/mcp
curl -sS "https://"bili.alaner.space/.well-known/oauth-protected-resource
```

第一次启用会生成 `secrets/mcp-token`，它与 `web-token`、B站 Cookie、模型密钥完全独立。仅在自己的私有终端查看，用来直接访问 MCP 或在网页上批准 OAuth：

```bash
cat /srv/bili-library/secrets/mcp-token
```

不要把令牌发到聊天、Git、URL 或截图。令牌不会写入服务日志或工具返回值。

## 二、用现有视频验收 HTTPS 与图片

无需第三方依赖；下面的检查不会提交下载或修改账号：

```bash
cd ~/apps/dsh-bili
/srv/bili-library/.venv/bin/python tools/mcp-smoke.py --token-file /srv/bili-library/secrets/mcp-token
```

如果之前验收的视频已保存在服务器，可加：

```bash
/srv/bili-library/.venv/bin/python tools/mcp-smoke.py --token-file /srv/bili-library/secrets/mcp-token --video-id BV1iG4y1g7JH_929437359
```

该视频ID仅适用于已归档的对应分P，其他视频用网页资料中的ID。应看到初始化成功、12个工具，以及实际JPEG图片验证成功。浏览器直接 GET `/mcp`，有令牌时返回405属于正常行为：这是支持JSON响应的无状态 Streamable HTTP，不提供长期SSE流。

## 三、Mac Codex：推荐 OAuth

在 Mac 终端执行（需已安装 Codex CLI；没有 CLI 时可在 Codex 的 MCP 设置中添加同一URL并选择OAuth）：

```bash
codex mcp add bili-library --url "https://"bili.alaner.space/mcp
codex mcp login bili-library
codex mcp list
```

弹出的浏览器授权页应位于 **bili.alaner.space**，显示客户端声明名称、精确回调地址与权限。输入独立 `mcp-token`，核对后点授权。不是输入B站Cookie或后台登录令牌。回调地址只接受已登记的精确地址，PKCE仅支持S256。默认支持动态客户端注册 DCR；如果CLI提供注册方式选项，可用：

```bash
codex mcp login bili-library --oauth-client-registration dcr
```

CLI登录成功后重新打开 Codex 中的对话/刷新MCP连接；应用与CLI需使用相同的 Codex 配置目录和账号环境。不要重复添加同名配置。
手动配置可参考 `deploy/codex-mcp.toml.example`，合并进 `~/.codex/config.toml`，不要覆盖其他配置：

```toml
[mcp_servers.bili-library]
url = "https://bili.alaner.space/mcp"
startup_timeout_sec = 20
tool_timeout_sec = 60
```

此配置不需要 OpenAI API key，也不在服务器配置模型key；总结由你正在使用的 Codex 模型完成。
官方配置说明：<https://developers.openai.com/codex/mcp>。

### 可选：Bearer令牌

若不使用 OAuth，可以在同一表中增加 `bearer_token_env_var = "BILI_MCP_TOKEN"`，再在 **Mac zsh终端**安全读取：

```zsh
read -rs 'BILI_MCP_TOKEN?请输入独立 MCP 令牌：'
export BILI_MCP_TOKEN
printf '\n'
codex
```

不要将真实值写入 TOML、shell历史或源码。从 Dock 启动的应用通常不继承终端环境变量，所以桌面端优先用 OAuth。不要同时配置过期Bearer和OAuth，显式Bearer优先，错误的Bearer会导致401。

## 四、ChatGPT / Work

使用同一远程地址与 OAuth，无需将服务器私钥或B站凭证上传给 ChatGPT。
按当前官方入口进入 ChatGPT 的 Plugins，选择加号 → **Add custom MCP server**：

| 项目 | 填写 |
|---|---|
| 名称 | B站资料库 |
| 描述 | 读取个人视频资料库的真实字幕和实际截图，按链接归档单分P |
| 连接 | Public HTTPS，`https://bili.alaner.space/mcp` |
| 认证 | OAuth，动态注册 DCR，公开客户端 `none`，不填写预设client secret |
| 权限范围 | `library`（如有填写框） |

创建后按页面提示连接，在 **bili.alaner.space** 输入独立MCP令牌批准。安装生成的插件，在新 Work 对话中 `@` 选择它。旧版界面可能在设置 → Apps / Connectors → 开发者模式中添加；实际入口和自定义MCP权限以你的账号及工作区策略为准。不能保证每个账号都显示相同菜单，管理员禁用自定义MCP时需要先开放相应权限。

服务公布授权服务器及资源元数据，支持PKCE S256、授权码一次性使用、1小时access token、30天refresh token轮换及撤销。默认允许ChatGPT的 `chatgpt.com` / `chat.openai.com` HTTPS回调，以及Codex的HTTP回环回调；不启用CIMD，不向客户端声明的URL发网络请求。若工作区采用其他回调主机，先从客户端确认精确主机，设置 `BILI_MCP_REDIRECT_HOSTS=经过核实的主机名` 后重启；不要使用通配符放开任意域名。

官方依据：
- 连接与工作区策略：<https://developers.openai.com/plugins/deploy/connect-chatgpt>
- OAuth / DCR / PKCE：<https://developers.openai.com/plugins/build/auth>

## 五、调用与详细课程总结

可以对 Codex / 已选择插件的 Work 说：

> 使用 B站资料库处理 https://b23.tv/Sh6MJ5i ，只处理这个分P。下载完成后读取全部真实字幕，分批查看实际截图，公式看不清时按时间补帧。按定义、适用条件、逐步推导、例题、易错点和复习题详细总结，并引用时间戳。没有字幕时明确告诉我，不推测讲解内容。

工具流程：

1. `bili_submit({"url":"https://b23.tv/Sh6MJ5i"})` → 下载task_id，后台job_id。
2. `bili_tasks({"task_id":"..."})` → 查询，间隔数秒；完成后取video_id。已有资料仍经过缓存检查，返回stage=cached。
3. `bili_subtitles({"video_id":"...","offset":0,"limit":100})` → 继续请求next_offset直到为空。缺字幕明确返回missing/unavailable，不启动云端转写。
4. `bili_frames({"video_id":"...","offset":0,"limit":2})` → 返回时间戳、真实JPEG base64 image内容及next_offset，不仅是路径。
5. `bili_add_frames({"video_id":"...","at":[83,100]})` 或 `{"video_id":"...","start":80,"end":100,"every":5}` → job_id，再查`bili_job`，完成后重新获取截图。
6. 由客户端模型综合材料生成总结，也可选用`course_summary` MCP提示词。

其他工具：`bili_videos`、`bili_video`、`bili_process_queue`、`bili_refresh_subtitles`、`bili_favorites`、`bili_sync_favorites`。
`bili_submit`默认启动**当前已提交队列**；如果只想入队，设置`start:false`。后台忙时只入队并提示，完成当前操作后可调用`bili_process_queue`。同一时刻只有一个下载/补帧/同步操作，不因新客户端连接而并发下载。

MCP不会修改B站收藏、永久删除本地资料或读取/上传凭证。这些操作继续在网页中由用户明确执行。
字幕与截图会作为工具结果发送给所选AI客户端；不会将完整视频上传给模型。20张初始截图不能代表整段视频的全部细节，长课程需要按章节补帧。无字幕时只可描述抽样画面，音频转写仍是后续功能。

## 六、配置、撤销与排错

| 配置 | 默认 | 作用 |
|---|---|---|
| `BILI_MCP_ENABLED` | `0` | 设置1并重启后启用MCP和OAuth |
| `BILI_WEB_PUBLIC_ORIGIN` | 未设置 | 启用MCP必填，HTTPS来源，不含末尾斜杠 |
| `BILI_MCP_REDIRECT_HOSTS` | 未设置 | 逗号分隔的额外精确HTTPS回调主机，默认无需填写 |
| `BILI_LIBRARY_HOME` | 原默认目录 | 服务器设为`/srv/bili-library`，与网页共用 |

访问令牌、OAuth记录和B站凭证均在私有`secrets/`，文件0600，目录0700。OAuth数据库只存访问/刷新令牌和授权码的SHA256，不存B站凭证。客户端登记最多100个，未完成授权页面最多100个，活跃授权最多500个；API连接最多8个，读请求体15秒超时，MCP请求体最多256KiB，每次截图最多4张/6MiB原图，防止1GB服务器无限分配。

停用：把服务drop-in中的 `BILI_MCP_ENABLED=1` 改为0，daemon-reload后重启，MCP路径返回404。网页继续可用。

撤销所有OAuth授权（保留客户端登记、视频、B站登录和独立MCP令牌）：

```bash
systemctl --user stop bili-library
cd ~/apps/dsh-bili
BILI_LIBRARY_HOME=/srv/bili-library /srv/bili-library/.venv/bin/python -m bili_library mcp-revoke
systemctl --user start bili-library
```

如独立MCP令牌泄露，停服后还需在服务器本地轮换`secrets/mcp-token`，不打印新值：

```bash
python3 - <<'PY'
import secrets
from pathlib import Path
p=Path('/srv/bili-library/secrets/mcp-token')
p.write_text(secrets.token_urlsafe(48)+'\n')
p.chmod(0o600)
PY
```

轮换前后都不要把内容粘到聊天；结合上面的OAuth撤销，再重启服务并重新授权客户端。取消客户端连接本身不一定撤销服务器已签发的令牌。

- **404**：未更新代码、未启用MCP或重启失败。
- **401**：匿名请求的预期响应；认证请求出现则检查独立MCP令牌/OAuth是否过期、是否误填web-token。
- **403**：查看JSON中的`code`。`mcp_host_not_allowed` / `oauth_public_host_required`时，检查Nginx保留的Host及公网来源配置；`mcp_origin_not_allowed` / `oauth_consent_origin_required`时，检查是否从新的授权页提交。授权入口GET支持外部客户端导航，但MCP接口与授权提交仍严格检查来源。授权页的Referrer-Policy必须是`same-origin`，不能被Nginx强制改成`no-referrer`；后者会让浏览器的原生表单POST发送`Origin: null`。不要允许任意来源或绕过Cookie/nonce。
- **405**：GET `/mcp` 无SSE流，认证后属正常现象；客户端应使用Streamable HTTP POST。
- **502**：检查用户服务日志 `journalctl --user -u bili-library -n 30 --no-pager`，域名环境变量不得含Markdown链接格式。
- **invalid_redirect_uri**：核对客户端精确回调和登记方式，默认DCR；不要把任意外部域名加入允许名单。
- **access_denied / invalid_request**：查看`code`与`error_description`。`oauth_owner_token_invalid`表示独立MCP令牌不匹配，10分钟内可返回表单更正；`oauth_request_not_found`表示旧页面已用或服务重启，必须由客户端重新发起；`oauth_request_expired`表示超过10分钟；`oauth_cookie_missing` / `oauth_cookie_mismatch`表示浏览器Cookie缺失或不匹配。授权期间不要重启服务，不跨浏览器转移页面。每个授权标签页使用独立Cookie，错误令牌不会消耗该页的一次性凭据；成功授权后仍禁止重放。不要刷新旧POST结果或复制旧完整授权链接重新尝试。
- **temporarily_unavailable**：授权尝试/登记/未完成页面超过上限，停止反复点击，核对客户端配置；异常大量登记时先关闭MCP，停服维护OAuth数据库再重新连接。
- **有下载没字幕**：导入有效B站凭证后调用`bili_refresh_subtitles`。B站自身没有真实字幕时无法补出讲解内容。

开发验证（仅开发机安装SDK，服务器无需）：`python -m pip install -r requirements-mcp-test.txt`，然后`python -m unittest discover -s tests -v`。实现已用官方Python MCP SDK验证Streamable HTTP初始化、工具发现、图片内容和缓存复用；真实ChatGPT工作区授权仍需部署后在你的账号内验收。
