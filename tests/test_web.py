import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from bili_library.common import Library,LibraryError,write_json
from bili_library import management as m
from bili_library.remote_favorites import preview,commit
from bili_library.webserver import AdminServer

BV='BV1234567890'
ITEM=dict(title='线性代数：伴随矩阵',url='https://www.bilibili.com/video/'+BV,bvid=BV,
          resource_id=123,resource_type=2,status='available',download_supported=True)
DATA=dict(folder_id=456,title='数学收藏',items=[ITEM],count=1)


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.lib=Library(Path(self.temp.name)/'library');self.addCleanup(self.lib.close)
        self.lib.config['min_free_bytes']=0
    def test_sync_preserves_annotations_and_tracks_removed(self):
        m.import_favorites(self.lib,DATA)
        m.annotate(self.lib,['456:2:123'],dict(category='复习',tags=['线代'],note='再看一次'))
        m.import_favorites(self.lib,DATA)
        item=m.favorite_items(self.lib)[0]
        self.assertEqual((item['category'],item['tags'],item['note']),('复习',['线代'],'再看一次'))
        m.import_favorites(self.lib,{**DATA,'items':[]})
        self.assertEqual(m.favorite_items(self.lib),[])
        m.import_favorites(self.lib,DATA)
        self.assertEqual(m.favorite_items(self.lib)[0]['category'],'复习')
    def test_classification_preview_apply_and_manual_protection(self):
        m.import_favorites(self.lib,DATA)
        p=m.classify(self.lib,['456:2:123'])
        self.assertEqual(p['count'],1);self.assertEqual(m.favorite_items(self.lib)[0]['category'],'')
        m.classify(self.lib,['456:2:123'],True)
        self.assertEqual(m.favorite_items(self.lib)[0]['category'],'数学与考研')
        m.annotate(self.lib,['456:2:123'],{'category':'自定义'})
        self.assertEqual(m.classify(self.lib,['456:2:123'],True)['count'],0)
    def test_duplicate_detection_and_url_rejection(self):
        m.import_favorites(self.lib,DATA);m.import_favorites(self.lib,{**DATA,'folder_id':789})
        self.assertTrue(all(i['duplicate_count']==2 for i in m.favorite_items(self.lib)))
        for url in ['https://evil.com/','https://www.bilibili.com.evil.com/a','file:///etc/passwd']:
            with self.assertRaises(LibraryError): m.import_favorites(self.lib,{**DATA,'items':[{**ITEM,'url':url}]})
    def test_trash_restore_purge_and_busy_lock(self):
        video=dict(id=BV+'_22',bvid=BV,cid=22,page=1,title='测试',part_title='测试',source=ITEM['url']+'?p=1',duration=4)
        with self.lib.db:
            self.lib.db.execute('INSERT INTO videos('+','.join(video)+') VALUES('+','.join('?' for _ in video)+')',tuple(video.values()))
        directory=self.lib.home/'videos'/video['id'];directory.mkdir();(directory/'video.mp4').write_bytes(b'content')
        with self.lib.lock(),self.assertRaises(LibraryError): m.delete_video(self.lib,video['id'])
        deleted=m.delete_video(self.lib,video['id'])
        self.assertFalse(directory.exists());self.assertEqual(self.lib.rows('SELECT * FROM videos'),[])
        m.trash_action(self.lib,deleted['trash_id'],True)
        self.assertTrue((directory/'video.mp4').exists());self.assertEqual(len(self.lib.rows('SELECT * FROM videos')),1)
        deleted=m.delete_video(self.lib,video['id']);m.trash_action(self.lib,deleted['trash_id'],False)
        self.assertFalse((self.lib.home/'trash'/deleted['trash_id']).exists())
    def test_organized_import_roundtrip_and_invalid_group_is_atomic(self):
        snapshot=dict(format='bili-library-organized-v1',folders=[{**DATA,'items':[{**ITEM,'category':'复习','tags':['线代'],'note':'重点'}]}])
        m.import_favorites(self.lib,snapshot)
        self.assertEqual(m.favorite_items(self.lib)[0]['tags'],['线代'])
        broken=dict(format='bili-library-organized-v1',folders=[{**DATA,'folder_id':789},{**DATA,'folder_id':'bad'}])
        with self.assertRaises(LibraryError): m.import_favorites(self.lib,broken)
        self.assertEqual([r['id'] for r in self.lib.rows('SELECT id FROM favorite_folders')],[456])

    def test_server_controller_close_does_not_cancel_independent_cli_task(self):
        from bili_library.webserver import Controller
        task=self.lib.submit(ITEM['url']);self.lib.update_task(task['id'],state='running')
        controller=Controller(self.lib.home);controller.close()
        self.assertEqual(self.lib.task(task['id'])['cancel_requested'],0)

    def test_config_rules_validation(self):
        with self.assertRaises(LibraryError): m.save_config(self.lib,{'initial_frames':100})
        with self.assertRaises(LibraryError): m.save_config(self.lib,{'token':'oops'})
        with self.assertRaises(LibraryError): m.save_rules(self.lib,[{'category':'a','keywords':[]}])
        m.save_config(self.lib,{'task_timeout_seconds':60})
        self.assertEqual(json.loads(self.lib.config_path.read_text())['task_timeout_seconds'],60)


