import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

from bili_library.common import Library, LibraryError, read_json, write_json
from bili_library.network import video_input, validate_url, public_addresses, Network, connect_public
from bili_library.bilibili import metadata, subtitles, export_favorites, folder_id, import_credentials
from bili_library.__main__ import execute, parser, export_text
from bili_library.jobs import process, cached, worker
from bili_library.media import initial_times, frames, probe, download

BV = 'BV1234567890'
URL = 'https://www.bilibili.com/video/'+BV+'?p=2'
META = dict(id=BV+'_22', bvid=BV,cid=22,page=2,title='课程',part_title='第二讲',source=URL,duration=4.0)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lib = Library(Path(self.temp.name)/'library')
        self.addCleanup(self.lib.close)
        self.lib.config['min_free_bytes'] = 0
        self.lib.config['initial_frames'] = 3
        write_json(self.lib.config_path,self.lib.config)

    def args(self,*values):
        return parser().parse_args(values)


class InputTests(Base):
    def test_single_page_and_no_collection(self):
        self.assertEqual(video_input(BV),URL.replace('p=2','p=1'))
        self.assertEqual(video_input(URL+'&spm_id_from=123'),URL)
        for value in ['https://evil.com/video/'+BV,'https://www.bilibili.com.evil.com/video/'+BV,
                      'https://127.0.0.1/video/'+BV,'http://www.bilibili.com/video/'+BV,
                      'https://u:p@www.bilibili.com/video/'+BV,URL+'&p=3',URL.replace('p=2','p=0'),
                      'https://www.bilibili.com/medialist/play/1','file:///etc/passwd',
                      'https://b23.tv/a@b','https://www.bilibili.com:8080/video/'+BV]:
            with self.subTest(value=value), self.assertRaises(LibraryError):
                video_input(value)

    def test_metadata_uses_selected_cid(self):
        network=Mock()
        network.resolve.return_value=URL
        network.api.return_value=dict(title='课程',pages=[dict(page=1,cid=11,duration=1),dict(page=2,cid=22,duration=4,part='第二讲')])
        self.assertEqual(metadata(network,URL),META)
        network.api.return_value['pages'].pop()
        with self.assertRaises(LibraryError): metadata(network,URL)

    def test_queue_duplicate_and_cancel(self):
        first=self.lib.submit(URL)
        self.assertEqual(first['id'],self.lib.submit(URL)['id'])
        execute(self.args('cancel',first['id']),self.lib)
        self.assertEqual(self.lib.task(first['id'])['state'],'cancelled')
        execute(self.args('retry',first['id']),self.lib)
        self.assertEqual(self.lib.task(first['id'])['state'],'queued')

    def test_lock_cross_process(self):
        with self.lib.lock():
            call=subprocess.run([sys.executable,'-c',
                'from bili_library.common import Library; import sys; l=Library(sys.argv[1]);\nwith l.lock(): print("unexpected")',str(self.lib.home)],capture_output=True,text=True)
            self.assertNotEqual(call.returncode,0)
            self.assertNotIn('unexpected',call.stdout)
        with self.lib.lock(): pass

    def test_quotas_config_and_private_files(self):
        self.assertEqual(self.lib.config_path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.lib.home.stat().st_mode & 0o777,0o700)
        self.lib.config['quota_bytes']=1
        with self.assertRaises(LibraryError): self.lib.check_space()
        write_json(self.lib.config_path,{'task_timeout_seconds':-1})
        with self.assertRaises(LibraryError): Library(self.lib.home)

    def test_internal_process_rejects_without_lock(self):
        task=self.lib.submit(URL)
        self.lib.update_task(task['id'],state='running')
        with patch.dict(os.environ,{},clear=True), self.assertRaises(LibraryError):
            execute(self.args('_process',task['id']),self.lib)

    def test_credentials_separate_not_returned(self):
        source=Path(self.temp.name)/'input.json'
        write_json(source,dict(sessdata='secret-only',bili_jct='csrf-only',dedeuserid='123'))
        value=import_credentials(self.lib,source)
        self.assertNotIn('secret-only',json.dumps(value))
        self.assertEqual((self.lib.home/'secrets'/'bilibili.json').stat().st_mode & 0o777,0o600)


