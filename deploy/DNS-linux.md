# Linux Fake-IP 与资料库独立 DNS

如果系统将`api.bilibili.com`解析为`198.18.0.18`等非公网地址，资料库会拒绝连接。该网段常用作代理的Fake-IP映射，但不能据此确定是哪一个DNS/代理进程提供的。无需改变收藏夹ID、关闭公网检查、停止VPN或替换全局DNS。

## 配置与检查

本次代码提交、推送后，在后台空闲时于服务器执行：

```bash
cd ~/apps/dsh-bili
git pull --ff-only
BILI_DNS_MODE=cloudflare /srv/bili-library/.venv/bin/python -m bili_library dns-check
```

结果应显示`mode: cloudflare`、`ok: true`及`selected_addresses`中的公网IPv4。
`system_addresses`可能仍是`198.18.0.18`，这是正常的：此方案不改动全局DNS。

只读检查通过后，在原来运行用户服务的同一Linux用户下配置：

```bash
mkdir -p ~/.config/systemd/user/bili-library.service.d
printf '[Service]\nEnvironment=BILI_DNS_MODE=cloudflare\n' > ~/.config/systemd/user/bili-library.service.d/dns.conf
systemctl --user daemon-reload
systemctl --user restart bili-library
systemctl --user is-active bili-library
systemctl --user show bili-library -p Environment --value | tr ' ' '\n' | grep '^BILI_DNS_MODE='
```

应分别显示`active`和`BILI_DNS_MODE=cloudflare`。
保留原来的`https.conf`、`mcp.conf`等配置。网页中重新同步收藏夹；或者在已连接的Codex里请求同步指定ID。失败的旧任务不会自动重试。
CLI不自动继承systemd服务环境；手动运行CLI时需同样设置`BILI_DNS_MODE=cloudflare`。

## 网络与资源边界

- 默认`system`，显式`cloudflare`才使用DoH；其他配置值报错，不静默切换。
- 只访问固定引导IP`1.1.1.1` / `1.0.0.1`的443端口，以`cloudflare-dns.com`校验TLS证书；不依赖系统DNS解析引导域名。
- 使用Cloudflare DNS JSON接口查询A记录；只发送待解析域名，不发送B站Cookie、MCP令牌、视频完整链接或模型密钥。
- 仅解析已有B站/CDN允许列表内的域名。校验DNS问题与响应、CNAME链及所有适用A记录，混入内网、回环、链路本地、Fake-IP等地址时整次拒绝。
- 最终HTTPS连接固定到已校验IP，TLS仍校验原B站域名。API、字幕、视频/CDN和yt-dlp内部代理共用同一边界。
- 不跟随DoH重定向、不继承HTTP/SOCKS代理变量；失败不回退到系统Fake-IP。现有系统VPN/TUN路由保持原样。
- 仅IPv4；没有可用A记录时明确失败。DNS缓存遵循TTL、最长300秒，最多256个域名；0 TTL与失败结果不缓存。
- 每次引导连接/读操作超时5秒，响应上限64KiB；不增加运行依赖或常驻服务。

如果检查提示DoH请求失败，需要服务器能够通过HTTPS连接至少一个固定引导IP。不要放开对`198.18.*`的下载权限。另一种可选修复是在你实际使用的DNS/代理软件中让B站相关域名返回真实公网解析；具体语法需核对软件和模式，避免影响现有服务。

## 回退

如需恢复系统解析，在`dns.conf`中将`cloudflare`改为`system`，然后daemon-reload并重启。全局DNS和VPN从未被修改，无需恢复。

## 验证记录

开发机已完成真实Cloudflare DoH查询及只读收藏夹API请求；生产服务器能否连通仍需以上`dns-check`和同步操作验收。单元测试覆盖Fake-IP/混合地址拒绝、固定TLS引导、不发送Cookie、不使用代理或系统回退、CNAME、TTL及缓存上限。

接口依据：[Cloudflare DoH JSON文档](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)。
