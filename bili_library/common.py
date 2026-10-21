from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid


class LibraryError(Exception):
    pass


DEFAULTS = dict(max_duration_seconds=3600, max_download_bytes=1024**3,
                task_timeout_seconds=1800, quota_bytes=10*1024**3,
                min_free_bytes=2*1024**3, initial_frames=20,
                max_extra_frames=20, download_rate_bytes=2*1024**2)


def home_path():
    parent = Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh'))) / 'bili-duty'
    return Path(os.environ.get('BILI_LIBRARY_HOME', str(parent / 'library'))).expanduser().resolve()


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_json(path, value):
    private_dir(path.parent)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as f:
            os.chmod(tmp, 0o600)
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise LibraryError('JSON 文件无法读取：' + path.name) from None


def number(value, name='时间', minimum=0):
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise LibraryError(name + '必须是有效数字') from None
    if not math.isfinite(n) or n < minimum:
        raise LibraryError(name + '超出范围')
    return n


def timestamp(value):
    pieces = str(value).split(':')
    if len(pieces) > 3:
        raise LibraryError('时间格式应为秒或 HH:MM:SS')
    result = 0.0
    for piece in pieces:
        result = result * 60 + number(piece)
    return result


class Library:
    def __init__(self, home=None):
        self.home = private_dir(Path(home) if home else home_path())
        self.config_path = self.home / 'config.json'
        if not self.config_path.exists():
            write_json(self.config_path, DEFAULTS)
        self.config = {**DEFAULTS, **read_json(self.config_path, {})}
        for key in DEFAULTS:
            self.config[key] = int(number(self.config[key], key, 1 if key not in ('min_free_bytes',) else 0))
        if self.config['initial_frames'] > 20 or self.config['max_extra_frames'] > 20:
            raise LibraryError('初始截图和每次补帧均不得超过 20 张')
        for name in ('videos', 'tmp', 'exports', 'secrets'):
            private_dir(self.home / name)
        self.db = sqlite3.connect(self.home / 'index.sqlite3', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY, bvid TEXT NOT NULL, cid INTEGER NOT NULL,
            page INTEGER NOT NULL, title TEXT NOT NULL, part_title TEXT NOT NULL,
            source TEXT NOT NULL, duration REAL NOT NULL, media TEXT,
            height INTEGER, bytes INTEGER, subtitle_status TEXT NOT NULL DEFAULT 'missing',
            UNIQUE(bvid,cid));
          CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, state TEXT NOT NULL,
            stage TEXT NOT NULL, video_id TEXT REFERENCES videos(id), error TEXT,
            created REAL NOT NULL, updated REAL NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0);
        ''')
        # Upgrade existing libraries without discarding their task history.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(tasks)')}
            for name in ('attempted', 'started', 'finished'):
                if name not in columns:
                    self.db.execute('ALTER TABLE tasks ADD COLUMN '+name+' REAL')
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS task_logs (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            created REAL NOT NULL, level TEXT NOT NULL, code TEXT NOT NULL,
            message TEXT NOT NULL, details TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS task_logs_task ON task_logs(task_id,id);
          CREATE INDEX IF NOT EXISTS task_attempt_time ON tasks(attempted DESC,created DESC);
        """)
        os.chmod(self.home / 'index.sqlite3', 0o600)
        from .management import init_tables
        init_tables(self)

    def close(self):
        self.db.close()

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def task(self, task_id):
        rows = self.rows('SELECT * FROM tasks WHERE id=?', (task_id,))
        if not rows:
            raise LibraryError('任务不存在')
        return rows[0]

    def video(self, video_id):
        rows = self.rows('SELECT * FROM videos WHERE id=?', (video_id,))
        if not rows:
            raise LibraryError('视频不存在')
        return rows[0]

    def update_task(self, task_id, **changes):
        now = time.time()
        changes['updated'] = now
        if changes.get('state') == 'running':
            changes.update(attempted=now, started=now, finished=None)
        elif changes.get('state') == 'queued':
            changes.update(attempted=now, started=None, finished=None)
        elif changes.get('state') in ('completed', 'failed', 'cancelled'):
            changes['finished'] = now
        with self.db:
            self.db.execute('UPDATE tasks SET ' + ','.join(k+'=?' for k in changes) + ' WHERE id=?',
                            (*changes.values(), task_id))

    def submit(self, source):
        from .network import video_input
        source = video_input(source)  # no network here
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            found = self.rows("SELECT * FROM tasks WHERE source=? AND state IN ('queued','running')", (source,))
            if found:
                return {**found[0], 'reused_task': True}
            task_id = uuid.uuid4().hex
            now = time.time()
            self.db.execute('INSERT INTO tasks(id,source,state,stage,video_id,error,created,updated,cancel_requested,attempted) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (task_id, source, 'queued', 'queued', None, None, now, now, 0, now))
        return self.task(task_id)

    def usage(self):
        return sum(p.stat().st_size for p in self.home.rglob('*') if p.is_file() and not p.is_symlink())

    def check_space(self, reserve=0):
        if self.usage() + reserve > self.config['quota_bytes']:
            raise LibraryError('资料库磁盘配额不足')
        if shutil.disk_usage(self.home).free - reserve < self.config['min_free_bytes']:
            raise LibraryError('磁盘剩余空间不足')

    @contextlib.contextmanager
    def lock(self):
        with (self.home / 'worker.lock').open('a') as f:
            os.chmod(f.name, 0o600)
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LibraryError('已有任务正在处理，请勿并行运行 worker 或补帧') from None
            try:
                yield f
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