class NetworkTests(unittest.TestCase):
    def test_private_mixed_ipv6_and_rebinding(self):
        for address in ['127.0.0.1','10.1.2.3','169.254.169.254','192.168.1.1','::1','fc00::1','::ffff:127.0.0.1']:
            with self.subTest(address=address),self.assertRaises(LibraryError):
                public_addresses('api.bilibili.com',resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))])
        with self.assertRaises(LibraryError):
            public_addresses('api.bilibili.com',resolver=lambda *a,**k:[(2,1,6,'',('1.1.1.1',443)),(2,1,6,'',('10.0.0.1',443))])
        entry=(2,1,6,'',('1.1.1.1',443))
        with patch('bili_library.network.public_addresses',return_value=[entry]) as resolved,patch('bili_library.network.socket.socket') as factory:
            connect_public('api.bilibili.com')
            factory.return_value.connect.assert_called_once_with(('1.1.1.1',443))
            resolved.assert_called_once()

    def test_redirect_never_reaches_private_or_external(self):
        for target in ['https://127.0.0.1/','https://evil.com/','http://api.bilibili.com/']:
            response=Mock(status=302)
            response.getheader.return_value=target
            conn=Mock(); conn.getresponse.return_value=response
            with patch('bili_library.network.PinnedHTTPS',return_value=conn) as connect,self.assertRaises(LibraryError):
                Network().get('https://b23.tv/test')
            self.assertEqual(connect.call_count,1)

    def test_cookies_only_sent_to_api_or_web(self):
        response=Mock(status=200); response.read.return_value=b'{}'
        conn=Mock(); conn.getresponse.return_value=response
        with patch('bili_library.network.PinnedHTTPS',return_value=conn):
            Network({'SESSDATA':'secret'}).get('https://i0.hdslb.com/a.json')
            self.assertNotIn('Cookie',conn.request.call_args.kwargs['headers'])
            Network({'SESSDATA':'secret'}).get('https://api.bilibili.com/x/test')
            self.assertEqual(conn.request.call_args.kwargs['headers']['Cookie'],'SESSDATA=secret')


class SubtitleTests(Base):
    def test_manual_preferred_over_ai_and_danmaku(self):
        network=Mock()
        network.api.return_value={'subtitle':{'subtitles':[
            {'lan':'ai-zh','subtitle_url':'https://i0.hdslb.com/ai.json'},
            {'lan':'danmaku','subtitle_url':'https://comment.bilibili.com/22.xml'},
            {'lan':'zh-CN','subtitle_url':'https://i0.hdslb.com/manual.json'}]}}
        network.json.return_value={'body':[{'from':1,'to':2.4,'content':'知识点'}]}
        value=subtitles(network,META)
        self.assertEqual(value['source'],'platform_manual')
        self.assertEqual(value['segments'],[{'start':1.0,'end':2.4,'text':'知识点'}])
        network.json.assert_called_once_with('https://i0.hdslb.com/manual.json')

    def test_refresh_subtitles_without_download(self):
        with self.lib.db:
            self.lib.db.execute('INSERT INTO videos(id,bvid,cid,page,title,part_title,source,duration) VALUES(?,?,?,?,?,?,?,?)',
                                tuple(META[k] for k in ('id','bvid','cid','page','title','part_title','source','duration')))
        refreshed=dict(status='available',segments=[dict(start=0,end=2,text='真实字幕')])
        with patch('bili_library.bilibili.subtitles',return_value=refreshed),patch('bili_library.jobs.download') as fetch:
            value=execute(self.args('subtitles',META['id'],'--refresh'),self.lib)
        self.assertEqual(value,refreshed)
        self.assertEqual(self.lib.video(META['id'])['subtitle_status'],'available')
        fetch.assert_not_called()

    def test_missing_or_auth_error_is_honest(self):
        network=Mock();network.api.return_value={}
        value=subtitles(network,META)
        self.assertEqual(value['status'],'missing');self.assertEqual(value['segments'],[])
        network.api.side_effect=LibraryError('需要登录')
        value=subtitles(network,META)
        self.assertEqual(value['status'],'unavailable');self.assertEqual(value['segments'],[])


