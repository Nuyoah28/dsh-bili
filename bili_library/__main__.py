from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

from .common import Library, LibraryError, home_path, read_json, timestamp, write_json


def doctor(home):
    target = home
    while not target.exists():
        target = target.parent
    tools = {}
    for name in ('ffmpeg', 'ffprobe'):
        path = os.environ.get('BILI_'+name.upper()) or shutil.which(name)
        available = False
        if path:
            try:
                available = subprocess.run([path, '-version'], capture_output=True, timeout=5).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        tools[name] = {'available': available, 'path': path}
    memory = None
    try:
        if sys.platform == 'darwin':
            call = subprocess.run(['sysctl','-n','hw.memsize'], capture_output=True, timeout=3, text=True)
            if call.returncode == 0:
                memory = int(call.stdout)
        else:
            memory = os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    tools['yt_dlp'] = {'available': importlib.util.find_spec('yt_dlp') is not None}
    return dict(ok=sys.version_info >= (3,10) and all(t['available'] for t in tools.values()),
                system=platform.system(), architecture=platform.machine(), python=platform.python_version(),
                python_supported=sys.version_info >= (3,10), cpu_count=os.cpu_count(), memory_bytes=memory,
                memory_note=None if memory else '系统权限未允许读取内存容量',
                disk_free_bytes=shutil.disk_usage(target).free, home=str(home), tools=tools,
                note='仅检查；不安装模型、不修改 VPN 或系统代理。工作负载串行处理。')


def parser():
    p = argparse.ArgumentParser(description='本地 B站视频资料库（无定时推荐或账号互动）')
    p.add_argument('--home', type=Path, help='数据目录；默认 BILI_LIBRARY_HOME 或 $DSH_HOME/bili-duty/library')
    p.add_argument('--json', action='store_true', help='默认已输出 JSON；收藏夹可选择其他导出格式')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    sub.add_parser('init')
    s = sub.add_parser('web'); s.add_argument('--port',type=int,default=int(os.environ.get('BILI_WEB_PORT','8765')))
    s = sub.add_parser('submit'); s.add_argument('url')
    s = sub.add_parser('worker'); s.add_argument('--drain', action='store_true', help='串行处理当前队列后退出（默认行为）')
    s = sub.add_parser('task'); s.add_argument('id', nargs='?')
    sub.add_parser('list')
    for cmd in ('show','subtitles','frames'):
        s = sub.add_parser(cmd); s.add_argument('id')
        if cmd == 'subtitles':
            s.add_argument('--refresh', action='store_true', help='登录后重新获取真实字幕；不重新下载视频')
        if cmd == 'frames':
            s.add_argument('--export', type=Path)
        s.add_argument('--json', action='store_true')
    for cmd in ('cancel','retry','_process'):
        s = sub.add_parser(cmd); s.add_argument('id')
    s = sub.add_parser('frames-add'); s.add_argument('id')
    s.add_argument('--at', action='append', help='可重复指定秒数或 HH:MM:SS')
    s.add_argument('--from', dest='start'); s.add_argument('--to', dest='end')
    s.add_argument('--every', default='10')
    s = sub.add_parser('credentials-import'); s.add_argument('file', type=Path)
    s = sub.add_parser('favorites')
    fs = s.add_subparsers(dest='action', required=True)
    f = fs.add_parser('folders'); f.add_argument('--uid')
    f = fs.add_parser('export'); f.add_argument('folder', help='收藏夹 ID 或含 fid 的收藏夹链接')
    f.add_argument('--format', choices=['json','csv','markdown'], default='json')
    f.add_argument('--out', type=Path, help='省略时保存到资料库 exports/，完整列表同时输出')
    return p


def export_text(data, format_):
    if format_ == 'json':
        return json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    if format_ == 'csv':
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['title','url','bvid','status'])
        for item in data['items']:
            row = [item[k] or '' for k in ('title','url','bvid','status')]
            # Prevent spreadsheet formula execution when opening an untrusted title in Excel.
            writer.writerow(["'"+s if s.lstrip().startswith(('=','+','-','@','\t','\r')) else s for s in row])
        return '\ufeff'+out.getvalue()
    def escape(value):
        for char in ('\\','[',']','*','_','`','<','>'):
            value = value.replace(char,'\\'+char)
        return value.replace('\n',' ').replace('\r',' ')
    return '# '+escape(data['title'])+'\n\n'+''.join(
        '- '+('['+escape(i['title'])+']('+i['url']+')' if i['url'] else escape(i['title']))
        +' · '+i['status']+'\n' for i in data['items'])


