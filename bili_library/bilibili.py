from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlsplit

from .common import LibraryError, read_json, write_json, number
from .network import BV, INPUT_HOSTS, Network, validate_url

COOKIE_FIELDS = {'sessdata': 'SESSDATA', 'bili_jct': 'bili_jct', 'buvid3': 'buvid3', 'dedeuserid': 'DedeUserID'}


def load_cookies(library):
    path = library.home / 'secrets' / 'bilibili.json'
    # Reuse an existing DSH QR login without copying it or returning its contents.
    if not path.exists():
        path = library.home.parent / 'bili_credential.json'
    data = read_json(path, {})
    cookies = {}
    for field, name in COOKIE_FIELDS.items():
        value = str(data.get(field) or '')
        if any(c in value for c in ('\r', '\n', ';')):
            raise LibraryError('B站凭证格式无效')
        if value:
            cookies[name] = value
    return cookies


def import_credentials(library, path):
    data = read_json(path, {})
    result = {k: str(data.get(k) or '') for k in COOKIE_FIELDS}
    if not result['sessdata'] or any(any(c in v for c in ('\r', '\n', ';')) for v in result.values()):
        raise LibraryError('凭证应为现有扫码登录生成的四字段 JSON')
    write_json(library.home / 'secrets' / 'bilibili.json', result)
    return {'ok': True, 'message': 'B站凭证已独立保存，未返回凭证内容'}


def metadata(network, source):
    source = network.resolve(source)
    u = urlsplit(source)
    bvid = u.path.rstrip('/').split('/')[-1]
    page = int(parse_qs(u.query)['p'][0])
    info = network.api('/x/web-interface/view', bvid=bvid)
    if info.get('rights', {}).get('is_stein_gate'):
        raise LibraryError('暂不支持互动视频的分支归档')
    pages = info.get('pages') or []
    chosen = next((p for p in pages if p.get('page') == page), None)
    if not chosen or not chosen.get('cid'):
        raise LibraryError('指定分P不存在')
    return dict(id=f'{bvid}_{int(chosen["cid"])}', bvid=bvid, cid=int(chosen['cid']),
                page=page, title=str(info.get('title') or ''), part_title=str(chosen.get('part') or ''),
                source=source, duration=number(chosen.get('duration'), '视频时长', 0.001))


def normalize_subtitles(body):
    result = []
    for item in body:
        start = number(item.get('from'), '字幕开始时间')
        end = number(item.get('to'), '字幕结束时间')
        text = str(item.get('content') or '').strip()
        if end < start:
            raise LibraryError('字幕时间范围无效')
        if text:
            result.append(dict(start=start, end=end, text=text))
    return sorted(result, key=lambda item: item['start'])


def subtitles(network, meta):
    base = dict(bvid=meta['bvid'], cid=meta['cid'], status='missing', source=None,
                language=None, segments=[], reason='没有可获取的真实字幕；云端转写未配置')
    try:
        data = network.api('/x/player/wbi/v2', bvid=meta['bvid'], cid=meta['cid'])
        tracks = [s for s in data.get('subtitle', {}).get('subtitles', [])
                  if s.get('subtitle_url') and s.get('lan') not in ('danmaku', 'xml')]
        def rank(track):
            lang = track.get('lan', '')
            return (lang.startswith('ai-'), lang not in ('zh', 'zh-CN', 'ai-zh'), lang)
        for track in sorted(tracks, key=rank):
            url = track['subtitle_url']
            if url.startswith('//'):
                url = 'https:' + url
            validate_url(url)
            segments = normalize_subtitles(network.json(url).get('body') or [])
            if segments:
                return {**base, 'status': 'available', 'source': 'platform_ai' if track.get('lan', '').startswith('ai-') else 'platform_manual',
                        'language': track.get('lan'), 'segments': segments, 'reason': None}
        if data.get('need_login_subtitle'):
            base['reason'] = '字幕需要登录；请导入凭证后运行 subtitles <视频ID> --refresh'
    except LibraryError as exc:
        base['status'] = 'unavailable'
        base['reason'] = '字幕获取失败：' + str(exc)
    return base