class FavoriteTests(Base):
    def test_paginated_list_invalid_items_dedupe_and_readonly(self):
        network=Mock()
        first=dict(id=1,bvid=BV,type=2,title='课程标题',attr=0)
        invalid=dict(id=2,bvid='BV0987654321',type=2,title='已失效视频',attr=1)
        network.api.side_effect=[dict(info=dict(title='我的收藏',media_count=3),medias=[first],has_more=True),
                                 dict(medias=[first,invalid,dict(id=3,type=12,title='非视频')],has_more=False)]
        with patch('bili_library.bilibili.time.sleep'):
            value=export_favorites(network,'https://space.bilibili.com/123/favlist?fid=456')
        self.assertEqual(value['count'],3)
        self.assertEqual(value['items'][0]['url'],'https://www.bilibili.com/video/'+BV)
        self.assertEqual(value['items'][1]['status'],'unavailable')
        self.assertIsNone(value['items'][2]['url'])
        self.assertEqual([c.kwargs['pn'] for c in network.api.call_args_list],[1,2])
        self.assertTrue(all(c.args[0]=='/x/v3/fav/resource/list' for c in network.api.call_args_list))
        self.assertEqual(self.lib.rows('SELECT * FROM tasks'),[])

    def test_episode_original_link_preserved_but_not_downloadable(self):
        network=Mock()
        network.api.return_value=dict(info=dict(title='收藏'),has_more=False,medias=[
            dict(id=468227,type=24,bvid=BV,title='课程番剧',link='https://www.bilibili.com/bangumi/play/ep468227?theme=movie'),
            dict(id=2,type=24,title='恶意链接',link='https://127.0.0.1/internal')])
        data=export_favorites(network,'456')
        episode=data['items'][0]
        self.assertEqual(episode['url'],'https://www.bilibili.com/bangumi/play/ep468227')
        self.assertEqual(episode['bvid'],BV)
        self.assertEqual(episode['status'],'available')
        self.assertFalse(episode['download_supported'])
        self.assertIsNone(data['items'][1]['url'])

    def test_pagination_errors_never_silently_export_partial(self):
        network=Mock();page=dict(medias=[dict(id=1,bvid=BV,title='课程')],has_more=True)
        network.api.return_value=page
        with patch('bili_library.bilibili.time.sleep'),self.assertRaises(LibraryError):
            export_favorites(network,'456')
        network.api.return_value={'medias':[],'has_more':True}
        with self.assertRaises(LibraryError): export_favorites(network,'456')

    def test_exports_escape_untrusted_titles(self):
        data={'title':'我的收藏','items':[dict(title='=IMPORTXML("evil")',url='https://www.bilibili.com/video/'+BV,bvid=BV,status='available')]}
        self.assertIn("'=IMPORTXML",export_text(data,'csv'))
        data['items'][0]['title']='[link](evil)\n<script>'
        self.assertIn('\\[link\\]',export_text(data,'markdown'))
        self.assertEqual(json.loads(export_text(data,'json')),data)

    def test_cli_export_writes_file_no_tasks_no_secrets(self):
        value={'folder_id':456,'title':'收藏','count':1,'items':[dict(title='课程',url='https://www.bilibili.com/video/'+BV,bvid=BV,status='available')]}
        with patch('bili_library.bilibili.export_favorites',return_value=value):
            result=execute(self.args('favorites','export','456'),self.lib)
        self.assertEqual(read_json(Path(result['export_file'])),value)
        self.assertEqual(self.lib.rows('SELECT * FROM tasks'),[])
        with patch('bili_library.bilibili.export_favorites',return_value=value),self.assertRaises(LibraryError):
            execute(self.args('favorites','export','456'),self.lib)


