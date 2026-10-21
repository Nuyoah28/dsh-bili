import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock

from bili_library.common import Library, LibraryError
from bili_library.diagnostics import classify, record, DownloadLogger
from bili_library.management import clear_history, task_logs
from bili_library.media import download
from bili_library.__main__ import execute, parser

URL='https://www.bilibili.com/video/BV1234567890?p=1'


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.lib=Library(Path(self.temp.name)/'library');self.addCleanup(self.lib.close)

    def test_times_sorting_retry_and_preserved_logs(self):
        with patch('bili_library.common.time.time', return_value=100):
            first=self.lib.submit(URL)
        with patch('bili_library.common.time.time', return_value=200):
            self.lib.update_task(first['id'],state='running')
        record(self.lib,first['id'],'starting')
        with patch('bili_library.common.time.time', return_value=210):
            self.lib.update_task(first['id'],state='failed')
        self.assertEqual(self.lib.task(first['id'])['finished'],210)
        with patch('bili_library.common.time.time', return_value=220):
            second=self.lib.submit(URL.replace('p=1','p=2'))
        with patch('bili_library.common.time.time', return_value=300):
            execute(parser().parse_args(['retry',first['id']]),self.lib)
        ordered=execute(parser().parse_args(['task']),self.lib)
        self.assertEqual([r['id'] for r in ordered],[first['id'],second['id']])
        self.assertIsNone(ordered[0]['started']);self.assertIsNone(ordered[0]['finished'])
        self.assertEqual(len(task_logs(self.lib,first['id'])['logs']),1)

    def test_delete_and_clear_active_protection_and_media_survival(self):
        done=self.lib.submit(URL)
        self.lib.update_task(done['id'],state='completed')
        record(self.lib,done['id'],'completed')
        waiting=self.lib.submit(URL.replace('p=1','p=2'))
        record(self.lib,waiting['id'],'starting')
        for kind in ('tasks','logs'):
            with self.assertRaises(LibraryError): clear_history(self.lib,kind,waiting['id'],True)
            with self.assertRaises(LibraryError): clear_history(self.lib,kind,done['id'])
        media=self.lib.home/'videos'/'keep.mp4';media.write_bytes(b'keep')
        with self.lib.lock(),self.assertRaises(LibraryError):
            clear_history(self.lib,'tasks',done['id'],True)
        self.assertEqual(clear_history(self.lib,'logs',done['id'],True)['count'],1)
        self.assertEqual(self.lib.task(done['id'])['state'],'completed')
        record(self.lib,done['id'],'completed')
        self.assertEqual(clear_history(self.lib,'tasks',confirmed=True)['count'],1)
        self.assertEqual(len(self.lib.rows('SELECT * FROM tasks')),1)
        self.assertEqual(len(self.lib.rows('SELECT * FROM task_logs')),1)
        self.assertTrue(media.exists())

    def test_delete_jobs_retains_running_and_remote_plan(self):
        for jid,state in [('a'*32,'completed'),('b'*32,'running')]:
            with self.lib.db:
                self.lib.db.execute('INSERT INTO web_jobs VALUES(?,?,?,?,?,?,?)',(jid,'worker',state,None,None,1,1))
        self.assertEqual(clear_history(self.lib,'jobs',confirmed=True)['count'],1)
        self.assertEqual(self.lib.rows('SELECT * FROM web_jobs')[0]['state'],'running')

    def test_safe_diagnostics_categorized_bounded_no_secrets(self):
        t=self.lib.submit(URL);logger=DownloadLogger(self.lib,t['id'])
        raw='HTTP Error 403: https://cdn.example/video?token=secret-media Cookie: SESSDATA=secret-cookie Authorization: Bearer secret-key'
        logger.error(raw)
        logs=task_logs(self.lib,t['id'])['logs']
        self.assertEqual(logs[0]['code'],'http_403')
        self.assertNotIn('secret',json.dumps(logs));self.assertNotIn('https:',json.dumps(logs))
        for _ in range(205):
            record(self.lib,t['id'],'extract',cookie_present=True,yt_dlp_version='2026.10.20',url=raw)
        logs=task_logs(self.lib,t['id'])['logs']
        self.assertEqual(len(logs),200)
        self.assertNotIn('secret',json.dumps(logs))
        for message,expected in [('网络安全策略拒绝非公网目标地址','non_public'),('Requested format is not available','format'),('HTTP Error 429','http_429'),('certificate verify failed','tls'),('Name resolution failed','dns'),('下载音视频合计超过大小上限','size'),('task timeout','timeout')]:
            self.assertEqual(classify(message),expected)

    def test_download_error_has_stage_and_safe_specific_reason(self):
        try: import yt_dlp
        except ImportError: self.skipTest('yt-dlp unavailable')
        t=self.lib.submit(URL);work=self.lib.home/'tmp'/t['id'];work.mkdir()
        ydl=Mock();ydl.__enter__=Mock(return_value=ydl);ydl.__exit__=Mock(return_value=False)
        ydl.extract_info.side_effect=RuntimeError('HTTP Error 403: https://cdn.invalid/?token=secret Cookie: secret')
        guard=Mock(url='http://127.0.0.1:1234',failure_code=None)
        with patch('bili_library.media.binary',return_value='/usr/bin/true'),patch('bili_library.media.GuardProxy') as proxy,patch('yt_dlp.YoutubeDL',return_value=ydl):
            proxy.return_value.__enter__.return_value=guard
            with self.assertRaisesRegex(LibraryError,r'\[http_403\]') as raised:
                download(self.lib,{'source':URL},work,{'SESSDATA':'secret-cookie'})
        self.assertNotIn('secret',str(raised.exception))
        self.assertEqual(task_logs(self.lib,t['id'])['logs'][0]['code'],'extract')

    def test_guard_proxy_preserves_non_public_failure_without_raw_text(self):
        import base64
        import socket
        from urllib.parse import urlsplit
        from bili_library.network import GuardProxy
        with patch('bili_library.network.connect_public',side_effect=LibraryError('网络安全策略拒绝非公网目标地址 secret')):
            with GuardProxy() as guard:
                u=urlsplit(guard.url)
                auth=base64.b64encode((u.username+':'+u.password).encode()).decode()
                with socket.create_connection((u.hostname,u.port),timeout=3) as conn:
                    conn.sendall(('CONNECT api.bilibili.com:443 HTTP/1.1\r\nProxy-Authorization: Basic '+auth+'\r\n\r\n').encode())
                    self.assertEqual(conn.recv(1024),b'')
                self.assertEqual(guard.failure_code,'non_public')

    def test_upgrade_legacy_database_preserves_unknown_times(self):
        home=Path(self.temp.name)/'legacy';home.mkdir()
        with sqlite3.connect(home/'index.sqlite3') as db:
            db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,source TEXT,state TEXT,stage TEXT,video_id TEXT,error TEXT,created REAL,updated REAL,cancel_requested INTEGER)')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)',('a'*32,URL,'failed','download',None,'legacy',100,120,0))
        upgraded=Library(home)
        try:
            self.assertIsNone(upgraded.task('a'*32)['attempted'])
            self.assertIsNone(upgraded.task('a'*32)['started'])
            self.assertEqual(upgraded.task('a'*32)['error'],'legacy')
            clear_history(upgraded,'tasks','a'*32,True)
        finally: upgraded.close()


if __name__=='__main__': unittest.main()