class RemoteTests(ManagementTests):
    def network(self):
        net=Mock();net.cookies={'SESSDATA':'account-secret','bili_jct':'csrf-secret'}
        net.api.side_effect=lambda path,**kw: {'isLogin':True,'mid':1234} if path.endswith('/nav') else {'list':[{'id':456,'title':'数学'},{'id':789,'title':'复习'}]}
        return net
    def prepare(self,action='move'):
        m.import_favorites(self.lib,DATA);net=self.network()
        with patch('bili_library.remote_favorites.export_favorites',return_value=DATA):
            p=preview(self.lib,['456:2:123'],action,789,net)
        net.post_favorite.assert_not_called()
        return p,net
    def test_preview_makes_no_write_and_preserves_backup(self):
        p,net=self.prepare()
        self.assertTrue((self.lib.home/'exports'/('before-remote-'+p['id']+'.json')).exists())
        self.assertNotIn('account-secret',json.dumps(p))
        self.assertEqual(p['confirmation'],'移动1条')
    def test_move_verified_and_plan_cannot_replay(self):
        p,net=self.prepare()
        with patch('bili_library.remote_favorites.export_favorites',side_effect=[DATA,{**DATA,'items':[]},{**DATA,'folder_id':789}]):
            result=commit(self.lib,p['id'],p['confirmation'],net)
        self.assertTrue(result['ok'])
        net.post_favorite.assert_called_once_with('/x/v3/fav/resource/move',{'resources':'123:2','platform':'web','src_media_id':456,'tar_media_id':789,'mid':1234})
        self.assertEqual([i['folder_id'] for i in m.favorite_items(self.lib)],[789])
        with self.assertRaises(LibraryError): commit(self.lib,p['id'],p['confirmation'],net)
    def test_wrong_confirmation_or_unowned_folder_never_writes(self):
        p,net=self.prepare()
        with self.assertRaises(LibraryError): commit(self.lib,p['id'],'错误',net)
        net.post_favorite.assert_not_called()
        net.api.side_effect=lambda path,**kw: {'isLogin':True,'mid':5678} if path.endswith('/nav') else {'list':[]}
        with self.assertRaises(LibraryError): commit(self.lib,p['id'],p['confirmation'],net)
        net.post_favorite.assert_not_called()
    def test_expired_plan_never_writes(self):
        p,net=self.prepare();p['expires']=time.time()-1
        with self.lib.db: self.lib.db.execute('UPDATE remote_plans SET payload=? WHERE id=?',(json.dumps(p),p['id']))
        with self.assertRaises(LibraryError): commit(self.lib,p['id'],p['confirmation'],net)
        net.post_favorite.assert_not_called()

    def test_timeout_never_retries_or_claims_success(self):
        p,net=self.prepare('delete');net.post_favorite.side_effect=LibraryError('结果不确定')
        with patch('bili_library.remote_favorites.export_favorites',return_value=DATA),self.assertRaises(LibraryError):
            commit(self.lib,p['id'],p['confirmation'],net)
        self.assertEqual(net.post_favorite.call_count,1)
        self.assertEqual(self.lib.rows('SELECT state FROM remote_plans')[0]['state'],'uncertain')
        self.assertEqual(len(m.favorite_items(self.lib)),1)
    def test_partial_verification_not_marked_success(self):
        p,net=self.prepare()
        with patch('bili_library.remote_favorites.export_favorites',side_effect=[DATA,DATA,{**DATA,'folder_id':789,'items':[]}]):
            result=commit(self.lib,p['id'],p['confirmation'],net)
        self.assertFalse(result['ok']);self.assertTrue(result['uncertain'])
    def test_changed_membership_and_unsupported_type_blocked(self):
        p,net=self.prepare()
        with patch('bili_library.remote_favorites.export_favorites',return_value={**DATA,'items':[]}),self.assertRaises(LibraryError):
            commit(self.lib,p['id'],p['confirmation'],net)
        net.post_favorite.assert_not_called()