class JobTests(Base):
    def test_failed_download_is_never_completed_and_redacts_errors(self):
        task=self.lib.submit(URL)
        with patch('bili_library.jobs.metadata',return_value=META),patch('bili_library.jobs.download',side_effect=RuntimeError('SESSDATA=secret')):
            process(self.lib,task['id'])
        value=self.lib.task(task['id'])
        self.assertEqual(value['state'],'failed')
        self.assertNotIn('secret',json.dumps(value))
        self.assertFalse((self.lib.home/'tmp'/task['id']).exists())

    def test_timeout_cancels_process_and_marks_task_failed(self):
        task=self.lib.submit(URL)
        child=Mock(pid=999999);child.poll.return_value=None
        with patch('bili_library.jobs.subprocess.Popen',return_value=child),patch('bili_library.jobs.stop_process') as stop,patch('bili_library.jobs.time.monotonic',side_effect=[0,999999]):
            result=worker(self.lib)
        self.assertEqual(result['tasks'][0]['state'],'failed')
        self.assertIn('超时',result['tasks'][0]['error'])
        stop.assert_called_once_with(child)

    def test_cancel_running_job_and_recover_interrupted_task(self):
        task=self.lib.submit(URL)
        child=Mock(pid=999999);child.poll.return_value=None
        self.lib.update_task(task['id'],cancel_requested=1)
        with patch('bili_library.jobs.subprocess.Popen',return_value=child),patch('bili_library.jobs.stop_process'):
            result=worker(self.lib)
        self.assertEqual(result['tasks'][0]['state'],'cancelled')
        old=self.lib.submit(URL)
        self.lib.update_task(old['id'],state='running')
        orphan=self.lib.home/'tmp'/old['id'];orphan.mkdir()
        (orphan/'partial').write_bytes(b'x')
        self.assertEqual(worker(self.lib)['tasks'],[])
        self.assertEqual(self.lib.task(old['id'])['state'],'failed')
        self.assertFalse(orphan.exists())

    def test_download_rejects_playlist_before_any_media(self):
        # Uses actual yt-dlp package only when installed; no network request is made.
        try: import yt_dlp
        except ImportError: self.skipTest('yt-dlp not installed')
        fake=Mock();fake.__enter__=Mock(return_value=fake);fake.__exit__=Mock(return_value=False)
        fake.extract_info.return_value={'_type':'playlist','id':BV}
        with patch('bili_library.media.binary',return_value='/bin/true'),patch('bili_library.media.GuardProxy') as guard,patch.object(yt_dlp,'YoutubeDL',return_value=fake):
            guard.return_value.__enter__.return_value.url='http://127.0.0.1:1'
            with self.assertRaises(LibraryError): download(self.lib,META,Path(self.temp.name),{})
        fake.process_info.assert_not_called()


@unittest.skipUnless(os.environ.get('BILI_FFMPEG') and os.environ.get('BILI_FFPROBE'),'set BILI_FFMPEG and BILI_FFPROBE for real media tests')
class RealMediaTests(Base):
    def fixture(self):
        path=Path(self.temp.name)/'fixture.mp4'
        subprocess.run([os.environ['BILI_FFMPEG'],'-v','error','-y','-f','lavfi','-i','testsrc2=size=320x240:rate=10',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','4','-c:v','libx264','-threads','1','-c:a','aac',str(path)],check=True,capture_output=True)
        return path

    def test_ffprobe_actual_timestamps_and_extra_frame_cache(self):
        path=self.fixture()
        self.assertEqual(probe(path)['height'],240)
        out=self.lib.home/'videos'/'fixture';out.mkdir()
        value=frames(self.lib,out,path,[0.15,1.25,2.35],4)
        self.assertEqual(len(value),3)
        for item in value:
            self.assertGreaterEqual(item['timestamp'],item['requested']-.001)
            self.assertLess(item['timestamp']-item['requested'],.101)
            self.assertGreater((out/item['file']).stat().st_size,100)
        self.assertEqual(len(frames(self.lib,out,path,[1.25,3.1],4)),4)
        with self.assertRaises(LibraryError): frames(self.lib,out,path,[4.1],4)

    def test_archive_subtitles_frames_and_repeat_cache_without_network(self):
        path=self.fixture()
        task=self.lib.submit(URL)
        def fake_download(library,meta,work,cookies):
            target=work/'video.mp4'; target.write_bytes(path.read_bytes())
            return target,probe(target)
        subs=dict(status='available',segments=[dict(start=0,end=1,text='测试课程')])
        with patch('bili_library.jobs.metadata',return_value=META),patch('bili_library.jobs.download',side_effect=fake_download) as fetch,patch('bili_library.jobs.subtitles',return_value=subs):
            process(self.lib,task['id'])
        self.assertEqual(self.lib.task(task['id'])['state'],'completed')
        self.assertEqual(fetch.call_count,1)
        vid=self.lib.video(META['id']);self.assertTrue(cached(self.lib,vid))
        repeat=self.lib.submit(URL)
        with patch('bili_library.jobs.metadata',side_effect=AssertionError('network must not be used')),patch('bili_library.jobs.download',side_effect=AssertionError('no duplicate download')):
            process(self.lib,repeat['id'])
        self.assertEqual(self.lib.task(repeat['id'])['stage'],'cached')
        self.assertEqual(execute(self.args('subtitles',META['id']),self.lib)['segments'][0]['text'],'测试课程')
        self.assertEqual(len(execute(self.args('frames',META['id']),self.lib)['frames']),3)


if __name__=='__main__': unittest.main()
