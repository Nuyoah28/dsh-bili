"""Authenticated loopback-only admin UI. No public binding or automatic scheduling."""
from __future__ import annotations

import hashlib
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from .common import Library, LibraryError, read_json, write_json
from . import management as m

ASSETS=Path(__file__).parent/'web'


class BusyError(LibraryError): pass


class Controller:
    def __init__(self,home):
        self.home=Path(home);self.guard=threading.Lock();self.thread=None;self.kind=None;self.stop_event=threading.Event()
        # A separate server lock prevents two controllers from declaring each other's jobs interrupted.
        self.server_lock=(self.home/'web.lock').open('a')
        import fcntl
        try: fcntl.flock(self.server_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.server_lock.close();raise LibraryError('这个资料库已有网页后台在运行') from None
        lib=Library(self.home)
        with lib.db:
            lib.db.execute("UPDATE web_jobs SET state='failed',error='后台上次被中断，请核对后重试' WHERE state='running'")
            lib.db.execute("UPDATE remote_plans SET state='uncertain' WHERE state='executing'")
        lib.close()

    def start(self,kind,payload):
        with self.guard:
            if self.thread and self.thread.is_alive(): raise BusyError('已有后台操作正在执行，请稍后再试')
            self.kind=kind
            jid=uuid.uuid4().hex
            lib=Library(self.home)
            with lib.db: lib.db.execute('INSERT INTO web_jobs VALUES(?,?,?,?,?,?,?)',(jid,kind,'running',None,None,time.time(),time.time()))
            lib.close()
            def run():
                current=Library(self.home)
                try:
                    result=perform(current,kind,payload,self.stop_event)
                    good=not isinstance(result,dict) or result.get('ok') is not False
                    with current.db: current.db.execute('UPDATE web_jobs SET state=?,result=?,updated=? WHERE id=?',
                         ('completed' if good else 'failed',json.dumps(result,ensure_ascii=False),time.time(),jid))
                except Exception as error:
                    message=str(error) if isinstance(error,LibraryError) else '操作失败，请检查配置、登录状态及本地文件'
                    with current.db: current.db.execute("UPDATE web_jobs SET state='failed',error=?,updated=? WHERE id=?",(message,time.time(),jid))
                finally: current.close()
            self.thread=threading.Thread(target=run,daemon=True);self.thread.start()
            return {'job_id':jid,'state':'running'}

    def close(self):
        # Let the worker cancel its own process group. Never release its lock while it runs.
        if self.thread and self.thread.is_alive():
            if self.kind=='worker': self.stop_event.set()
            self.thread.join(timeout=5)
        self.server_lock.close()


def perform(lib,kind,payload,stop_event=None):
    from .__main__ import execute,parser
    from .bilibili import Network,load_cookies,export_favorites,folders
    if kind=='worker':
        from .jobs import worker
        return worker(lib,stop_event=stop_event)
    if kind=='sync':
        with lib.lock():
            data=export_favorites(Network(load_cookies(lib)),str(payload.get('folder','')))
            lib.check_space(len(json.dumps(data).encode()))
            result=m.import_favorites(lib,data)
            write_json(lib.home/'exports'/f'favorites-{data["folder_id"]}-{uuid.uuid4().hex[:8]}.json',data)
            return result
    if kind=='owned-folders':
        from .remote_favorites import identity
        with lib.lock():
            uid,owned=identity(Network(load_cookies(lib)))
            return {'uid':uid,'folders':list(owned.values())}
    if kind in ('frames','subtitles'):
        if kind=='frames':
            argv=['frames-add',str(payload.get('id',''))]
            if payload.get('at'):
                for value in payload['at']: argv+=['--at',str(value)]
            else: argv+=['--from',str(payload.get('from','')),'--to',str(payload.get('to','')),'--every',str(payload.get('every',10))]
        else: argv=['subtitles',str(payload.get('id','')),'--refresh']
        # Parse only server-generated command arguments, never pass arbitrary shell or filesystem input.
        try: args=parser().parse_args(argv)
        except SystemExit: raise LibraryError('补帧或字幕参数无效') from None
        return execute(args,lib)
    if kind=='remote-preview':
        from .remote_favorites import preview
        return preview(lib,payload.get('keys'),payload.get('action'),payload.get('target'))
    if kind=='remote-commit':
        from .remote_favorites import commit
        return commit(lib,str(payload.get('plan_id','')),str(payload.get('confirmation','')))
    raise LibraryError('不支持的后台操作')


class AdminServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,home,port=8765,token=None,remote=False):
        self.home=Path(home);self.sessions={};self.sessions_lock=threading.Lock();self.attempts={}
        self.remote=remote
        self.public_origin=os.environ.get('BILI_WEB_PUBLIC_ORIGIN','').strip()
        self.public_host=None
        if self.public_origin:
            parsed=urlsplit(self.public_origin)
            try: port_=parsed.port
            except ValueError: raise LibraryError('BILI_WEB_PUBLIC_ORIGIN 端口无效') from None
            if (parsed.scheme!='https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?',parsed.netloc)
                or port_==0):
                raise LibraryError('BILI_WEB_PUBLIC_ORIGIN 必须是完整 HTTPS 来源，不含路径，例如 https://bili.example.com')
            self.public_host=parsed.netloc
        lib=Library(self.home);lib.close()
        token_path=self.home/'secrets'/'web-token'
        token=token or os.environ.get('BILI_WEB_TOKEN')
        if token is None:
            if token_path.exists(): token=token_path.read_text().strip()
            else:
                token=secrets.token_urlsafe(32)
                with token_path.open('x',encoding='utf-8') as f: os.chmod(token_path,0o600);f.write(token+'\n')
        if len(token)<24: raise LibraryError('后台访问令牌至少需要 24 个字符')
        self.token=token;self.controller=None
        try:
            self.controller=Controller(self.home)
            super().__init__(('127.0.0.1',port),Handler)
        except Exception:
            if self.controller: self.controller.close()
            raise

    def server_close(self):
        super().server_close()
        if self.controller: self.controller.close();self.controller=None


