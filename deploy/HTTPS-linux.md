# Linux HTTPS 部署（Ubuntu / Debian，现有用户级 systemd 服务）

示例域名 `bili.alaner.space`，后端继续监听 `127.0.0.1:8765`。
先将本次项目修改推送至自己的 Git，再在服务器项目目录 `git pull --ff-only`。

## 1. DNS 与端口

新增 A 记录 `bili` 指向服务器公网 IPv4。不要改邮箱的 MX / SPF / DKIM / DMARC 记录。
没有配置 IPv6 时不要新增 AAAA。Cloudflare 用户先使用“仅 DNS”。
在云安全组及已有防火墙中允许 TCP 80、443，不开放 8765。
先查看是否有其他需要保留的监听：

```bash
sudo ss -lntup | grep -E ':(80|443|8765)\b'
```

用户已同意关闭 Xray 时，先确认服务名称 `systemctl status xray`。
确保有直接 SSH 或云控制台连接，然后停止它（会断开其代理连接）：

```bash
sudo systemctl stop xray
```

不要卸载或删除 Xray 配置。网页验收成功后再 `sudo systemctl disable xray`，防止重启抢占 443。

## 2. 项目允许的 HTTPS 来源

此部署使用用户级systemd服务。若要求退出SSH及开机后继续运行，在原来运行资料库的Linux用户下执行一次：

```bash
sudo loginctl enable-linger "$(id -un)"
systemctl --user enable --now bili-library
loginctl show-user "$(id -un)" -p Linger
```

应显示`Linger=yes`。配置完成后退出所有SSH连接，等待15秒，再从Mac直接访问HTTPS域名，确认仍可用；不需要SSH隧道。若仍502，检查用户服务日志及Nginx错误日志，不要因重新登录后服务自动恢复就认定问题已消失。

在原本运行资料库的同一 Linux 用户下执行（用户级服务不属于其他用户）：

```bash
systemctl --user edit bili-library
```

添加：

```ini
[Service]
Environment=BILI_WEB_PUBLIC_ORIGIN=https://bili.alaner.space
```

```bash
systemctl --user daemon-reload
systemctl --user restart bili-library
systemctl --user status bili-library --no-pager
```

该配置不含末尾 `/`。保留令牌认证、CSRF 和严格 Host/Origin 检查；公网域名登录 Cookie 使用 Secure。
不用信任客户端提交的 X-Forwarded-* 头。反向代理需保留原始 Host。

## 3. 安装 Nginx 和申请证书

```bash
sudo apt update
sudo apt install -y nginx certbot
sudo mkdir -p /var/www/bili-acme
sudo tee /etc/nginx/sites-available/bili-library >/dev/null <<'NGINX'
server {
    listen 80;
    server_name bili.alaner.space;
    access_log off;
    location /.well-known/acme-challenge/ {
        root /var/www/bili-acme;
    }
    location / { return 404; }
}
NGINX
sudo ln -s /etc/nginx/sites-available/bili-library /etc/nginx/sites-enabled/bili-library
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/bili-acme -d bili.alaner.space
```

如果链接已存在，不必重复 `ln -s`。证书命令会询问通知邮箱和服务条款；只有成功签发后才继续。

## 4. 启用 HTTPS 反代

```bash
sudo tee /etc/nginx/sites-available/bili-library >/dev/null <<'NGINX'
server {
    listen 80;
    server_name bili.alaner.space;
    access_log off;
    location /.well-known/acme-challenge/ {
        root /var/www/bili-acme;
    }
    location / { return 301 https://bili.alaner.space$request_uri; }
}
server {
    listen 443 ssl;
    server_name bili.alaner.space;
    ssl_certificate /etc/letsencrypt/live/bili.alaner.space/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bili.alaner.space/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    client_max_body_size 8m;
    client_body_timeout 15s;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $http_host;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 90s;
    }
}
NGINX
sudo nginx -t
sudo systemctl reload nginx
```

不修改其他 Nginx 站点；不删除 Origin，不将 Host 伪装成 localhost。

## 5. 验收、续期与回退

在任意设备访问 `https://bili.alaner.space`，输入原资料库的后台令牌。
令牌仅在服务器本机读取，不贴到聊天、URL 或 Git：

```bash
cat /srv/bili-library/secrets/web-token
curl -sS -o /dev/null -w '%{http_code}\n' https://bili.alaner.space/
sudo ss -lntp | grep -E ':(80|443|8765)\b'
```

页面检测应返回 200。
再检查登录、截图、视频拖动播放和收藏夹列表；网页任务仍由用户手动启动。

设置证书更新后重载 Nginx：

```bash
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/bili-nginx >/dev/null <<'SH'
#!/bin/sh
/usr/sbin/nginx -t && /bin/systemctl reload nginx
SH
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/bili-nginx
sudo systemctl enable --now certbot.timer
sudo certbot renew --dry-run
```

80 端口需继续开放用于 HTTP-01 自动续期。Certbot webroot 方式不接管 Xray 配置。
验收成功且确定不再使用 Xray 后：

```bash
sudo systemctl disable xray
```

如要恢复 Xray，先移除这个站点的启用链接并重载 Nginx，释放 443，再恢复 Xray：

```bash
sudo rm /etc/nginx/sites-enabled/bili-library
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl enable --now xray
```

回退后仍可通过 SSH 隧道访问后台。
