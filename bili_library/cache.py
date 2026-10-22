"""Explicit, previewed cleanup of stopped-task leftovers; no automatic eviction."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import uuid
import time

from .common import LibraryError, private_dir
from .network import BV


def tree_state(path, device):
    """Never follow symlinks, hardlinks, other mounts or special files."""
    entries=[]
    pending=[path]
    total=0
    while pending:
        current=pending.pop()
        info=current.lstat()
        if info.st_dev!=device or stat.S_ISLNK(info.st_mode):
            raise ValueError('unsafe tree')
        name=str(current.relative_to(path.parent))
        entries.append((name,info.st_ino,info.st_size,info.st_mtime_ns,info.st_mode))
        if len(entries)>100000:
            raise LibraryError('缓存文件过多，请分批手动检查')
        if stat.S_ISDIR(info.st_mode):
            pending.extend(current.iterdir())
        elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
            total+=info.st_size
        else:
            raise ValueError('unsafe tree')
    return total,sorted(entries)


def root(lib,name):
    path=lib.home/name
    if path.is_symlink() or path.resolve().parent!=lib.home.resolve():
        raise LibraryError('缓存目录异常，拒绝扫描或清理')
    return path


def scan(lib,include_failed):
    items=[];signatures=[];skipped=0
    device=lib.home.stat().st_dev
    tasks=lib.rows('SELECT id,video_id,source,state FROM tasks')
    active=[t for t in tasks if t['state'] in ('queued','running')]
    active_ids={t['id'] for t in active}
    unresolved_short=any(not t['video_id'] and t['source'].startswith('https://b23.tv/') for t in active)
    for entry in sorted(root(lib,'tmp').iterdir()):
        if not re.fullmatch(r'[a-f0-9]{32}',entry.name) or entry.name in active_ids:
            skipped+=1;continue
        try: size,fingerprint=tree_state(entry,device)
        except (ValueError,FileNotFoundError): skipped+=1;continue
        items.append({'kind':'temporary','id':entry.name,'name':'tmp/'+entry.name,'bytes':size})
        signatures.append(('temporary',entry.name,fingerprint))
    if include_failed:
        videos=root(lib,'videos')
        for video in lib.rows('SELECT * FROM videos ORDER BY id'):
            vid=video['id']
            if not re.fullmatch(BV+r'_[1-9][0-9]*',vid):
                skipped+=1;continue
            sources={video['source'],*(t['source'] for t in tasks if t['video_id']==vid)}
            related=[t for t in tasks if t['video_id']==vid or t['source'] in sources]
            if unresolved_short or any(t['state'] in ('queued','running','completed') for t in related):
                skipped+=1;continue
            directory=videos/vid
            # Protect complete archives even if their completed task history was deleted.
            if all((directory/name).is_file() for name in ('metadata.json','subtitles.json','frames/index.json')):
                skipped+=1;continue
            try: size,fingerprint=tree_state(directory,device)
            except FileNotFoundError: size,fingerprint=0,[]
            except ValueError: skipped+=1;continue
            items.append({'kind':'incomplete','id':vid,'name':video['title'],'bytes':size})
            signatures.append(('incomplete',vid,fingerprint,video,related))
    digest=hashlib.sha256(json.dumps(signatures,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'items':items,'snapshot':digest,'skipped':skipped,
            'free_bytes':shutil.disk_usage(lib.home).free,'min_free_bytes':lib.config['min_free_bytes'],
            'quota_bytes':lib.config['quota_bytes'],
            'reclaimable_bytes':sum(i['bytes'] for i in items if i['kind']=='temporary'),
            'trash_bytes':sum(i['bytes'] for i in items if i['kind']=='incomplete')}


def preview(lib,include_failed=False):
    if not isinstance(include_failed,bool): raise LibraryError('缓存扫描参数无效')
    with lib.lock():
        return scan(lib,include_failed)


def clean(lib,snapshot,include_failed=False,confirmed=False):
    if confirmed is not True: raise LibraryError('请先预览并确认清理缓存')
    if not isinstance(include_failed,bool) or not isinstance(snapshot,str) or not re.fullmatch('[a-f0-9]{64}',snapshot):
        raise LibraryError('清理参数无效，请重新扫描')
    moved=[];deleted=0;freed=0
    with lib.lock():
        try:
            with lib.db:
                lib.db.execute('BEGIN IMMEDIATE')
                current=scan(lib,include_failed)
                if current['snapshot']!=snapshot:
                    raise LibraryError('缓存或任务状态已变化，请重新扫描后确认')
                incomplete=[i for i in current['items'] if i['kind']=='incomplete']
                if incomplete:
                    trash=root(lib,'trash');private_dir(trash)
                    for item in incomplete:
                        video=lib.video(item['id']);source=lib.home/'videos'/item['id']
                        tid=uuid.uuid4().hex;destination=trash/tid
                        if source.exists():
                            source.rename(destination);moved.append((source,destination))
                        lib.db.execute('INSERT INTO trash VALUES(?,?,?)',(tid,json.dumps(video,ensure_ascii=False),time.time()))
                        lib.db.execute('UPDATE tasks SET video_id=NULL WHERE video_id=?',(video['id'],))
                        lib.db.execute('DELETE FROM videos WHERE id=?',(video['id'],))
        except Exception:
            for source,destination in reversed(moved):
                destination.rename(source)
            raise
        # DB commits before removing temporary files, so archives remain recoverable
        # even if a later filesystem operation fails. A retry always needs a new scan.
        for item in current['items']:
            if item['kind']!='temporary': continue
            target=lib.home/'tmp'/item['id']
            try:
                if target.is_dir(): shutil.rmtree(target)
                else: target.unlink()
                deleted+=1;freed+=item['bytes']
            except OSError:
                raise LibraryError('已完成部分清理，部分临时文件无法删除；请重新扫描并检查权限') from None
    return {'ok':True,'deleted_temporary':deleted,'moved_to_trash':len(incomplete),
            'freed_bytes':freed,'free_bytes':shutil.disk_usage(lib.home).free}
