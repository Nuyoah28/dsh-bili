"""HTTPS-only network boundary. Resolve once, reject non-public addresses, pin TCP destination.

A short-lived authenticated loopback CONNECT proxy applies the SAME policy to yt-dlp.
No system proxy settings are changed, no remote FFmpeg input is allowed.
"""
from __future__ import annotations

import base64
import contextlib
import http.client
import ipaddress
import json
import re
import secrets
import select
import socket
import socketserver
import ssl
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .common import LibraryError

BV = r'BV[0-9A-Za-z]{10}'
INPUT_HOSTS = {'bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv', 'www.b23.tv'}
NETWORK_ROOTS = ('bilibili.com', 'b23.tv', 'bilivideo.com', 'bilivideo.cn', 'hdslb.com')
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0 Safari/537.36'


def validate_url(url, input_only=False):
    try:
        u = urlsplit(url)
        host = (u.hostname or '').lower()
        port = u.port
    except ValueError:
        raise LibraryError('链接格式无效') from None
    hosts_ok = host in INPUT_HOSTS if input_only else any(host == r or host.endswith('.'+r) for r in NETWORK_ROOTS)
    if (u.scheme != 'https' or not hosts_ok or port not in (None, 443)
            or u.username is not None or u.password is not None or '\\' in url
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise LibraryError('仅允许受支持的 B站 HTTPS 地址')
    return u


def video_input(value):
    value = value.strip()
    if re.fullmatch(BV, value):
        value = 'https://www.bilibili.com/video/' + value
    u = validate_url(value, input_only=True)
    if u.hostname in ('b23.tv', 'www.b23.tv'):
        if not re.fullmatch(r'/[A-Za-z0-9]+/?', u.path):
            raise LibraryError('短链接格式无效')
        return 'https://b23.tv' + u.path
    match = re.fullmatch('/video/('+BV+r')/?', u.path)
    if not match:
        raise LibraryError('请提供 BV 视频链接；暂不接受番剧、合集或直播链接')
    values = parse_qs(u.query, keep_blank_values=True).get('p', ['1'])
    if len(values) != 1 or not re.fullmatch(r'[1-9][0-9]*', values[0]):
        raise LibraryError('分P序号必须是正整数且只能指定一次')
    return f'https://www.bilibili.com/video/{match[1]}?p={int(values[0])}'


def public_addresses(host, resolver=None):
    validate_url('https://' + host)
    if resolver is None:
        from .dns_resolver import mode,resolve
        resolver=resolve if mode()=='cloudflare' else socket.getaddrinfo
    try:
        entries = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise LibraryError('B站域名解析失败') from None
    if not entries:
        raise LibraryError('B站域名没有可用地址')
    for _, _, _, _, address in entries:
        ip = ipaddress.ip_address(address[0].split('%')[0])
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or (getattr(ip, 'ipv4_mapped', None) and not ip.ipv4_mapped.is_global)):
            raise LibraryError('网络安全策略拒绝非公网目标地址（'+host+'；请检查DNS、hosts或代理Fake-IP配置）')
    return entries


