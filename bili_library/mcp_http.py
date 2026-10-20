"""Stateless Streamable HTTP JSON transport, mounted in the existing admin server.

No SSE stream/session allocation: GET and DELETE return 405 as allowed by MCP.
Long operations return persisted job IDs instead of keeping HTTP requests open.
"""
from __future__ import annotations

import html
from http.cookies import SimpleCookie
import json
import re
from urllib.parse import parse_qs, urlsplit

from .common import Library, LibraryError
from .mcp_auth import OAuthError, consent_cookie
from .mcp_tools import TOOLS, INSTRUCTIONS, call

VERSIONS=('2025-03-26','2025-06-18','2025-11-25')
PATHS={'/mcp','/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/mcp',
       '/.well-known/oauth-authorization-server','/oauth/register','/oauth/authorize',
       '/oauth/consent','/oauth/token','/oauth/revoke'}


class SourceError(PermissionError):
    """Only fixed reason codes are returned; no headers, tokens or OAuth query data."""
    pass


def single(data):
    if any(len(values)!=1 for values in data.values()): raise OAuthError('invalid_request')
    return {k:v[0] for k,v in data.items()}


def body(h,json_=False):
    if h.headers.get('Transfer-Encoding'): raise OAuthError('invalid_request')
    mime=h.headers.get_content_type()
    if mime!=('application/json' if json_ else 'application/x-www-form-urlencoded'): raise OAuthError('invalid_request')
    try: length=int(h.headers.get('Content-Length','0'))
    except ValueError: raise OAuthError('invalid_request') from None
    if not 0<length<=256*1024: raise OAuthError('invalid_request')
    h.connection.settimeout(15)
    try:
        raw=h.rfile.read(length).decode('utf-8')
        value=json.loads(raw) if json_ else single(parse_qs(raw,keep_blank_values=True,max_num_fields=30))
    except (ValueError,UnicodeError,OSError): raise OAuthError('invalid_request') from None
    if not isinstance(value,dict): raise OAuthError('invalid_request')
    return value


def empty(h,status):
    h.send_response(status);h.headers_common();h.send_header('Content-Length','0')
    if status==405: h.send_header('Allow','POST')
    h.end_headers()


def authorize_page(h,data):
    auth=h.server.mcp
    nonce,binding,name=auth.begin(data)
    esc=html.escape
    page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>授权 B站资料库</title><body><main><h1>授权个人 B站资料库</h1>
