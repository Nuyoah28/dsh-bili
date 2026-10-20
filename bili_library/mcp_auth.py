"""Single-owner OAuth authorization-code + S256 server, with hashed credentials.

No Bilibili credential is ever an MCP credential. Public registration is bounded;
every grant requires a fresh browser-bound consent and the owner's MCP token.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit, urlencode

from .common import LibraryError

SCOPE='library'


def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def challenge(value): return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).rstrip(b'=').decode()


class OAuthError(LibraryError):
    def __init__(self,code): self.code=code;super().__init__(code)


class Auth:
    def __init__(self,home,origin):
        self.origin=origin;self.resource=origin+'/mcp';self.guard=threading.RLock()
        self.pending={};self.attempts=[]
        self.redirect_hosts={'chatgpt.com','chat.openai.com'}
        self.redirect_hosts.update(h.strip() for h in os.environ.get('BILI_MCP_REDIRECT_HOSTS','').split(',') if h.strip())
        path=Path(home)/'secrets'/'mcp-token'
        if not path.exists():
            with path.open('x') as f: os.chmod(path,0o600);f.write(secrets.token_urlsafe(48)+'\n')
        self.owner=path.read_text().strip()
        if len(self.owner)<32: raise LibraryError('MCP令牌至少需要32个字符')
        self.path=Path(home)/'secrets'/'mcp-oauth.sqlite3'
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY,name TEXT,redirects TEXT);
            CREATE TABLE IF NOT EXISTS codes(hash TEXT PRIMARY KEY,data TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS grants(access TEXT PRIMARY KEY,refresh TEXT UNIQUE,client TEXT,
                access_expires REAL,refresh_expires REAL,resource TEXT,scope TEXT,family TEXT);
            CREATE TABLE IF NOT EXISTS spent_refresh(hash TEXT PRIMARY KEY,family TEXT,client TEXT,expires REAL);
            ''')
            if 'family' not in {r[1] for r in db.execute('PRAGMA table_info(grants)')}:
                db.execute('ALTER TABLE grants ADD COLUMN family TEXT')
                db.execute('UPDATE grants SET family=access WHERE family IS NULL')
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        # Serialize code consumption and refresh rotation across request threads.
        with self.guard:
            db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
            try:
                with db: yield db
            finally: db.close()

    def check_owner(self,value): return hmac.compare_digest(str(value).encode(),self.owner.encode())

    def authenticated(self,token):
        if self.check_owner(token): return True
        with self.db() as db:
            row=db.execute('SELECT * FROM grants WHERE access=?',(digest(token),)).fetchone()
            return bool(row and row['access_expires']>time.time() and row['resource']==self.resource and row['scope']==SCOPE)

    def register(self,data):
        if data.get('token_endpoint_auth_method','none')!='none': raise OAuthError('invalid_client_metadata')
        if data.get('grant_types',['authorization_code','refresh_token']) not in (['authorization_code'],['authorization_code','refresh_token']):
            raise OAuthError('invalid_client_metadata')
        if data.get('response_types',['code'])!=['code']: raise OAuthError('invalid_client_metadata')
        redirects=data.get('redirect_uris')
        if not isinstance(redirects,list) or not 1<=len(redirects)<=8: raise OAuthError('invalid_redirect_uri')
        for uri in redirects:
            if not isinstance(uri,str) or len(uri)>2048: raise OAuthError('invalid_redirect_uri')
            p=urlsplit(uri)
            try: port=p.port
            except ValueError: raise OAuthError('invalid_redirect_uri') from None
            local=p.hostname in ('127.0.0.1','localhost','::1') and p.scheme=='http'
            remote=p.hostname in self.redirect_hosts and p.scheme=='https' and port in (None,443)
            if not (local or remote) or p.username is not None or p.password is not None or p.fragment or not p.path:
                raise OAuthError('invalid_redirect_uri')
        name=data.get('client_name','MCP client')
        if not isinstance(name,str) or len(name)>120: raise OAuthError('invalid_client_metadata')
        with self.db() as db:
            if db.execute('SELECT count(*) FROM clients').fetchone()[0]>=100: raise OAuthError('temporarily_unavailable')
            cid=secrets.token_urlsafe(24)
            db.execute('INSERT INTO clients VALUES(?,?,?)',(cid,name,json.dumps(redirects)))
        return dict(client_id=cid,client_name=name,redirect_uris=redirects,token_endpoint_auth_method='none',grant_types=['authorization_code','refresh_token'],response_types=['code'])

    def begin(self,data):
        with self.db() as db:
            client=db.execute('SELECT * FROM clients WHERE id=?',(data.get('client_id',''),)).fetchone()
        if not client: raise OAuthError('invalid_client')
        if data.get('redirect_uri') not in json.loads(client['redirects']): raise OAuthError('invalid_redirect_uri')
        if data.get('response_type')!='code': raise OAuthError('unsupported_response_type')
        if data.get('resource')!=self.resource: raise OAuthError('invalid_target')
        if data.get('scope',SCOPE)!=SCOPE: raise OAuthError('invalid_scope')
        if data.get('code_challenge_method')!='S256' or not re.fullmatch(r'[A-Za-z0-9_-]{43}',data.get('code_challenge','')):
            raise OAuthError('invalid_request')
        if not 1<=len(data.get('state',''))<=2048: raise OAuthError('invalid_request')
        with self.guard:
            now=time.time();self.pending={k:v for k,v in self.pending.items() if v['expires']>now}
            if len(self.pending)>=100: raise OAuthError('temporarily_unavailable')
            nonce=secrets.token_urlsafe(32);binding=secrets.token_urlsafe(32)
            self.pending[digest(nonce)]={'data':data,'binding':digest(binding),'expires':now+600}
        return nonce,binding,client['name']

    def consent(self,nonce,binding,owner,approve):
        with self.guard:
            now=time.time();self.attempts=[t for t in self.attempts if now-t<60]
            if len(self.attempts)>=10: raise OAuthError('temporarily_unavailable')
            p=self.pending.pop(digest(nonce),None)
            if not p or p['expires']<=now or not hmac.compare_digest(p['binding'],digest(binding)):
                raise OAuthError('invalid_request')
            if not self.check_owner(owner):
                self.attempts.append(now);raise OAuthError('access_denied')
            data=p['data'];params={'state':data['state'],'iss':self.origin}
            if approve:
                code=secrets.token_urlsafe(32)
                with self.db() as db:
                    db.execute('DELETE FROM codes WHERE expires<=?',(now,))
                    db.execute('INSERT INTO codes VALUES(?,?,?)',(digest(code),json.dumps(data),now+120))
                params['code']=code
            else: params['error']='access_denied'
            return data['redirect_uri']+('&' if '?' in data['redirect_uri'] else '?')+urlencode(params)

    def token(self,data):
        with self.guard: return self._token(data)

    def _token(self,data):
        if data.get('resource')!=self.resource: raise OAuthError('invalid_target')
        cid=data.get('client_id','');now=time.time()
        replay=False
        if data.get('grant_type')=='refresh_token':
            with self.db() as db:
                db.execute('DELETE FROM spent_refresh WHERE expires<=?',(now,))
                spent=db.execute('SELECT * FROM spent_refresh WHERE hash=? AND client=?',(digest(data.get('refresh_token','')),cid)).fetchone()
                if spent:
                    db.execute('DELETE FROM grants WHERE family=?',(spent['family'],));replay=True
            if replay: raise OAuthError('invalid_grant')
        with self.db() as db:
            if not db.execute('SELECT id FROM clients WHERE id=?',(cid,)).fetchone(): raise OAuthError('invalid_client')
            if data.get('grant_type')=='authorization_code':
                key=digest(data.get('code',''));row=db.execute('SELECT * FROM codes WHERE hash=?',(key,)).fetchone()
                if not row or row['expires']<=now: raise OAuthError('invalid_grant')
                grant=json.loads(row['data']);verifier=data.get('code_verifier','')
                if (grant['client_id']!=cid or grant['resource']!=self.resource or grant['redirect_uri']!=data.get('redirect_uri')
                    or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}',verifier)
                    or not hmac.compare_digest(challenge(verifier),grant['code_challenge'])):
                    raise OAuthError('invalid_grant')
                db.execute('DELETE FROM codes WHERE hash=?',(key,))
                family=secrets.token_urlsafe(24)
            elif data.get('grant_type')=='refresh_token':
                key=digest(data.get('refresh_token',''));row=db.execute('SELECT * FROM grants WHERE refresh=?',(key,)).fetchone()
                if not row or row['client']!=cid or row['refresh_expires']<=now or row['resource']!=self.resource:
                    raise OAuthError('invalid_grant')
                if data.get('scope',SCOPE)!=SCOPE: raise OAuthError('invalid_scope')
                family=row['family']
                if db.execute('SELECT count(*) FROM spent_refresh').fetchone()[0]>=10000: raise OAuthError('temporarily_unavailable')
                db.execute('INSERT INTO spent_refresh VALUES(?,?,?,?)',(key,family,cid,row['refresh_expires']))
                db.execute('DELETE FROM grants WHERE refresh=?',(key,))
            else: raise OAuthError('unsupported_grant_type')
            db.execute('DELETE FROM grants WHERE refresh_expires<=?',(now,))
            if db.execute('SELECT count(*) FROM grants').fetchone()[0]>=500: raise OAuthError('temporarily_unavailable')
            access=secrets.token_urlsafe(48);refresh=secrets.token_urlsafe(48)
            db.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?,?)',(digest(access),digest(refresh),cid,now+3600,now+30*86400,self.resource,SCOPE,family))
        return dict(access_token=access,token_type='Bearer',expires_in=3600,refresh_token=refresh,scope=SCOPE)

    def revoke(self,data):
        with self.db() as db:
            key=digest(data.get('token',''))
            db.execute('DELETE FROM grants WHERE (access=? OR refresh=?) AND client=?',(key,key,data.get('client_id','')))
        return {}

    def metadata(self,resource=False):
        if resource:
            return dict(resource=self.resource,authorization_servers=[self.origin],scopes_supported=[SCOPE],bearer_methods_supported=['header'])
        return dict(issuer=self.origin,authorization_endpoint=self.origin+'/oauth/authorize',
                    token_endpoint=self.origin+'/oauth/token',registration_endpoint=self.origin+'/oauth/register',
                    revocation_endpoint=self.origin+'/oauth/revoke',response_types_supported=['code'],
                    grant_types_supported=['authorization_code','refresh_token'],token_endpoint_auth_methods_supported=['none'],
                    code_challenge_methods_supported=['S256'],scopes_supported=[SCOPE],authorization_response_iss_parameter_supported=True)