class Handler(BaseHTTPRequestHandler):
    server_version='BiliLibrary'
    def log_message(self,*args): pass  # no request/query/cookie logging

    def headers_common(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def respond(self,value,status=200,cookie=None):
        body=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status);self.headers_common()
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(body)

    def origin_check(self):
        port=self.server.server_address[1]
        allowed={f'127.0.0.1:{port}',f'localhost:{port}'}
        host=self.headers.get('Host')
        if self.server.public_host and host==self.server.public_host:
            origins={self.server.public_origin}
        elif host in allowed:
            origins={'http://'+host}
        else: raise PermissionError('不允许的 Host')
        origin=self.headers.get('Origin')
        if origin and origin not in origins: raise PermissionError('不允许跨站请求')
        if self.headers.get('Sec-Fetch-Site')=='cross-site': raise PermissionError('不允许跨站请求')

    def session_cookie(self,value,max_age):
        secure='; Secure' if self.headers.get('Host')==self.server.public_host else ''
        return f'bili_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}'+secure

    def session(self):
        cookie=SimpleCookie()
        try: cookie.load(self.headers.get('Cookie',''))
        except Exception: return None
        sid=cookie.get('bili_session')
        if not sid: return None
        with self.server.sessions_lock:
            value=self.server.sessions.get(sid.value)
            if not value or value['expires']<time.time(): return None
            return value

    def body(self):
        if self.headers.get('Transfer-Encoding'): raise LibraryError('不支持分块请求体')
        if self.headers.get_content_type()!='application/json': raise LibraryError('请求必须是 JSON')
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: raise LibraryError('无效请求长度') from None
        if not 0<length<=8*1024**2: raise LibraryError('请求体大小应在 1 字节～8 MiB')
        self.connection.settimeout(15)
        try: value=json.loads(self.rfile.read(length))
        except (ValueError,OSError): raise LibraryError('请求 JSON 无效或读取超时') from None
        if not isinstance(value,dict): raise LibraryError('请求体必须为 JSON 对象')
        return value

    def login(self,data):
        ip=self.client_address[0];now=time.time()
        with self.server.sessions_lock:
            attempts=[t for t in self.server.attempts.get(ip,[]) if now-t<60]
            if len(attempts)>=10:
                self.respond({'error':'尝试次数过多，请一分钟后重试'},429);return
            if not hmac.compare_digest(str(data.get('token','')).encode(),self.server.token.encode()):
                self.server.attempts[ip]=attempts+[now]
                self.respond({'error':'访问令牌不正确'},401);return
            sid=secrets.token_urlsafe(32)
            self.server.sessions={k:v for k,v in self.server.sessions.items() if v['expires']>now}
            if len(self.server.sessions)>100: self.server.sessions.clear()
            self.server.sessions[sid]={'csrf':secrets.token_urlsafe(32),'expires':now+8*3600}
        self.respond({'ok':True},cookie=self.session_cookie(sid,28800))

    def file(self,path,mime=None,attachment=False):
        if not path.is_file() or path.is_symlink(): raise LibraryError('文件不存在')
        total=path.stat().st_size;start=0;end=total-1
        range_=self.headers.get('Range')
        if range_:
            match=re.fullmatch(r'bytes=(\d+)-(\d*)',range_)
            if not match:
                self.respond({'error':'不支持的 Range'},416);return
            start=int(match[1]);end=min(int(match[2]),end) if match[2] else end
            if start>end or start>=total:
                self.respond({'error':'Range 超出文件范围'},416);return
        self.send_response(206 if range_ else 200);self.headers_common()
        self.send_header('Content-Type',mime or mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
        if range_: self.send_header('Content-Range',f'bytes {start}-{end}/{total}')
        if attachment: self.send_header('Content-Disposition','attachment; filename="'+path.name+'"')
        self.end_headers()
        with path.open('rb') as f:
            f.seek(start);remaining=end-start+1
            while remaining>0:
                chunk=f.read(min(65536,remaining))
                if not chunk: break
                self.wfile.write(chunk);remaining-=len(chunk)

    def do_GET(self): self.dispatch(False)
    def do_POST(self): self.dispatch(True)

    def dispatch(self,write):
        lib=None
        try:
            self.origin_check()
            path=urlsplit(self.path).path
            if not write and path in ('/','/app.js','/style.css'):
                self.file(ASSETS/('index.html' if path=='/' else path[1:]));return
            if write and path=='/api/login': self.login(self.body());return
            session=self.session()
            if not session: self.respond({'error':'请先登录后台'},401);return
            if write and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']):
                raise PermissionError('请求校验失败，请重新登录')
            lib=Library(self.server.home)
            query=parse_qs(urlsplit(self.path).query)
            if write:
                result=self.mutate(lib,path,self.body())
                if result is not None: self.respond(result)
                return
            if path=='/api/session': self.respond({'csrf':session['csrf'],'remote_enabled':self.server.remote});return
            if path=='/api/state':
                jobs=lib.rows('SELECT * FROM web_jobs ORDER BY created DESC LIMIT 20')
                for job in jobs: job['result']=json.loads(job['result']) if job['result'] else None
                self.respond({'videos':lib.rows('SELECT * FROM videos ORDER BY rowid DESC'),
                              'tasks':lib.rows('SELECT * FROM tasks ORDER BY created DESC LIMIT 200'),
                              'folders':lib.rows('SELECT f.*, (SELECT count(*) FROM favorite_items i WHERE i.folder_id=f.id AND i.present=1) count FROM favorite_folders f ORDER BY title'),
                              'jobs':jobs,'usage_bytes':lib.usage(),'config':lib.config,
                              'trash':[{'id':r['id'],'deleted':r['deleted'],'video':json.loads(r['video'])} for r in lib.rows('SELECT * FROM trash ORDER BY deleted DESC')]});return
            if path=='/api/favorites': self.respond({'items':m.favorite_items(lib,query.get('folder',[None])[0])});return
            if path=='/api/rules': self.respond({'rules':m.rules(lib)});return
            if path=='/api/config':
                from .__main__ import doctor
                self.respond({'config':lib.config,'doctor':doctor(lib.home),'remote_enabled':self.server.remote,
                    'credentials_present':(lib.home/'secrets/bilibili.json').exists() or (lib.home.parent/'bili_credential.json').exists()});return
            if path.startswith('/api/video/'):
                vid=unquote(path[len('/api/video/'):]);video=lib.video(vid)
                directory=lib.home/'videos'/vid
                self.respond({'video':video,'subtitles':read_json(directory/'subtitles.json',{'status':'missing','segments':[]}),
                              'frames':read_json(directory/'frames/index.json',[])});return
            match=re.fullmatch(r'/media/([^/]+)/(video|frame)/?([^/]*)',path)
            if match:
                video=lib.video(match[1]);directory=lib.home/'videos'/video['id']
                if match[2]=='video':
                    if not video['media']: raise LibraryError('视频尚未下载')
                    target=directory/video['media']
                    if target.resolve().parent!=directory.resolve(): raise LibraryError('媒体路径异常')
                    self.file(target,attachment=True);return
                index=read_json(directory/'frames/index.json',[])
                item=next((i for i in index if Path(i['file']).name==match[3]),None)
                if not item: raise LibraryError('截图不存在')
                target=directory/item['file']
                if target.resolve().parent!=(directory/'frames').resolve(): raise LibraryError('截图路径异常')
                self.file(target,'image/jpeg');return
            if path=='/api/export':
                from .__main__ import export_text
                fmt=query.get('format',['json'])[0]
                if fmt not in ('json','csv','markdown'): raise LibraryError('导出格式无效')
                items=m.favorite_items(lib,query.get('folder',[None])[0])
                category=query.get('category',[''])[0]
                if category: items=[i for i in items if i['category']==category]
                value={'title':'整理后的收藏夹','items':items,'count':len(items)}
                # CSV/Markdown include annotations for local organization.
                if fmt=='json':
                    groups=[]
                    for folder in lib.rows('SELECT * FROM favorite_folders ORDER BY id'):
                        selected_items=[i for i in items if i['folder_id']==folder['id']]
                        if selected_items: groups.append({'folder_id':folder['id'],'title':folder['title'],'items':selected_items,'count':len(selected_items)})
                    value.update(format='bili-library-organized-v1',folders=groups)
                    value.pop('items',None)
                text=export_text(value,fmt)
                if fmt=='csv':
                    import csv,io
                    f=io.StringIO();writer=csv.writer(f);writer.writerow(['title','url','category','tags','note','status'])
                    for i in items:
                        row=[i['title'],i['url'] or '',i['category'],','.join(i['tags']),i['note'],i['status']]
                        writer.writerow(["'"+s if s.lstrip().startswith(('=','+','-','@')) else s for s in row])
                    text='\ufeff'+f.getvalue()
                body=text.encode('utf-8');self.send_response(200);self.headers_common()
                self.send_header('Content-Type','application/json; charset=utf-8' if fmt=='json' else 'text/plain; charset=utf-8')
                self.send_header('Content-Disposition','attachment; filename="organized-favorites.'+{'json':'json','csv':'csv','markdown':'md'}[fmt]+'"')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            self.respond({'error':'接口不存在'},404)
        except PermissionError:
            self.respond({'error':'请求来源或校验不通过'},403)
        except BusyError as error: self.respond({'error':str(error)},409)
        except LibraryError as error: self.respond({'error':str(error)},400)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception: self.respond({'error':'操作失败，请检查参数、文件权限或运行状态'},500)
        finally:
            if lib: lib.close()

    def mutate(self,lib,path,data):
        if path=='/api/logout':
            # Invalidate the current session, not just the browser cookie.
            cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''))
            with self.server.sessions_lock: self.server.sessions.pop(cookie['bili_session'].value,None)
            self.respond({'ok':True},cookie=self.session_cookie('',0));return None
        if path=='/api/tasks': return lib.submit(str(data.get('url','')))
        if path=='/api/task-action':
            from .__main__ import parser,execute
            if data.get('action') not in ('retry','cancel'): raise LibraryError('任务操作无效')
            return execute(parser().parse_args([data['action'],str(data.get('id',''))]),lib)
        if path=='/api/delete-video': return m.delete_video(lib,str(data.get('id','')))
        if path=='/api/trash':
            if data.get('action') not in ('restore','purge'): raise LibraryError('回收站操作无效')
            return m.trash_action(lib,str(data.get('id','')),data['action']=='restore')
        if path=='/api/config': return {'config':m.save_config(lib,data.get('config'))}
        if path=='/api/rules': return {'rules':m.save_rules(lib,data.get('rules'))}
        if path=='/api/favorites/import': return m.import_favorites(lib,data.get('data'))
        if path=='/api/favorites/annotate': return m.annotate(lib,data.get('keys'),data.get('patch'))
        if path=='/api/favorites/classify': return m.classify(lib,data.get('keys'),data.get('apply') is True)
        if path=='/api/credentials':
            from .bilibili import import_credentials
            # Explicit upload only; arbitrary local filesystem paths cannot be read through this API.
            value=data.get('data')
            if not isinstance(value,dict): raise LibraryError('请上传凭证 JSON 文件')
            temp=lib.home/'secrets'/('upload-'+uuid.uuid4().hex+'.json')
            try: write_json(temp,value);return import_credentials(lib,temp)
            finally: temp.unlink(missing_ok=True)
        if path=='/api/job':
            kind=data.get('kind')
            if kind not in ('worker','sync','owned-folders','frames','subtitles','remote-preview','remote-commit'): raise LibraryError('后台任务类型无效')
            if kind.startswith('remote-') and not self.server.remote: raise LibraryError('远程修改未启用；启动前设置 BILI_REMOTE_WRITES=1')
            return self.server.controller.start(kind,data.get('payload') or {})
        raise LibraryError('接口不存在')


def serve(home,port=8765):
    if not 1<=port<=65535: raise LibraryError('端口必须为 1～65535')
    server=AdminServer(home,port,remote=os.environ.get('BILI_REMOTE_WRITES')=='1')
    print(f'B站资料库后台：http://127.0.0.1:{port}',flush=True)
    print('访问令牌：读取数据目录 secrets/web-token，或使用已设置的 BILI_WEB_TOKEN；令牌不写入日志。',flush=True)
    import signal
    previous=signal.getsignal(signal.SIGTERM)
    def terminate(_signum,_frame): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,terminate)
    try: server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM,previous)