def folder_id(value):
    if re.fullmatch(r'[1-9][0-9]*', value):
        return int(value)
    u = validate_url(value)
    if u.hostname != 'space.bilibili.com' or not re.fullmatch(r'/[0-9]+/favlist/?', u.path):
        raise LibraryError('请输入收藏夹 ID 或 space.bilibili.com 的收藏夹链接')
    values = parse_qs(u.query).get('fid', [])
    if len(values) != 1 or not re.fullmatch(r'[1-9][0-9]*', values[0]):
        raise LibraryError('收藏夹链接缺少有效 fid')
    return int(values[0])


def folders(network, uid=None):
    uid = uid or network.cookies.get('DedeUserID')
    if not uid or not re.fullmatch(r'[1-9][0-9]*', str(uid)):
        raise LibraryError('请提供 --uid 或先导入带用户 UID 的 B站凭证')
    data = network.api('/x/v3/fav/folder/created/list-all', up_mid=uid)
    return {'folders': [dict(id=int(it['id']), title=str(it.get('title') or ''),
                             count=int(it.get('media_count') or 0)) for it in data.get('list') or []]}


def export_favorites(network, fid):
    """Read-only paginated inventory; never submit download tasks or call mutation APIs."""
    fid = folder_id(str(fid))
    result, seen, page_signatures = [], set(), set()
    deadline = time.monotonic() + 300
    expected = 0
    title = ''
    for page in range(1, 1001):
        if time.monotonic() > deadline:
            raise LibraryError('收藏夹导出超时；未生成不完整导出，请稍后重试')
        data = network.api('/x/v3/fav/resource/list', media_id=fid, pn=page, ps=20,
                           order='mtime', type=0, tid=0, platform='web')
        info = data.get('info') or {}
        title = str(info.get('title') or title)
        expected = int(info.get('media_count') or expected)
        items = data.get('medias') or []
        signature = tuple((it.get('id'), it.get('bvid')) for it in items)
        if items and signature in page_signatures:
            raise LibraryError('收藏夹接口重复分页；未生成不完整导出')
        page_signatures.add(signature)
        for it in items:
            key = (it.get('type', 2), it.get('id'), it.get('bvid'))
            if key in seen:
                continue
            seen.add(key)
            bvid = str(it.get('bvid') or '')
            resource_type = int(it.get('type', 2))
            valid_bvid = re.fullmatch(BV, bvid) is not None
            url = 'https://www.bilibili.com/video/'+bvid if resource_type == 2 and valid_bvid else None
            # Favorites can include PGC episodes. Preserve their actual API-provided playback link,
            # without expanding the single-BV archival/downloader scope.
            if not url and it.get('link'):
                try:
                    link = validate_url(str(it['link']))
                    if link.hostname == 'www.bilibili.com' and re.fullmatch(r'/bangumi/play/ep[0-9]+/?',link.path):
                        url = 'https://www.bilibili.com'+link.path.rstrip('/')
                except LibraryError:
                    pass
            unavailable = bool(int(it.get('attr') or 0) & 1) or it.get('title') == '已失效视频'
            result.append(dict(title=str(it.get('title') or '未命名条目'),
                               bvid=bvid if valid_bvid else None, url=url,
                               status='unavailable' if unavailable else ('available' if url else 'unsupported'),
                               resource_type=resource_type, download_supported=bool(resource_type == 2 and valid_bvid),
                               resource_id=it.get('id')))
        if not data.get('has_more'):
            return dict(folder_id=fid, title=title, reported_count=expected, count=len(result),
                        items=result, note='仅导出列表；收藏夹可能在分页过程中发生变化，不自动下载视频')
        if not items:
            raise LibraryError('收藏夹分页提前中断；未生成不完整导出')
        time.sleep(0.2)
    raise LibraryError('收藏夹超过 1000 页；未生成不完整导出')