<p>客户端名称（由客户端自行声明）：{esc(name)}</p><p>返回地址：{esc(data['redirect_uri'])}</p>
<p>权限：读取资料库、字幕和实际截图；按链接提交下载、处理队列、补帧及同步收藏清单。
不包含修改 B站收藏、永久删除、读取或上传账号凭证。</p>
<p>仅在你主动连接此客户端时授权。输入服务器 secrets/mcp-token 中的独立 MCP 令牌；不是后台登录令牌或 B站 Cookie。</p>
<form method="post" action="/oauth/consent"><input type="hidden" name="nonce" value="{esc(nonce)}">
<label>MCP 令牌 <input name="owner" type="password" required autocomplete="off"></label>
<button name="approve" value="yes">授权</button><button name="approve" value="no">拒绝</button></form>
</main></body></html>'''.encode()
    # no-referrer makes native HTML form POSTs serialize Origin as null in browsers.
    # same-origin keeps the real form Origin, but discloses no referrer on external callbacks.
    h.send_response(200);h.headers_common(referrer_policy='same-origin');h.send_header('Content-Type','text/html; charset=utf-8')
    h.send_header('Content-Length',str(len(page)))
    h.send_header('Set-Cookie',f'{consent_cookie(nonce)}={binding}; Path=/oauth; HttpOnly; Secure; SameSite=Lax; Max-Age=600')
    h.end_headers();h.wfile.write(page)


def rpc(h,message):
    rid=message.get('id');method=message.get('method');params=message.get('params',{})
    def error(code,text): return dict(jsonrpc='2.0',id=rid,error=dict(code=code,message=text))
    if message.get('jsonrpc')!='2.0' or not isinstance(method,str) or not isinstance(params,dict) or ('id' in message and type(rid) not in (str,int)):
        return error(-32600,'Invalid Request')
    if 'id' not in message:
        return None  # Stateless notifications, including initialized/cancelled, allocate no state.
    if method=='initialize':
        version=params.get('protocolVersion')
        result=dict(protocolVersion=version if version in VERSIONS else '2025-06-18',capabilities=dict(tools=dict(listChanged=False),prompts=dict(listChanged=False)),
                    serverInfo=dict(name='bili-library',version='0.4.0'),instructions=INSTRUCTIONS)
    elif method=='ping': result={}
    elif method=='tools/list': result={'tools':TOOLS}
    elif method=='tools/call':
        name=params.get('name');args=params.get('arguments',{})
        lib=None
        try:
            if not isinstance(name,str) or not isinstance(args,dict): return error(-32602,'Invalid params')
            if name not in {t['name'] for t in TOOLS}: return error(-32602,'Unknown tool')
            lib=Library(h.server.home)
            result=call(h.server,lib,name,args)
        except LibraryError as exc:
            result=dict(content=[dict(type='text',text=str(exc))],isError=True)
        except Exception:
            result=dict(content=[dict(type='text',text='工具执行失败，请检查任务状态和本地媒体资料')],isError=True)
        finally:
            if lib: lib.close()
    elif method=='prompts/list':
        result={'prompts':[dict(name='course_summary',description='按真实字幕与截图详细总结课程，缺失内容不编造',arguments=[dict(name='url',description='B站单分P链接',required=True)])]}
    elif method=='prompts/get':
        if params.get('name')!='course_summary': return error(-32602,'Unknown prompt')
        args=params.get('arguments',{})
        if not isinstance(args,dict) or not isinstance(args.get('url'),str): return error(-32602,'Missing url')
        from .network import video_input
        try: url=video_input(args['url'])
        except LibraryError: return error(-32602,'Invalid Bilibili URL')
        result=dict(description='详细课程总结',messages=[dict(role='user',content=dict(type='text',text=INSTRUCTIONS+'\n请总结：'+url))])
    else: return error(-32601,'Method not found')
    return dict(jsonrpc='2.0',id=rid,result=result)


def handle(h,path,write):
    """Called before browser session auth. Returns whether this is an MCP route."""
    if path not in PATHS: return False
    auth=h.server.mcp
    if auth is None:
        h.respond({'error':'MCP未启用'},404);return True
    try:
        if h.command not in ('GET','POST','DELETE') or (h.command=='DELETE' and path!='/mcp'):
            empty(h,405);return True
        host=h.headers.get('Host')
        local={f'127.0.0.1:{h.server.server_address[1]}',f'localhost:{h.server.server_address[1]}'}
        if host not in local|{h.server.public_host}: raise SourceError('mcp_host_not_allowed')
        # OAuth browser navigation from a third party is expected; POST consent stays same-origin.
        origin=h.headers.get('Origin')
        allowed={auth.origin,'https://chatgpt.com','https://chat.openai.com'}
        # The authorization endpoint is a browser navigation from an external client,
        # not an authenticated API call. Consent still requires exact same-origin POST.
        authorization_navigation=(path=='/oauth/authorize' and h.command=='GET')
        if not authorization_navigation and origin and origin not in allowed: raise SourceError('mcp_origin_not_allowed')
        if path=='/oauth/consent':
            if not write: raise SourceError('oauth_consent_requires_post')
            if host!=h.server.public_host: raise SourceError('oauth_public_host_required')
            if origin!=auth.origin: raise SourceError('oauth_consent_origin_required')
            if h.headers.get('Sec-Fetch-Site')=='cross-site': raise SourceError('oauth_consent_cross_site')
            data=body(h);cookie=SimpleCookie();cookie.load(h.headers.get('Cookie',''))
            nonce=data.get('nonce','');cookie_name=consent_cookie(nonce)
            binding=cookie.get(cookie_name)
            location=auth.consent(nonce,binding.value if binding else '',data.get('owner',''),data.get('approve')=='yes')
            h.send_response(303);h.headers_common();h.send_header('Location',location)
            h.send_header('Set-Cookie',f'{cookie_name}=; Path=/oauth; HttpOnly; Secure; SameSite=Lax; Max-Age=0')
            h.send_header('Content-Length','0');h.end_headers();return True
        if path.startswith('/.well-known/'):
            if write: empty(h,405)
            else: h.respond(auth.metadata(resource='oauth-protected-resource' in path))
        elif path=='/oauth/authorize':
            if write: empty(h,405)
            elif host!=h.server.public_host: raise SourceError('oauth_public_host_required')
            else: authorize_page(h,single(parse_qs(urlsplit(h.path).query,keep_blank_values=True,max_num_fields=30)))
        elif path in ('/oauth/register','/oauth/token','/oauth/revoke'):
            if not write: empty(h,405)
            else:
                data=body(h,json_=path.endswith('/register'))
                fn={'/oauth/register':auth.register,'/oauth/token':auth.token,'/oauth/revoke':auth.revoke}[path]
                h.respond(fn(data),201 if path.endswith('/register') else 200)
        elif path=='/mcp':
            header=h.headers.get('Authorization','')
            if not header.startswith('Bearer ') or len(header)>1024 or not auth.authenticated(header[7:]):
                h.respond({'error':'MCP authentication required'},401,extra_headers={'WWW-Authenticate':f'Bearer resource_metadata="{auth.origin}/.well-known/oauth-protected-resource", scope="library"'})
                return True
            if not write: empty(h,405);return True
            if not {'application/json','text/event-stream'}<=set(t.strip().split(';')[0] for t in h.headers.get('Accept','').split(',')):
                h.respond({'error':'Accept must include application/json and text/event-stream'},406);return True
            if h.headers.get('MCP-Protocol-Version','2025-03-26') not in VERSIONS:
                h.respond({'error':'Unsupported MCP protocol version'},400);return True
            result=rpc(h,body(h,json_=True))
            if result is None: empty(h,202)
            else: h.respond(result)
    except SourceError as exc: h.respond({'error':'请求来源或校验不通过','code':str(exc)},403)
    except PermissionError: h.respond({'error':'请求来源或校验不通过','code':'request_permission_denied'},403)
    except OAuthError as exc:
        value={'error':exc.code}
        if exc.reason: value['code']=exc.reason
        if exc.description: value['error_description']=exc.description
        h.respond(value,400)
    except (BrokenPipeError,ConnectionResetError): pass
    except Exception: h.respond({'error':'invalid_request'},400)
    return True
