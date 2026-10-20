"""Optional interoperability test: pip install -r requirements-mcp-test.txt."""
import asyncio
import base64
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from bili_library.common import Library, read_json
from bili_library.webserver import AdminServer


@unittest.skipUnless(importlib.util.find_spec('mcp'),'optional official MCP SDK not installed')
class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_oauth_discovery_registration_and_consent(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata
        from mcp.client.streamable_http import streamable_http_client
        origin='https://bili.example.com'
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ,{'BILI_MCP_ENABLED':'1','BILI_WEB_PUBLIC_ORIGIN':origin}):
                server=AdminServer(temp,port=0,token='different-web-token-at-least-32-chars')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            class LocalTransport(httpx.AsyncBaseTransport):
                def __init__(self): self.inner=httpx.AsyncHTTPTransport()
                async def handle_async_request(self,request):
                    if request.url.host!='bili.example.com': raise AssertionError('unexpected outbound request')
                    local=httpx.Request(request.method,request.url.copy_with(scheme='http',host='127.0.0.1',port=server.server_address[1]),
                                        headers=request.headers,stream=request.stream,extensions=request.extensions)
                    return await self.inner.handle_async_request(local)
                async def aclose(self): await self.inner.aclose()
            class Storage:
                tokens=None;client=None
                async def get_tokens(self): return self.tokens
                async def set_tokens(self,tokens): self.tokens=tokens
                async def get_client_info(self): return self.client
                async def set_client_info(self,client_info): self.client=client_info
            storage=Storage();callback={}
            async def browser(url):
                async with httpx.AsyncClient(transport=LocalTransport(),trust_env=False) as client:
                    response=await client.get(url)
                    self.assertEqual(response.status_code,200)
                    nonce=re.search(r'name="nonce" value="([^"]+)"',response.text)[1]
                    # Client transport uses plaintext loopback in this test; send the Secure cookie explicitly.
                    cookie=response.headers['set-cookie'].split(';')[0]
                    consent=await client.post(origin+'/oauth/consent',data=dict(nonce=nonce,owner=server.mcp.owner,approve='yes'),headers={'Origin':origin,'Cookie':cookie})
                    self.assertEqual(consent.status_code,303)
                    callback.update(parse_qs(urlsplit(consent.headers['location']).query))
            async def receive(): return callback['code'][0],callback['state'][0]
            try:
                auth=OAuthClientProvider(server_url=origin+'/mcp',client_metadata=OAuthClientMetadata(
                    client_name='official-sdk',redirect_uris=['http://127.0.0.1:12345/callback'],
                    grant_types=['authorization_code','refresh_token'],response_types=['code'],token_endpoint_auth_method='none'),
                    storage=storage,redirect_handler=browser,callback_handler=receive)
                async with httpx.AsyncClient(transport=LocalTransport(),auth=auth,trust_env=False) as client:
                    async with streamable_http_client(origin+'/mcp',http_client=client) as (read,write,_):
                        async with ClientSession(read,write) as session:
                            await session.initialize()
                            self.assertEqual(len((await session.list_tools()).tools),12)
                self.assertTrue(server.mcp.authenticated(storage.tokens.access_token))
                self.assertEqual(storage.client.token_endpoint_auth_method,'none')
            finally:
                await asyncio.to_thread(server.shutdown);server.server_close();thread.join(timeout=3)

    async def test_official_streamable_client(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ,{'BILI_MCP_ENABLED':'1','BILI_WEB_PUBLIC_ORIGIN':'https://bili.example.com'}):
                server=AdminServer(temp,port=0,token='different-web-token-at-least-32-chars')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                fixture=os.environ.get('BILI_MCP_MEDIA_FIXTURE')
                if fixture:
                    source=Path(fixture);meta=read_json(source/'metadata.json')
                    lib=Library(temp)
                    with lib.db:
                        lib.db.execute('INSERT INTO videos('+','.join(meta)+') VALUES('+','.join('?' for _ in meta)+')',tuple(meta.values()))
                    shutil.copytree(source,lib.home/'videos'/meta['id']);lib.close()
                async with httpx.AsyncClient(headers={'Host':'bili.example.com','Authorization':'Bearer '+server.mcp.owner},trust_env=False) as client:
                    async with streamable_http_client(f'http://127.0.0.1:{server.server_address[1]}/mcp',http_client=client) as (read,write,_):
                        async with ClientSession(read,write) as session:
                            initialized=await session.initialize()
                            self.assertEqual(initialized.serverInfo.name,'bili-library')
                            tools=await session.list_tools();self.assertEqual(len(tools.tools),12)
                            prompts=await session.list_prompts();self.assertEqual(prompts.prompts[0].name,'course_summary')
                            await session.get_prompt('course_summary',{'url':'https://www.bilibili.com/video/BV1234567890'})
                            result=await session.call_tool('bili_videos',{})
                            self.assertFalse(result.isError)
                            if fixture:
                                frames=await session.call_tool('bili_frames',{'video_id':meta['id'],'limit':1})
                                images=[c for c in frames.content if c.type=='image']
                                self.assertEqual(len(images),1)
                                index=read_json(source/'frames/index.json')
                                self.assertEqual(base64.b64decode(images[0].data),(source/index[0]['file']).read_bytes())
                                submitted=await session.call_tool('bili_submit',{'url':meta['source']})
                                task=json.loads(submitted.content[0].text)
                                for _ in range(100):
                                    status=await session.call_tool('bili_tasks',{'task_id':task['id']})
                                    current=json.loads(status.content[0].text)
                                    if current['state'] in ('completed','failed','cancelled'): break
                                    await asyncio.sleep(.1)
                                self.assertEqual((current['state'],current['stage']),('completed','cached'))
                            invalid=await session.call_tool('bili_frames',{'video_id':'../../secrets/mcp-token'})
                            self.assertTrue(invalid.isError)
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close();thread.join(timeout=3)


if __name__=='__main__': unittest.main()