def execute(args, library):
    from .bilibili import Network, load_cookies, import_credentials, folders, export_favorites, folder_id
    from .jobs import worker, process
    if args.command == 'init':
        return {'ok':True, 'home':str(library.home), 'config':library.config}
    if args.command == 'submit':
        return library.submit(args.url)
    if args.command == 'worker':
        return worker(library)
    if args.command == 'task':
        return library.task(args.id) if args.id else library.rows('SELECT * FROM tasks ORDER BY created DESC')
    if args.command == 'list':
        return library.rows('SELECT * FROM videos ORDER BY rowid DESC')
    if args.command in ('cancel','retry'):
        task = library.task(args.id)
        if args.command == 'cancel':
            if task['state'] == 'running':
                library.update_task(args.id, cancel_requested=1)
            elif task['state'] == 'queued':
                library.update_task(args.id, state='cancelled', error='用户取消任务')
            else:
                raise LibraryError('只能取消排队或运行中的任务')
        else:
            if task['state'] not in ('failed','cancelled','completed'):
                raise LibraryError('任务仍在队列或处理中，无需重试')
            library.update_task(args.id, state='queued', stage='queued', error=None, cancel_requested=0)
        return library.task(args.id)
    if args.command == '_process':
        try:
            fd = int(os.environ.get('BILI_LIBRARY_WORKER_FD', '-1'))
            inherited = os.fstat(fd)
            actual = (library.home/'worker.lock').stat()
            if (inherited.st_dev, inherited.st_ino) != (actual.st_dev, actual.st_ino):
                raise ValueError()
        except (ValueError, OSError):
            raise LibraryError('内部处理入口必须由持有排他锁的 worker 启动') from None
        if library.task(args.id)['state'] != 'running':
            raise LibraryError('内部处理入口只接受 worker 已领取的任务')
        process(library,args.id)
        return {'ok':library.task(args.id)['state']=='completed'}
    if args.command == 'credentials-import':
        return import_credentials(library,args.file)
    if args.command == 'favorites':
        network = Network(load_cookies(library))
        if args.action == 'folders':
            return folders(network,args.uid)
        with library.lock():
            fid = folder_id(args.folder)
            suffix = {'json':'json','csv':'csv','markdown':'md'}[args.format]
            target = args.out or library.home/'exports'/f'favorites-{fid}.{suffix}'
            target = target.expanduser().resolve()
            if target.exists():
                raise LibraryError('导出文件已存在，请选择新的 --out，避免覆盖已有资料')
            data = export_favorites(network,args.folder)
            target.parent.mkdir(parents=True, exist_ok=True)
            library.check_space(len(export_text(data,args.format).encode('utf-8')))
            with target.open('x',encoding='utf-8') as f:
                os.chmod(target,0o600)
                f.write(export_text(data,args.format))
            return {**data,'export_file':str(target)}
    video = library.video(args.id)
    directory = library.home/'videos'/video['id']
    if args.command == 'show':
        return video
    if args.command == 'subtitles':
        if args.refresh:
            from .bilibili import subtitles
            with library.lock():
                data = subtitles(Network(load_cookies(library)), video)
                write_json(directory/'subtitles.json',data)
                with library.db:
                    library.db.execute('UPDATE videos SET subtitle_status=? WHERE id=?',(data['status'],video['id']))
                write_json(directory/'metadata.json',library.video(video['id']))
                return data
        data = read_json(directory/'subtitles.json')
        if data is None:
            raise LibraryError('字幕尚未处理，请查询任务状态')
        return data
    if args.command == 'frames':
        data = read_json(directory/'frames'/'index.json',[])
        if args.export:
            args.export.mkdir(parents=True,exist_ok=True)
            for item in data:
                source = directory/item['file']
                destination = args.export/source.name
                # copyfile would follow an existing symlink / overwrite user files
                with destination.open('xb') as output, source.open('rb') as source_file:
                    shutil.copyfileobj(source_file,output)
        return {'video_id':video['id'], 'directory':str(directory), 'frames':data}
    if args.command == 'frames-add':
        from .media import frames
        if bool(args.at) == bool(args.start is not None or args.end is not None):
            raise LibraryError('请选择 --at，或者完整的 --from/--to 区间')
        if args.at:
            times = sorted(set(timestamp(v) for v in args.at))
        else:
            if args.start is None or args.end is None:
                raise LibraryError('区间必须同时提供 --from 和 --to')
            start,end,step = timestamp(args.start),timestamp(args.end),timestamp(args.every)
            if step <= 0 or end < start:
                raise LibraryError('区间或采样间隔无效')
            count = int((end-start)/step)+1
            if count > library.config['max_extra_frames']:
                raise LibraryError('一次补帧数量超过上限')
            times = [start+i*step for i in range(count)]
        if len(times) > library.config['max_extra_frames']:
            raise LibraryError('一次补帧数量超过上限')
        if not video['media']:
            raise LibraryError('视频尚未下载成功')
        with library.lock():
            return {'frames':frames(library,directory,directory/video['media'],times,video['duration'])}
    raise LibraryError('命令不可用')


def main():
    args = parser().parse_args()
    os.umask(0o077)
    if args.command == 'doctor':
        value = doctor(args.home or home_path())
        print(json.dumps(value,ensure_ascii=False,indent=2))
        return 0 if value['ok'] else 1
    library = None
    try:
        library = Library(args.home)
        if args.command == 'web':
            from .webserver import serve
            directory=library.home;library.close();library=None
            serve(directory,args.port)
            return 0
        value = execute(args,library)
        print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
        return 1 if isinstance(value,dict) and value.get('ok') is False else 0
    except LibraryError as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({'ok':False,'error':'操作已中断'},ensure_ascii=False))
        return 130
    except Exception:
        print(json.dumps({'ok':False,'error':'操作失败；请检查输入格式、文件权限及依赖'},ensure_ascii=False))
        return 1
    finally:
        if library:
            library.close()


if __name__ == '__main__':
    sys.exit(main())
