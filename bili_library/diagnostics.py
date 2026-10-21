"""Bounded structured diagnostics. Never store arbitrary downloader output."""
from __future__ import annotations

import json
import re
import time

# Only these locally authored messages may cross the persistence boundary.
MESSAGES = {
    'starting': '开始本次下载处理',
    'metadata': '读取所选分P信息',
    'download': '下载所选视频，最高720p，串行处理',
    'extract': 'yt-dlp正在提取媒体格式',
    'formats': '媒体格式检查通过，准备下载',
    'transfer': '音视频传输与本地合并',
    'verify': '校验下载文件的分辨率、时长与完整性',
    'subtitles': '读取真实字幕',
    'frames': '提取初始画面',
    'completed': '视频及资料归档完成',
    'cached': '复用已有完整视频与资料，无需重复下载',
    'cancelled': '本次处理已取消',
    'interrupted': '上次处理被中断，需手动重新排队',
    'http_401': 'HTTP 401：服务要求有效登录，请重新检查B站凭证',
    'http_403': 'HTTP 403：服务拒绝访问，可能是媒体权限、签名过期或风控',
    'http_404': 'HTTP 404：视频或媒体资源不存在',
    'http_412': 'HTTP 412：请求被服务端风控拒绝',
    'http_429': 'HTTP 429：请求过于频繁，请稍后手动重试',
    'http_5xx': 'HTTP 5xx：远端服务异常',
    'login': '视频需要登录、会员或其他账号权限；登录成功不代表具有该视频权限',
    'format': '没有符合要求的可用媒体格式（最高720p）；检查视频权限或yt-dlp版本',
    'non_public': '网络安全策略拒绝非公网地址；检查Fake-IP/DNS，保留安全检查',
    'domain': '媒体地址不在允许的B站HTTPS域名范围，已拒绝访问',
    'dns': '域名解析失败；检查资料库DNS配置与网络',
    'tls': 'TLS证书验证或握手失败；检查系统时间、证书与网络',
    'timeout': '任务或网络请求超时；检查网络、下载限速和任务超时设置',
    'connection': '网络连接失败或被中断；检查服务器到B站的连通性',
    'size': '下载大小超过单任务上限',
    'space': '可用磁盘空间或资料库配额不足',
    'duration': '视频时长超过配置上限',
    'ffmpeg': 'FFmpeg/ffprobe缺失或媒体处理失败；检查工具路径和文件完整性',
    'permission': '本地文件读写权限不足',
    'unknown': '未识别的下载错误；检查依赖版本及上述处理阶段，原始输出因可能含凭证未保存',
}


def classify(error):
    """Inspect in memory only; return a fixed code, never any input text."""
    text = str(error).lower()[:32768]
    for code in (401, 403, 404, 412, 429):
        if re.search(r'\b(?:http(?:\s+error)?|status(?:\s+code)?)\s*[:=]?\s*'+str(code)+r'\b', text):
            return 'http_'+str(code)
    if re.search(r'\b(?:http(?:\s+error)?|status(?:\s+code)?)\s*[:=]?\s*5\d\d\b', text):
        return 'http_5xx'
    checks = [
        ('non_public', ('非公网', 'fake-ip', 'fakeip')),
        ('domain', ('仅允许受支持', 'not allowed', '域名范围')),
        ('dns', ('解析', 'name resolution', 'getaddrinfo', 'name or service not known', 'nodename nor servname')),
        ('tls', ('certificate', 'ssl', 'tls')),
        ('size', ('大小超过', '超过大小', 'filesize', 'max-filesize')),
        ('space', ('磁盘', '配额', 'no space left')),
        ('duration', ('时长限制', '时长超过')),
        ('ffmpeg', ('ffmpeg', 'ffprobe', 'postprocessing', '媒体处理', '媒体文件')),
        ('format', ('requested format', 'no video formats', '没有可用', '媒体格式')),
        ('login', ('login required', 'log in', 'sign in', 'premium', 'members-only', '会员', '登录已失效')),
        ('timeout', ('timed out', 'timeout', '超时')),
        ('permission', ('permission denied', 'access is denied')),
        ('connection', ('connection', 'network is unreachable', '无法连接', 'broken pipe')),
    ]
    return next((code for code, words in checks if any(w in text for w in words)), 'unknown')


def record(lib, task_id, code, level='info', **details):
    if not task_id:
        return
    code = code if code in MESSAGES else 'unknown'
    safe = {}
    for key in ('height', 'streams', 'estimated_bytes', 'max_bytes', 'timeout_seconds', 'cookie_present'):
        value = details.get(key)
        if isinstance(value, (int, bool)) and abs(value) <= 2**63-1:
            safe[key] = value
    version = details.get('yt_dlp_version')
    if isinstance(version, str) and re.fullmatch(r'[0-9.]{1,32}', version):
        safe['yt_dlp_version'] = version
    with lib.db:
        lib.db.execute('INSERT INTO task_logs(task_id,created,level,code,message,details) VALUES(?,?,?,?,?,?)',
                       (task_id, time.time(), level if level in ('info','warning','error') else 'info',
                        code, MESSAGES[code], json.dumps(safe)))
        lib.db.execute('DELETE FROM task_logs WHERE task_id=? AND id NOT IN '
                       '(SELECT id FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT 200)', (task_id,task_id))


class DownloadLogger:
    def __init__(self, lib, task_id):
        self.lib, self.task_id, self.code = lib, task_id, None
        self.seen = set()

    def debug(self, *args, **kwargs): pass
    def info(self, *args, **kwargs): pass

    def capture(self, message, level):
        code = classify(message)
        # Prefer a specific diagnosis over an unclassified wrapper exception.
        if code != 'unknown' or self.code is None:
            self.code = code
        if code not in self.seen:
            record(self.lib, self.task_id, code, level)
            self.seen.add(code)

    def warning(self, message, **kwargs): self.capture(message, 'warning')
    def error(self, message, **kwargs): self.capture(message, 'error')