class WriterTransportTests(unittest.TestCase):
    def test_constrained_post_csrf_and_no_redirect(self):
        from bili_library.network import Network
        network=Network({'SESSDATA':'secret','bili_jct':'csrf-only'})
        response=Mock(status=200);response.read.return_value=b'{"code":0}'
        conn=Mock();conn.getresponse.return_value=response
        with patch('bili_library.network.PinnedHTTPS',return_value=conn):
            self.assertEqual(network.post_favorite('/x/v3/fav/resource/batch-del',{'media_id':456,'resources':'123:2'}),{'accepted':True})
        args=conn.request.call_args
        self.assertEqual(args.args[:2],('POST','/x/v3/fav/resource/batch-del'))
        self.assertIn(b'csrf=csrf-only',args.kwargs['body'])
        with self.assertRaises(LibraryError): network.post_favorite('/arbitrary-endpoint',{})
        response.status=302
        with patch('bili_library.network.PinnedHTTPS',return_value=conn),self.assertRaises(LibraryError):
            network.post_favorite('/x/v3/fav/resource/batch-del',{})
        self.assertNotIn('secret',str(network.post_favorite))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)/'library'
        self.server=AdminServer(self.home,port=0,token='test-token-with-at-least-32-characters',remote=False)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close_server);self.cookie='';self.csrf=''
    def close_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
    def request(self,path,body=None,extra=None,raw=False):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=10)
        headers={'Host':'127.0.0.1:'+str(self.server.server_address[1])}
        if self.cookie: headers['Cookie']=self.cookie
        if body is not None: headers.update({'Content-Type':'application/json','X-CSRF-Token':self.csrf})
        headers.update(extra or {})
        conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
        res=conn.getresponse();content=res.read();status=res.status;out_headers=dict(res.getheaders());conn.close()
        return status,(content if raw else json.loads(content)),out_headers
    def login(self):
        status,_,headers=self.request('/api/login',{'token':'test-token-with-at-least-32-characters'})
        self.assertEqual(status,200);self.cookie=headers['Set-Cookie'].split(';')[0]
        self.csrf=self.request('/api/session')[1]['csrf']
    def test_auth_csrf_origin_and_secret_isolation(self):
        self.assertEqual(self.request('/api/state')[0],401)
        self.login()
        self.assertEqual(self.request('/api/state',extra={'Host':'evil.test'})[0],403)
        self.assertEqual(self.request('/api/tasks',{'url':ITEM['url']},extra={'Origin':'https://evil.test'})[0],403)
        csrf=self.csrf;self.csrf='wrong';self.assertEqual(self.request('/api/tasks',{'url':ITEM['url']})[0],403);self.csrf=csrf
        state=self.request('/api/state')[1]
        self.assertNotIn(self.server.token,json.dumps(state))
        self.assertEqual(self.request('/api/job',{'kind':'remote-commit','payload':{}})[0],400)
        self.request('/api/logout',{})
        self.assertEqual(self.request('/api/state')[0],401)
    def test_submit_favorite_import_annotate_export_and_persistence(self):
        self.login()
        self.assertEqual(self.request('/api/tasks',{'url':ITEM['url']+'?p=2'})[0],200)
        self.assertEqual(self.request('/api/favorites/import',{'data':DATA})[0],200)
        self.request('/api/favorites/annotate',{'keys':['456:2:123'],'patch':{'category':'学习','tags':['线代']}})
        result=self.request('/api/favorites')[1]['items'][0]
        self.assertEqual(result['category'],'学习')
        code,csv,headers=self.request('/api/export?format=csv',raw=True)
        self.assertEqual(code,200);self.assertIn('学习'.encode(),csv)
        self.assertIn('attachment',headers['Content-Disposition'])
        lib=Library(self.home);self.assertEqual(m.favorite_items(lib)[0]['tags'],['线代']);lib.close()
    def test_static_security_headers_path_traversal_and_range(self):
        code,body,headers=self.request('/',raw=True)
        self.assertEqual(code,200);self.assertIn(b'BILIBILI LIBRARY',body)
        self.assertIn('Content-Security-Policy',headers)
        self.login()
        self.assertEqual(self.request('/../../secrets/web-token')[0],404)
        self.assertNotEqual(self.request('/media/x/frame/../../secrets/web-token')[0],200)
        self.assertEqual(self.request('/api/tasks',{'url':'https://127.0.0.1/'})[0],400)
    def test_media_range_and_organized_export(self):
        self.login()
        lib=Library(self.home)
        with lib.db: lib.db.execute("INSERT INTO videos(id,bvid,cid,page,title,part_title,source,duration,media) VALUES(?,?,?,?,?,?,?,?,?)",(BV+'_22',BV,22,1,'test','test',ITEM['url'],4,'video.mp4'))
        directory=lib.home/'videos'/(BV+'_22');directory.mkdir();(directory/'video.mp4').write_bytes(b'0123456789')
        lib.close()
        code,body,headers=self.request('/media/'+BV+'_22/video',extra={'Range':'bytes=2-5'},raw=True)
        self.assertEqual((code,body),(206,b'2345'));self.assertEqual(headers['Content-Range'],'bytes 2-5/10')
        self.request('/api/favorites/import',{'data':DATA})
        exported=self.request('/api/export?format=json')[1]
        self.assertEqual(exported['format'],'bili-library-organized-v1')
        self.assertEqual(exported['folders'][0]['folder_id'],456)

    def test_credentials_upload_never_echoed(self):
        self.login();data={'sessdata':'my-account-secret','bili_jct':'my-csrf','dedeuserid':'123'}
        status,value,_=self.request('/api/credentials',{'data':data})
        self.assertEqual(status,200);self.assertNotIn('my-account-secret',json.dumps(value))
        self.assertNotIn('my-account-secret',json.dumps(self.request('/api/config')[1]))
        self.assertEqual((self.home/'secrets/bilibili.json').stat().st_mode&0o777,0o600)

    def test_public_origin_cookie_csrf_and_host_pairing(self):
        self.server.public_origin='https://bili.alaner.space'
        self.server.public_host='bili.alaner.space'
        headers={'Host':'bili.alaner.space','Origin':'https://bili.alaner.space'}
        status,_,response=self.request('/api/login',{'token':self.server.token},extra=headers)
        self.assertEqual(status,200)
        self.assertIn('; Secure',response['Set-Cookie'])
        self.cookie=response['Set-Cookie'].split(';')[0]
        self.csrf=self.request('/api/session',extra=headers)[1]['csrf']
        self.assertEqual(self.request('/api/tasks',{'url':ITEM['url']},extra=headers)[0],200)
        self.assertEqual(self.request('/api/tasks',{'url':ITEM['url']},extra={**headers,'Origin':'http://bili.alaner.space'})[0],403)
        self.assertEqual(self.request('/api/session',extra={'Origin':headers['Origin']})[0],403)
        self.csrf='wrong'
        self.assertEqual(self.request('/api/tasks',{'url':ITEM['url']},extra=headers)[0],403)
        self.csrf=self.request('/api/session',extra=headers)[1]['csrf']
        self.assertIn('; Secure',self.request('/api/logout',{},extra=headers)[2]['Set-Cookie'])

    def test_invalid_public_origin_fails_before_binding(self):
        for origin in ['http://bili.example.com','https://bili.example.com/','https://u:p@bili.example.com','https://bili.example.com:bad','https://bili.example.com?x=1']:
            with patch.dict(os.environ,{'BILI_WEB_PUBLIC_ORIGIN':origin}),self.assertRaises(LibraryError):
                AdminServer(self.home,port=0,token=self.server.token)


if __name__=='__main__': unittest.main()
