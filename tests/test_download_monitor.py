import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from bili_library.common import Library, LibraryError
from bili_library.media import DownloadMonitor
from bili_library.management import task_logs


class DownloadMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.lib=Library(Path(self.temp.name)/'library');self.addCleanup(self.lib.close)
        self.lib.config['min_free_bytes']=0
        self.now=0
        self.clock=patch('bili_library.media.time.monotonic',side_effect=lambda:self.now)
        self.clock.start();self.addCleanup(self.clock.stop)

    def test_many_chunks_do_not_walk_library_and_still_recheck(self):
        with patch.object(self.lib,'usage',wraps=self.lib.usage) as usage:
            monitor=DownloadMonitor(self.lib)
            for n in range(1,1001):
                monitor.hook({'filename':'video.part','downloaded_bytes':n*1024})
            self.assertEqual(usage.call_count,1)
            self.now=2.1
            monitor.hook({'filename':'video.part','downloaded_bytes':1024*1001})
            self.assertEqual(usage.call_count,2)

    def test_limits_are_enforced_between_full_scans(self):
        monitor=DownloadMonitor(self.lib)
        self.lib.config['max_download_bytes']=100
        monitor.hook({'filename':'video','downloaded_bytes':60})
        with self.assertRaisesRegex(LibraryError,'大小上限'):
            monitor.hook({'filename':'audio','downloaded_bytes':41})
        self.lib.config['max_download_bytes']=1000
        self.lib.config['quota_bytes']=monitor.baseline+80
        with self.assertRaisesRegex(LibraryError,'配额不足'):
            monitor.hook({'filename':'audio','downloaded_bytes':21})
        self.lib.config['quota_bytes']=10**9
        self.lib.config['min_free_bytes']=1
        with patch('bili_library.media.shutil.disk_usage',return_value=Mock(free=0)):
            with self.assertRaisesRegex(LibraryError,'剩余空间不足'):
                monitor.hook({'filename':'video','downloaded_bytes':60})
        self.lib.config['task_timeout_seconds']=1;self.now=2
        with self.assertRaisesRegex(LibraryError,'超时'):
            monitor.hook({'filename':'video','downloaded_bytes':60})

    def test_progress_is_bounded_and_excludes_paths_and_secrets(self):
        t=self.lib.submit('https://www.bilibili.com/video/BV1234567890')
        monitor=DownloadMonitor(self.lib,t['id'])
        progress={'filename':'secret-file','downloaded_bytes':500,'speed':123.5,'eta':4.5,'total_bytes':1000,'url':'https://secret/','Cookie':'secret-cookie'}
        monitor.hook(progress)
        self.now=1;monitor.hook(progress)
        self.assertEqual(len(task_logs(self.lib,t['id'])['logs']),1)
        self.now=16;monitor.hook(progress)
        logs=task_logs(self.lib,t['id'])['logs']
        self.assertEqual(len(logs),2)
        self.assertEqual(logs[0]['details']['speed_bytes'],123)
        self.assertNotIn('secret',json.dumps(logs))
        monitor.hook({**progress,'status':'finished','speed':float('nan')})
        self.assertNotIn('speed_bytes',task_logs(self.lib,t['id'])['logs'][-1]['details'])


if __name__=='__main__': unittest.main()
