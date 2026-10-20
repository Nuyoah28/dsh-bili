import base64
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit, parse_qs

from bili_library.common import Library, LibraryError, write_json
from bili_library.mcp_auth import Auth, OAuthError, challenge
from bili_library.webserver import AdminServer

ORIGIN='https://bili.example.com'
BV='BV1234567890';VID=BV+'_22'


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.lib=Library(self.temp.name);self.addCleanup(self.lib.close)
        self.auth=Auth(self.lib.home,ORIGIN)
        self.client=self.auth.register(dict(redirect_uris=['http://127.0.0.1:12345/callback'],client_name='test'))
        self.verifier='v'*43
        self.request=dict(client_id=self.client['client_id'],redirect_uri=self.client['redirect_uris'][0],
                          response_type='code',resource=ORIGIN+'/mcp',scope='library',state='random-state',
                          code_challenge_method='S256',code_challenge=challenge(self.verifier))

    def code(self):
        nonce,binding,_=self.auth.begin(self.request)
        uri=self.auth.consent(nonce,binding,self.auth.owner,True)
        query=parse_qs(urlsplit(uri).query)
        self.assertEqual(query['iss'],[ORIGIN]);self.assertEqual(query['state'],['random-state'])
        return query['code'][0]

    def token_request(self):
        return dict(grant_type='authorization_code',client_id=self.client['client_id'],code=self.code(),
                    code_verifier=self.verifier,redirect_uri=self.request['redirect_uri'],resource=ORIGIN+'/mcp')

    def test_pkce_one_use_expiry_persistence_and_hashed_storage(self):
        data=self.token_request()
        with self.assertRaises(OAuthError): self.auth.token({**data,'code_verifier':'w'*43})
        with self.assertRaises(OAuthError): self.auth.token({**data,'resource':'https://other.test/mcp'})
        token=self.auth.token(data)
        self.assertTrue(self.auth.authenticated(token['access_token']))
        self.assertFalse(self.auth.authenticated(token['refresh_token']))
        with self.assertRaises(OAuthError): self.auth.token(data)
        restarted=Auth(self.lib.home,ORIGIN)
        self.assertTrue(restarted.authenticated(token['access_token']))
        raw=restarted.path.read_bytes()
        self.assertNotIn(token['access_token'].encode(),raw);self.assertNotIn(token['refresh_token'].encode(),raw)
        with restarted.db() as db: db.execute('UPDATE grants SET access_expires=0')
        self.assertFalse(restarted.authenticated(token['access_token']))

    def test_refresh_rotation_client_binding_and_revocation(self):
        token=self.auth.token(self.token_request())
        req=dict(grant_type='refresh_token',client_id=self.client['client_id'],resource=ORIGIN+'/mcp',refresh_token=token['refresh_token'])
        other=self.auth.register(dict(redirect_uris=['https://chatgpt.com/callback']))
        with self.assertRaises(OAuthError): self.auth.token({**req,'client_id':other['client_id']})
        new=self.auth.token(req)
        self.assertFalse(self.auth.authenticated(token['access_token']))
        self.assertTrue(self.auth.authenticated(new['access_token']))
        with self.assertRaises(OAuthError): self.auth.token(req)
        self.assertFalse(self.auth.authenticated(new['access_token']))
        new=self.auth.token(self.token_request())
        self.auth.revoke(dict(client_id=self.client['client_id'],token=new['refresh_token']))
        self.assertFalse(self.auth.authenticated(new['access_token']))

    def test_consent_browser_binding_scope_redirect_and_deny(self):
        for changed in [dict(redirect_uri='https://evil.test/callback'),dict(code_challenge_method='plain'),dict(scope='admin'),dict(resource='https://other.test')]:
            with self.assertRaises(OAuthError): self.auth.begin({**self.request,**changed})
        nonce,binding,_=self.auth.begin(self.request)
        with self.assertRaises(OAuthError): self.auth.consent(nonce,'wrong',self.auth.owner,True)
        with self.assertRaises(OAuthError): self.auth.consent(nonce,binding,self.auth.owner,True)
        nonce,binding,_=self.auth.begin(self.request)
        denied=self.auth.consent(nonce,binding,self.auth.owner,False)
        self.assertEqual(parse_qs(urlsplit(denied).query)['error'],['access_denied'])
        for uri in ['https://evil.test/callback','http://chatgpt.com/callback','https://chatgpt.com.evil.test/callback','file:///tmp/callback','https://u:p@chatgpt.com/callback','https://chatgpt.com/callback#secret']:
            with self.assertRaises(OAuthError): self.auth.register(dict(redirect_uris=[uri]))

    def test_expired_code_pending_and_resource_change(self):
        req=self.token_request()
        with self.auth.db() as db: db.execute('UPDATE codes SET expires=0')
        with self.assertRaises(OAuthError): self.auth.token(req)
        token=self.auth.token(self.token_request())
        other=Auth(self.lib.home,'https://changed.example.com')
        self.assertFalse(other.authenticated(token['access_token']))
        nonce,binding,_=self.auth.begin(self.request)
        with self.auth.guard:
            for pending in self.auth.pending.values(): pending['expires']=0
        with self.assertRaises(OAuthError): self.auth.consent(nonce,binding,self.auth.owner,True)


class MCPHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ,{'BILI_MCP_ENABLED':'1','BILI_WEB_PUBLIC_ORIGIN':ORIGIN}):
            self.server=AdminServer(self.temp.name,port=0,token='web-token-is-different-from-mcp-token')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
        self.token=self.server.mcp.owner
        lib=Library(self.temp.name)
        with lib.db:
            lib.db.execute('INSERT INTO videos(id,bvid,cid,page,title,part_title,source,duration) VALUES(?,?,?,?,?,?,?,?)',
                (VID,BV,22,1,'线性代数','第一课','https://www.bilibili.com/video/'+BV+'?p=1',30))
        self.directory=lib.home/'videos'/VID;self.directory.mkdir()
        write_json(self.directory/'subtitles.json',dict(status='available',source='platform_manual',segments=[dict(start=0,end=2,text='定义'),dict(start=10,end=12,text='例题')]))
        (self.directory/'frames').mkdir();(self.directory/'frames/a.jpg').write_bytes(b'\xff\xd8\xfftest-image')
        write_json(self.directory/'frames/index.json',[dict(file='frames/a.jpg',timestamp=1.04,requested=1)])
        lib.close()

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

    def request(self,path='/mcp',data=None,headers=None,method='POST',token=True,form=False):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=10)
        h={'Host':'bili.example.com','Accept':'application/json, text/event-stream'}
        if token: h['Authorization']='Bearer '+self.token
        payload=None
        if data is not None:
            payload=urlencode(data) if form else json.dumps(data)
            h['Content-Type']='application/x-www-form-urlencoded' if form else 'application/json'
        h.update(headers or {})
        conn.request(method,path,body=payload,headers=h);response=conn.getresponse()
        raw=response.read();status=response.status;out=dict(response.getheaders());conn.close()
        value=json.loads(raw) if raw and out.get('Content-Type','').startswith('application/json') else raw
        return status,value,out

    def rpc(self,method,params=None,**kwargs):
        return self.request(data=dict(jsonrpc='2.0',id=1,method=method,params=params or {}),**kwargs)

    def tool(self,name,args=None): return self.rpc('tools/call',dict(name=name,arguments=args or {}))[1]['result']

    def test_transport_auth_discovery_protocol_and_errors(self):
        self.assertEqual(self.rpc('tools/list',token=False)[0],401)
        self.assertIn('resource_metadata',self.rpc('tools/list',token=False)[2]['WWW-Authenticate'])
        self.assertEqual(self.rpc('tools/list',headers={'Authorization':'Bearer '+self.server.token})[0],401)
        self.assertEqual(self.rpc('tools/list',headers={'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.rpc('tools/list',headers={'Host':'evil.test'})[0],403)
        self.assertEqual(self.request(method='GET')[0],405)
        self.assertEqual(self.rpc('tools/list',headers={'Accept':'application/json'})[0],406)
        self.assertEqual(self.rpc('tools/list',headers={'MCP-Protocol-Version':'invalid'})[0],400)
        init=self.rpc('initialize',dict(protocolVersion='2025-06-18'))[1]['result']
        self.assertEqual(init['protocolVersion'],'2025-06-18')
        self.assertIn('字幕',init['instructions'])
        code,value,_=self.request(data=dict(jsonrpc='2.0',method='notifications/initialized'))
        self.assertEqual((code,value),(202,b''))
        self.assertEqual(self.rpc('not-found')[1]['error']['code'],-32601)
        tools=self.rpc('tools/list')[1]['result']['tools']
        self.assertTrue(all(t['inputSchema']['additionalProperties'] is False for t in tools))
        self.assertFalse(any('delete' in t['name'] or 'commit' in t['name'] for t in tools))
        self.assertEqual(self.request('/.well-known/oauth-protected-resource/mcp',method='GET',token=False)[1]['resource'],ORIGIN+'/mcp')

    def test_tool_images_subtitle_pagination_and_missing(self):
        result=self.tool('bili_frames',dict(video_id=VID,limit=1))
        img=next(c for c in result['content'] if c['type']=='image')
        self.assertEqual(base64.b64decode(img['data']),b'\xff\xd8\xfftest-image')
        self.assertIn('1.04',result['content'][1]['text'])
        sub=json.loads(self.tool('bili_subtitles',dict(video_id=VID,limit=1))['content'][0]['text'])
        self.assertEqual(sub['next_offset'],1);self.assertEqual(sub['segments'][0]['text'],'定义')
        (self.directory/'subtitles.json').unlink()
        missing=json.loads(self.tool('bili_subtitles',dict(video_id=VID))['content'][0]['text'])
        self.assertEqual(missing['status'],'missing');self.assertEqual(missing['segments'],[])
        for args in [dict(video_id=VID,limit=5),dict(video_id='../../secrets/mcp-token'),dict(video_id=VID,path='/etc/passwd')]:
            self.assertTrue(self.tool('bili_frames',args)['isError'])
        self.assertTrue(self.tool('bili_submit',dict(url='https://127.0.0.1/'))['isError'])
        self.assertTrue(self.tool('bili_add_frames',dict(video_id=VID,at=[2],start=0,end=1))['isError'])
        response=self.tool('bili_submit',dict(url='https://www.bilibili.com/video/'+BV+'?p=2',start=False))
        self.assertEqual(json.loads(response['content'][0]['text'])['state'],'queued')

    def test_frames_reject_symlink_and_huge_image(self):
        image=self.directory/'frames/a.jpg';image.unlink();image.symlink_to(Path(self.temp.name)/'secrets/mcp-token')
        self.assertTrue(self.tool('bili_frames',dict(video_id=VID))['isError'])
        image.unlink()
        with image.open('wb') as f: f.truncate(6*1024**2+1)
        self.assertTrue(self.tool('bili_frames',dict(video_id=VID))['isError'])

    def test_oauth_http_browser_flow(self):
        code,client,_=self.request('/oauth/register',dict(redirect_uris=['https://chatgpt.com/callback'],client_name='<script>'),token=False)
        self.assertEqual(code,201)
        params=dict(client_id=client['client_id'],redirect_uri=client['redirect_uris'][0],response_type='code',
                    resource=ORIGIN+'/mcp',scope='library',state='my-state',code_challenge=challenge('v'*43),code_challenge_method='S256')
        status,page,headers=self.request('/oauth/authorize?'+urlencode(params),method='GET',token=False,headers={'Sec-Fetch-Site':'cross-site'})
        self.assertEqual(status,200);self.assertNotIn(b'<script>',page);self.assertIn(b'&lt;script&gt;',page)
        nonce=re.search(rb'name="nonce" value="([^"]+)"',page)[1].decode()
        cookie=headers['Set-Cookie'].split(';')[0]
        self.assertIn('Secure',headers['Set-Cookie'])
        form=dict(nonce=nonce,owner=self.token,approve='yes')
        self.assertEqual(self.request('/oauth/consent',form,form=True,headers={'Cookie':cookie,'Origin':'https://evil.test'})[0],403)
        status,_,headers=self.request('/oauth/consent',form,form=True,headers={'Cookie':cookie,'Origin':ORIGIN})
        self.assertEqual(status,303)
        authcode=parse_qs(urlsplit(headers['Location']).query)['code'][0]
        req=dict(grant_type='authorization_code',client_id=client['client_id'],redirect_uri=params['redirect_uri'],resource=ORIGIN+'/mcp',code=authcode,code_verifier='v'*43)
        status,tokens,_=self.request('/oauth/token',req,token=False,form=True)
        self.assertEqual(status,200)
        self.assertEqual(self.rpc('tools/list',headers={'Authorization':'Bearer '+tokens['access_token']})[0],200)
        self.assertEqual(self.request('/oauth/token',req,token=False,form=True)[0],400)

    def test_disabled_and_requires_https_origin(self):
        with patch.dict(os.environ,{'BILI_MCP_ENABLED':'1','BILI_WEB_PUBLIC_ORIGIN':''}),self.assertRaises(LibraryError):
            AdminServer(self.temp.name,port=0)
        self.server.mcp=None
        self.assertEqual(self.rpc('tools/list')[0],404)


if __name__=='__main__': unittest.main()