def connect_public(host):
    for family, socktype, proto, _, address in public_addresses(host):
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(15)
        try:
            sock.connect(address)  # connect the validated address, never resolve a second time
            return sock
        except OSError:
            sock.close()
    raise LibraryError('无法连接 B站服务')


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        raw = connect_public(self.host)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class Network:
    def __init__(self, cookies=None):
        self.cookies = cookies or {}

    def get(self, url, limit=8*1024**2, redirects=5):
        for _ in range(redirects + 1):
            u = validate_url(url)
            headers = {'User-Agent': UA, 'Referer': 'https://www.bilibili.com/'}
            # Never send account cookies to CDN or short-link hosts.
            if u.hostname == 'api.bilibili.com' or u.hostname == 'www.bilibili.com':
                headers['Cookie'] = '; '.join(k+'='+v for k, v in self.cookies.items())
            conn = PinnedHTTPS(u.hostname, timeout=15, context=ssl.create_default_context())
            try:
                conn.request('GET', (u.path or '/') + ('?'+u.query if u.query else ''), headers=headers)
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.getheader('Location') or '')
                    validate_url(url)
                    continue
                if response.status != 200:
                    raise LibraryError(f'B站请求失败（HTTP {response.status}）')
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise LibraryError('B站响应超过大小上限')
                return body, url
            except (OSError, http.client.HTTPException):
                raise LibraryError('B站网络请求失败或超时') from None
            finally:
                conn.close()
        raise LibraryError('链接重定向次数超限')

    def json(self, url):
        try:
            return json.loads(self.get(url)[0])
        except (ValueError, UnicodeError):
            raise LibraryError('B站返回的不是有效 JSON') from None

    def api(self, path, **params):
        payload = self.json('https://api.bilibili.com' + path + '?' + urlencode(params))
        if payload.get('code') != 0:
            # No untrusted server message, signed URL or cookie can reach logs/results.
            raise LibraryError(f'B站 API 失败（code={int(payload.get("code") or -1)}，可能需要登录或稍后重试）')
        return payload.get('data') or {}

    def resolve(self, source):
        source = video_input(source)
        if urlsplit(source).hostname == 'b23.tv':
            _, target = self.get(source, limit=2*1024**2)
            return video_input(target)
        return source


    def post_favorite(self, path, params):
        # Explicit capability, not a generic URL/body forwarding endpoint.
        if path not in ('/x/v3/fav/resource/move','/x/v3/fav/resource/batch-del'):
            raise LibraryError('未允许的 B站写操作')
        if not self.cookies.get('SESSDATA') or not self.cookies.get('bili_jct'):
            raise LibraryError('此操作需要有效的 B站登录及 CSRF 凭证')
        body=urlencode({**params,'csrf':self.cookies['bili_jct']}).encode('utf-8')
        headers={'User-Agent':UA,'Referer':'https://www.bilibili.com/',
                 'Content-Type':'application/x-www-form-urlencoded',
                 'Cookie':'; '.join(k+'='+v for k,v in self.cookies.items())}
        conn=PinnedHTTPS('api.bilibili.com',timeout=15,context=ssl.create_default_context())
        try:
            conn.request('POST',path,body=body,headers=headers)
            res=conn.getresponse()
            if res.status!=200: raise LibraryError('B站写操作未确认成功，请同步收藏夹核对后再决定是否重试')
            raw=res.read(1024*1024+1)
            if len(raw)>1024*1024: raise LibraryError('B站写操作响应过大，结果未确认')
            payload=json.loads(raw)
            if payload.get('code')!=0:
                raise LibraryError('B站拒绝写操作（code='+str(int(payload.get('code') or -1))+'）；请同步收藏夹核对')
            return {'accepted':True}
        except (OSError,http.client.HTTPException,ValueError):
            raise LibraryError('B站写操作结果不确定；不会自动重试，请同步源和目标收藏夹核对') from None
        finally:
            conn.close()


class GuardProxy:
    """Internal transport helper only; authenticated, loopback, lifetime of one task."""
    def __enter__(self):
        self.token = secrets.token_urlsafe(32)
        expected = 'Basic ' + base64.b64encode(('bili:'+self.token).encode()).decode()
        slots = threading.BoundedSemaphore(4)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                if not slots.acquire(blocking=False):
                    return
                try:
                    self.connection.settimeout(15)
                    first = self.rfile.readline(4097).decode('ascii', 'replace').strip().split()
                    if len(first) != 3 or first[0] != 'CONNECT' or not first[1].endswith(':443'):
                        return
                    host = first[1][:-4]
                    headers = {}
                    for _ in range(40):
                        line = self.rfile.readline(8193)
                        if line == b'\r\n':
                            break
                        if len(line) > 8192:
                            return
                        k, _, v = line.decode('ascii', 'replace').partition(':')
                        headers[k.lower()] = v.strip()
                    if not secrets.compare_digest(headers.get('proxy-authorization', ''), expected):
                        self.wfile.write(b'HTTP/1.1 407 Proxy Authentication Required\r\n\r\n')
                        return
                    upstream = connect_public(host)
                    with upstream:
                        self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
                        self.wfile.flush()
                        deadline = time.monotonic() + 1800
                        while time.monotonic() < deadline:
                            ready, _, _ = select.select([self.connection, upstream], [], [], 15)
                            if not ready:
                                return
                            for src in ready:
                                data = src.recv(65536)
                                if not data:
                                    return
                                (upstream if src is self.connection else self.connection).sendall(data)
                except Exception:
                    pass  # Never log remote hosts/URLs/credentials, including socketserver tracebacks.
                finally:
                    slots.release()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://bili:{self.token}@127.0.0.1:{self.server.server_address[1]}'
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

