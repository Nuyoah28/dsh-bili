from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .bilibili import load_cookies, metadata, subtitles
from .common import Library, LibraryError, private_dir, read_json, write_json
from .media import download, frames, initial_times, probe
from .network import Network


def cached(library, video):
    if not video or not video.get('media'):
        return False
    directory = library.home / 'videos' / video['id']
    media = directory / video['media']
    if not media.is_file() or media.stat().st_size != video.get('bytes'):
        return False
    try:
        probe(media)
        info = read_json(directory / 'metadata.json')
        subs = read_json(directory / 'subtitles.json')
        index = read_json(directory / 'frames' / 'index.json')
        count = len(initial_times(video['duration'], library.config['initial_frames']))
        return bool(info and subs is not None and isinstance(index, list) and len(index) >= count
                    and all((directory / f['file']).is_file() and (directory / f['file']).stat().st_size for f in index))
    except (LibraryError, KeyError, OSError):
        return False


def process(library, task_id):
    task = library.task(task_id)
    work = private_dir(library.home / 'tmp' / task_id)
    try:
        library.update_task(task_id, stage='metadata')
        cookies = load_cookies(library)
        network = Network(cookies)
        # A cached canonical URL or previously resolved short URL needs no network.
        found = library.rows('SELECT v.* FROM videos v JOIN tasks t ON t.video_id=v.id WHERE t.source=? '
                             'ORDER BY t.updated DESC LIMIT 1', (task['source'],))
        if not found:
            found = library.rows('SELECT * FROM videos WHERE source=?', (task['source'],))
        meta = found[0] if found else metadata(network, task['source'])
        if meta['duration'] > library.config['max_duration_seconds']:
            raise LibraryError('所选分P超过时长限制')
        with library.db:
            library.db.execute('INSERT OR IGNORE INTO videos(id,bvid,cid,page,title,part_title,source,duration) '
                               'VALUES (?,?,?,?,?,?,?,?)', tuple(meta[k] for k in ('id','bvid','cid','page','title','part_title','source','duration')))
        library.update_task(task_id, video_id=meta['id'])
        video = library.video(meta['id'])
        if cached(library, video):
            library.update_task(task_id, state='completed', stage='cached', error=None)
            return
        library.check_space(32*1024**2)
        directory = private_dir(library.home / 'videos' / meta['id'])
        library.update_task(task_id, stage='download')
        media = directory / (video.get('media') or 'missing')
        usable = False
        if media.is_file() and media.stat().st_size == video.get('bytes'):
            try:
                checked = probe(media)
                usable = checked['height'] <= 720 and abs(checked['duration']-meta['duration']) <= max(5,meta['duration']*.05)
            except LibraryError:
                pass
        if not usable:
            path, checked = download(library, meta, work, cookies)
            media = directory / path.name
            path.replace(media)
        with library.db:
            library.db.execute('UPDATE videos SET media=?,height=?,bytes=? WHERE id=?',
                               (media.name, checked['height'], media.stat().st_size, meta['id']))
        write_json(directory / 'metadata.json', library.video(meta['id']))
        library.update_task(task_id, stage='subtitles')
        subs = read_json(directory / 'subtitles.json')
        if not subs or subs.get('status') != 'available':
            subs = subtitles(network, meta)
            write_json(directory / 'subtitles.json', subs)
        with library.db:
            library.db.execute('UPDATE videos SET subtitle_status=? WHERE id=?', (subs['status'],meta['id']))
        library.update_task(task_id, stage='frames')
        frames(library, directory, media, initial_times(checked['duration'],library.config['initial_frames']), checked['duration'])
        write_json(directory / 'metadata.json', library.video(meta['id']))
        library.check_space()
        library.update_task(task_id, state='completed', stage='completed', error=None)
    except LibraryError as exc:
        library.update_task(task_id, state='failed', error=str(exc))
    except Exception:
        # Avoid leaking downloader/network exceptions and credential-bearing URLs.
        library.update_task(task_id, state='failed', error='处理失败；请检查依赖、配置及本地媒体文件')
    finally:
        shutil.rmtree(work, ignore_errors=True)


def stop_process(child):
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def worker(library, stop_event=None):
    outcomes = []
    with library.lock() as lock_file:
        # The lock proves there is no live worker. Keep interrupted jobs visible, never auto-restart them.
        interrupted = library.rows("SELECT id FROM tasks WHERE state='running'")
        with library.db:
            library.db.execute("UPDATE tasks SET state='failed',error='上次处理被中断，请显式 retry',updated=? WHERE state='running'", (time.time(),))
        for previous in interrupted:
            shutil.rmtree(library.home / 'tmp' / previous['id'], ignore_errors=True)
        while True:
            if stop_event is not None and stop_event.is_set(): break
            pending = library.rows("SELECT * FROM tasks WHERE state='queued' ORDER BY created LIMIT 1")
            if not pending:
                break
            task_id = pending[0]['id']
            library.update_task(task_id, state='running', stage='starting', error=None)
            child = subprocess.Popen([sys.executable, '-m', 'bili_library', '--home', str(library.home), '_process', task_id],
                                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     cwd=str(Path(__file__).resolve().parent.parent), pass_fds=(lock_file.fileno(),),
                                     env={**os.environ, 'PYTHONUTF8':'1', 'BILI_LIBRARY_WORKER_FD':str(lock_file.fileno())})
            started = time.monotonic()
            error = None
            cancelled = False
            try:
                while child.poll() is None:
                    if stop_event is not None and stop_event.is_set():
                        error, cancelled = '后台停止，取消本次 worker 任务', True
                        break
                    if library.task(task_id)['cancel_requested']:
                        error, cancelled = '用户取消任务', True
                        break
                    if time.monotonic()-started > library.config['task_timeout_seconds']:
                        error = '任务超时，已终止下载及媒体处理子进程'
                        break
                    try:
                        library.check_space()
                    except LibraryError as exc:
                        error = str(exc)
                        break
                    time.sleep(0.25)
            except KeyboardInterrupt:
                error, cancelled = '用户中断 worker', True
            finally:
                stop_process(child)
            current = library.task(task_id)
            if error or current['state'] == 'running':
                library.update_task(task_id, state='cancelled' if cancelled else 'failed',
                                    error=error or '任务子进程异常退出')
            shutil.rmtree(library.home / 'tmp' / task_id, ignore_errors=True)
            outcomes.append(library.task(task_id))
            if cancelled:
                break
    return {'tasks': outcomes, 'ok': all(t['state']=='completed' for t in outcomes)}
