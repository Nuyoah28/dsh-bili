import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from bili_library.common import Library,LibraryError
from bili_library.cache import preview,clean
from bili_library.management import trash_action

URL='https://www.bilibili.com/video/BV1234567890?p=1'
VID='BV1234567890_22'


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.lib=Library(Path(self.temp.name)/'library');self.addCleanup(self.lib.close)
    def temporary(self,task_id='a'*32):
        d=self.lib.home/'tmp'/task_id;d.mkdir();(d/'video.mp4.part').write_bytes(b'x'*123)
        return d
    def video(self,completed=False,active=False):
        task=self.lib.submit(URL)
        self.lib.update_task(task['id'],state='completed' if completed else ('queued' if active else 'failed'))
        with self.lib.db:
            self.lib.db.execute('INSERT INTO videos(id,bvid,cid,page,title,part_title,source,duration) VALUES(?,?,?,?,?,?,?,?)',(VID,'BV1234567890',22,1,'test','test',URL,60))
            self.lib.db.execute('UPDATE tasks SET video_id=? WHERE id=?',(VID,task['id']))
        d=self.lib.home/'videos'/VID;d.mkdir();(d/'video.mkv').write_bytes(b'media')
        return task,d
    def test_preview_and_confirm_delete_only_temporary(self):
        d=self.temporary();task,archive=self.video()
        secret=self.lib.home/'secrets'/'keep';secret.write_bytes(b'credential')
        runtime=self.lib.home/'.venv';runtime.mkdir();(runtime/'keep').write_bytes(b'runtime')
        report=preview(self.lib)
        self.assertEqual(report['reclaimable_bytes'],123)
        self.assertEqual(report['trash_bytes'],0)
        with self.assertRaises(LibraryError):clean(self.lib,report['snapshot'])
        result=clean(self.lib,report['snapshot'],confirmed=True)
        self.assertEqual(result['freed_bytes'],123);self.assertFalse(d.exists())
        self.assertTrue(archive.exists());self.assertTrue(secret.exists());self.assertTrue((runtime/'keep').exists())
        self.assertEqual(self.lib.task(task['id'])['state'],'failed')
    def test_incomplete_archive_is_recoverable_and_history_preserved(self):
        task,d=self.video()
        report=preview(self.lib,True)
        self.assertEqual(report['trash_bytes'],5)
        result=clean(self.lib,report['snapshot'],True,True)
        self.assertEqual(result['moved_to_trash'],1);self.assertEqual(result['freed_bytes'],0)
        self.assertFalse(d.exists());self.assertEqual(self.lib.rows('SELECT * FROM videos'),[])
        self.assertIsNone(self.lib.task(task['id'])['video_id'])
        tid=self.lib.rows('SELECT id FROM trash')[0]['id']
        trash_action(self.lib,tid,True)
        self.assertEqual((d/'video.mkv').read_bytes(),b'media')
    def test_protect_completed_archive_even_after_history_removed(self):
        task,d=self.video(completed=True)
        self.assertEqual(preview(self.lib,True)['items'],[])
        with self.lib.db:self.lib.db.execute('DELETE FROM tasks WHERE id=?',(task['id'],))
        (d/'metadata.json').write_text('{}');(d/'subtitles.json').write_text('{}')
        (d/'frames').mkdir();(d/'frames'/'index.json').write_text('[]')
        self.assertEqual(preview(self.lib,True)['items'],[])
    def test_active_tasks_aliases_and_worker_lock_protected(self):
        task,d=self.video(active=True);tmp=self.temporary(task['id'])
        self.assertEqual(preview(self.lib,True)['items'],[])
        with self.lib.lock(),self.assertRaises(LibraryError):preview(self.lib)
        self.lib.update_task(task['id'],state='failed')
        p=preview(self.lib,True)
        with self.lib.lock(),self.assertRaises(LibraryError):clean(self.lib,p['snapshot'],True,True)
        self.assertTrue(d.exists());self.assertTrue(tmp.exists())
    def test_stale_preview_refused_when_bytes_or_task_state_change(self):
        d=self.temporary();p=preview(self.lib)
        (d/'video.mp4.part').write_bytes(b'changed')
        with self.assertRaisesRegex(LibraryError,'重新扫描'):clean(self.lib,p['snapshot'],confirmed=True)
        self.assertTrue(d.exists())
        task,archive=self.video();p=preview(self.lib,True)
        self.lib.update_task(task['id'],state='queued')
        with self.assertRaisesRegex(LibraryError,'重新扫描'):clean(self.lib,p['snapshot'],True,True)
        self.assertTrue(archive.exists());self.assertTrue(d.exists())
    def test_symlinks_hardlinks_unknown_names_and_roots_not_followed(self):
        outside=Path(self.temp.name)/'outside';outside.mkdir();target=outside/'secret';target.write_bytes(b'keep')
        (self.lib.home/'tmp'/('b'*32)).symlink_to(outside,target_is_directory=True)
        d=self.temporary();(d/'link').symlink_to(target)
        (self.lib.home/'tmp'/'user-folder').mkdir()
        self.assertEqual(preview(self.lib)['items'],[])
        import os
        os.link(target,self.lib.home/'tmp'/('c'*32))
        self.assertEqual(preview(self.lib)['items'],[])
        self.assertEqual(target.read_bytes(),b'keep')
        import shutil
        shutil.rmtree(d);(self.lib.home/'tmp'/('b'*32)).unlink()
        shutil.rmtree(self.lib.home/'tmp');(self.lib.home/'tmp').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(LibraryError):preview(self.lib)
        self.assertTrue(target.exists())
    def test_failure_during_move_restores_previous_directory_and_db(self):
        task,d=self.video()
        video=self.lib.video(VID);second={**video,'id':'BV1234567890_33','cid':33}
        with self.lib.db:
            self.lib.db.execute('INSERT INTO videos('+','.join(second)+') VALUES('+','.join('?' for _ in second)+')',tuple(second.values()))
        other=self.lib.home/'videos'/second['id'];other.mkdir();(other/'video.part').write_bytes(b'keep')
        p=preview(self.lib,True)
        with patch.object(self.lib,'video',side_effect=[video,LibraryError('test')]):
            with self.assertRaises(LibraryError):clean(self.lib,p['snapshot'],True,True)
        self.assertTrue(d.exists());self.assertTrue(other.exists())
        self.assertEqual(len(self.lib.rows('SELECT * FROM videos')),2)
        self.assertEqual(self.lib.rows('SELECT * FROM trash'),[])
    def test_queued_short_url_protects_archive_with_unknown_target(self):
        task,d=self.video()
        self.lib.submit('https://b23.tv/Abc123')
        self.assertEqual(preview(self.lib,True)['items'],[])
        self.assertTrue(d.exists())
    def test_space_error_explains_current_free_reserve_and_quota(self):
        self.lib.config['quota_bytes']=10**12
        self.lib.config['min_free_bytes']=2*1024**3
        with patch('bili_library.common.shutil.disk_usage',return_value=Mock(free=int(2.1*1024**3))):
            with self.assertRaisesRegex(LibraryError,'当前剩余.*最低保留.*临时预留'):
                self.lib.check_space(300*1024**2)
        self.lib.config['quota_bytes']=1
        with self.assertRaisesRegex(LibraryError,'配额不足.*已用.*配额.*临时预留'):self.lib.check_space()


if __name__=='__main__':unittest.main()
