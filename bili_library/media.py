from __future__ import annotations

import http.cookiejar
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .common import LibraryError, number, read_json, write_json
from .network import GuardProxy, UA, validate_url


def binary(name):
    override = os.environ.get('BILI_' + name.upper())
    path = override or shutil.which(name)
    if not path or not Path(path).is_file():
        raise LibraryError(name + ' 不可用；请运行 doctor 并配置独立工具路径')
    return str(Path(path).resolve())


def run_media(args, timeout=120):
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=timeout, text=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise LibraryError('本地媒体处理失败或超时；请检查 FFmpeg/ffprobe 和媒体文件') from None


def probe(path):
    result = run_media([binary('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file,crypto,data',
                        '-show_entries', 'format=duration:stream=codec_type,height', '-of', 'json', str(path)])
    try:
        data = json.loads(result.stdout)
        duration = number(data['format']['duration'], '媒体时长', 0.001)
        video = next(s for s in data['streams'] if s.get('codec_type') == 'video')
        return {'duration': duration, 'height': int(video['height'])}
    except (ValueError, KeyError, StopIteration, TypeError):
        raise LibraryError('下载文件不是可校验的视频') from None


def add_cookies(ydl, cookies):
    for name, value in cookies.items():
        ydl.cookiejar.set_cookie(http.cookiejar.Cookie(
            0, name, value, None, False, '.bilibili.com', True, True, '/', True,
            True, None, True, None, None, {}, False))


def download(library, meta, work, cookies):
    from .diagnostics import DownloadLogger, record, MESSAGES
    task_id = work.name if library.rows("SELECT id FROM tasks WHERE id=?", (work.name,)) else None
    logger = DownloadLogger(library, task_id)
    try:
        import yt_dlp
    except ImportError:
        raise LibraryError('缺少 yt-dlp；请先运行 node tools/library.mjs setup') from None
    binary('ffmpeg')
    binary('ffprobe')
    record(library, task_id, 'extract', cookie_present=bool(cookies.get('SESSDATA')),
           max_bytes=library.config['max_download_bytes'], timeout_seconds=library.config['task_timeout_seconds'],
           yt_dlp_version=getattr(getattr(yt_dlp, 'version', None), '__version__', ''))
    sizes = {}
    started = time.monotonic()
    def hook(progress):
        if time.monotonic() - started > library.config['task_timeout_seconds']:
            raise LibraryError('下载任务超时')
        sizes[progress.get('filename', '')] = int(progress.get('downloaded_bytes') or 0)
        if sum(sizes.values()) > library.config['max_download_bytes']:
            raise LibraryError('下载音视频合计超过大小上限')
        library.check_space()
    with GuardProxy() as guard:
        opts = dict(proxy=guard.url, geo_verification_proxy=guard.url, noplaylist=True,
                    format='bestvideo[height<=720]+bestaudio/best[height<=720]',
                    outtmpl=str(work / 'video.%(ext)s'), merge_output_format='mkv',
                    ffmpeg_location=binary('ffmpeg'),
                    postprocessor_args={'ffmpeg': ['-threads', '1']},
                    concurrent_fragment_downloads=1, socket_timeout=15,
                    retries=2, fragment_retries=2, file_access_retries=1,
                    ratelimit=library.config['download_rate_bytes'],
                    max_filesize=library.config['max_download_bytes'],
                    progress_hooks=[hook], logger=logger, quiet=True, noprogress=True,
                    writesubtitles=False, writeautomaticsub=False, writethumbnail=False,
                    getcomments=False, cachedir=False, enable_file_urls=False,
                    hls_prefer_native=True, fixup='never',
                    http_headers={'User-Agent': UA, 'Referer': 'https://www.bilibili.com/'})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                add_cookies(ydl, cookies)
                info = ydl.extract_info(meta['source'], download=False)
                expected = {meta['bvid'] + f'_p{meta["page"]}'}
                if meta['page'] == 1:
                    expected.add(meta['bvid'])
                if not info or info.get('_type', 'video') != 'video' or info.get('id') not in expected:
                    raise LibraryError('下载器返回了合集或错误分P，已拒绝下载')
                streams = info.get('requested_formats') or [info]
                estimate = sum(int(s.get('filesize') or s.get('filesize_approx') or 0) for s in streams)
                if estimate > library.config['max_download_bytes']:
                    raise LibraryError('预计下载大小超过上限')
                library.check_space(max(estimate*2, 32*1024**2))
                for stream in streams:
                    # Only native HTTP downloads. Never let FFmpeg access a network URL.
                    url = stream.get('url', '')
                    if url.startswith('http://'):
                        url = 'https://' + url[7:]
                        stream['url'] = url
                    validate_url(url)
                    if stream.get('protocol') not in ('https', 'http', 'http_dash_segments'):
                        raise LibraryError('暂不支持需要外部网络下载器的媒体格式')
                    if stream.get('vcodec') != 'none' and int(stream.get('height') or 100000) > 720:
                        raise LibraryError('没有可用的 720p 以下格式')
                    for fragment in stream.get('fragments') or []:
                        f_url = fragment.get('url')
                        if not f_url:
                            raise LibraryError('暂不支持带相对片段地址的媒体')
                        if f_url.startswith('http://'):
                            fragment['url'] = 'https://' + f_url[7:]
                        validate_url(fragment['url'])
                record(library, task_id, 'formats', streams=len(streams), estimated_bytes=estimate)
                record(library, task_id, 'transfer')
                ydl.process_info(info)
        except LibraryError:
            raise
        except Exception as error:
            logger.capture(error, 'error')
            code = getattr(guard, 'failure_code', None) or logger.code or 'unknown'
            record(library, task_id, code, 'error')
            raise LibraryError('yt-dlp 下载失败 ['+code+']：'+MESSAGES[code]) from None
    files = [p for p in work.glob('video.*') if p.suffix in ('.mp4', '.mkv', '.webm', '.flv')]
    if len(files) != 1:
        raise LibraryError('下载器没有产出唯一完整视频文件')
    path = files[0]
    if path.stat().st_size > library.config['max_download_bytes']:
        raise LibraryError('下载文件超过大小上限')
    record(library, task_id, 'verify')
    checked = probe(path)
    if checked['height'] > 720 or checked['duration'] > library.config['max_duration_seconds']:
        raise LibraryError('实际视频分辨率或时长超过限制')
    if abs(checked['duration']-meta['duration']) > max(5, meta['duration']*0.05):
        raise LibraryError('下载视频时长与所选分P不符，可能只有预览片段')
    return path, checked


def initial_times(duration, count):
    # Interior samples avoid the common blank first and end frame. Tiny videos may deduplicate.
    return sorted({round(duration*(i+0.5)/count, 3) for i in range(count) if duration*(i+0.5)/count < duration})


def frames(library, video_dir, media, times, duration):
    frame_dir = video_dir / 'frames'
    frame_dir.mkdir(exist_ok=True, mode=0o700)
    result = [item for item in read_json(frame_dir / 'index.json', [])
              if (video_dir / item['file']).is_file()]
    for requested in times:
        requested = number(requested, '截图时间')
        if requested >= duration:
            raise LibraryError('截图时间必须小于视频时长')
    deadline = time.monotonic() + library.config['task_timeout_seconds']
    for requested in times:
        existing = next((item for item in result if abs(item['requested']-requested) < 0.001
                         and (video_dir/item['file']).is_file()), None)
        if existing:
            continue
        library.check_space(4*1024**2)
        # Fast input seek; copyts+showinfo retains the actual presentation timestamp.
        name = f'{round(requested*1000000):016d}.jpg'
        target = frame_dir / name
        tmp = frame_dir / (name + '.tmp.jpg')
        try:
            call = run_media([binary('ffmpeg'), '-hide_banner', '-loglevel', 'info', '-nostdin', '-y',
                              '-threads', '1', '-filter_threads', '1', '-protocol_whitelist', 'file,crypto,data',
                              '-copyts', '-ss', str(requested), '-i', str(media),
                              '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', 'showinfo,scale=min(1280\\,iw):-2',
                              '-frames:v', '1', '-q:v', '3', '-threads', '1', str(tmp)],
                             timeout=max(1, min(120, deadline-time.monotonic())))
            matches = re.findall(r'pts_time:\s*([0-9.eE+-]+)', call.stderr)
            if not matches or not tmp.is_file() or not tmp.stat().st_size:
                raise LibraryError('指定时间没有可解码的画面')
            # showinfo can inspect more than one frame, but the first is the one written.
            actual = number(matches[0], '实际画面时间')
            tmp.replace(target)
            result.append(dict(requested=requested, timestamp=actual,
                               file='frames/'+name, mime_type='image/jpeg'))
            result.sort(key=lambda item: item['timestamp'])
            write_json(frame_dir / 'index.json', result)
        finally:
            tmp.unlink(missing_ok=True)
    return result
