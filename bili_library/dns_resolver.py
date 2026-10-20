"""Optional service-local IPv4 DoH resolver with fixed, TLS-verified bootstrap.

This module never inherits proxy variables, follows redirects, sends cookies, or
uses system DNS to reach its resolver. The destination IP policy is unchanged.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlencode

from .common import LibraryError

BOOTSTRAP=('1.1.1.1','1.0.0.1')
RESOLVER_HOST='cloudflare-dns.com'
_cache={}
_lock=threading.Lock()


def mode():
    value=os.environ.get('BILI_DNS_MODE','system')
    if value not in ('system','cloudflare'):
        raise LibraryError('BILI_DNS_MODE仅允许system或cloudflare')
    return value


def public_ip(value):
    ip=ipaddress.ip_address(value.split('%')[0])
    return bool(ip.is_global and not ip.is_multicast and not ip.is_reserved
                and not (getattr(ip,'ipv4_mapped',None) and not ip.ipv4_mapped.is_global))


class BootstrapHTTPS(http.client.HTTPSConnection):
    def __init__(self,address):
        super().__init__(RESOLVER_HOST,timeout=5,context=ssl.create_default_context())
        self.address=address

    def connect(self):
        if self.address not in BOOTSTRAP or not public_ip(self.address):
            raise LibraryError('DoH引导地址无效')
        raw=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        raw.settimeout(5)
        try:
            raw.connect((self.address,443))
            self.sock=self._context.wrap_socket(raw,server_hostname=RESOLVER_HOST)
        except Exception:
            raw.close();raise


def query(host):
    for address in BOOTSTRAP:
        conn=BootstrapHTTPS(address)
        try:
            conn.request('GET','/dns-query?'+urlencode(dict(name=host,type='A')),
                         headers={'Accept':'application/dns-json'})
            response=conn.getresponse()
            if response.status!=200: continue  # Never follow a resolver redirect.
            content_type=(response.getheader('Content-Type') or '').split(';')[0].strip()
            if content_type not in ('application/dns-json','application/json'): continue
            raw=response.read(65537)
            if len(raw)>65536: continue
            return json.loads(raw)
        except (OSError,http.client.HTTPException,ValueError):
            continue
        finally: conn.close()
    raise LibraryError('Cloudflare DoH请求失败；需要服务器能以HTTPS直连1.1.1.1或1.0.0.1，不会回退到系统DNS')


def dns_name(value):
    if not isinstance(value,str): raise LibraryError('DoH域名格式无效')
    name=value.lower().rstrip('.')
    if len(name)>253 or not re.fullmatch(r'[a-z0-9_-]+(?:\.[a-z0-9_-]+)*',name):
        raise LibraryError('DoH域名格式无效')
    return name


def answers(host,data):
    if not isinstance(data,dict) or type(data.get('Status')) is not int or data.get('Status')!=0 or data.get('TC'):
        raise LibraryError('DoH解析失败或返回了截断响应')
    questions=data.get('Question');records=data.get('Answer',[])
    if (not isinstance(questions,list) or len(questions)!=1 or not isinstance(questions[0],dict)
        or dns_name(questions[0].get('name'))!=host or type(questions[0].get('type')) is not int or questions[0].get('type')!=1
        or not isinstance(records,list) or len(records)>128):
        raise LibraryError('DoH响应与查询不匹配')
    if any(not isinstance(r,dict) for r in records): raise LibraryError('DoH记录无效')
    reachable={host};ttls=[]
    # Only accept A records belonging to the queried name or its actual CNAME chain.
    for _ in range(16):
        found=False
        for record in records:
            if record.get('type')==5 and dns_name(record.get('name')) in reachable:
                target=dns_name(record.get('data'))
                if target not in reachable:
                    reachable.add(target);ttls.append(record.get('TTL',0));found=True
        if not found: break
    addresses=[]
    for record in records:
        if record.get('type')==1 and dns_name(record.get('name')) in reachable:
            try:
                if not isinstance(record.get('data'),str): raise ValueError()
                ip=ipaddress.IPv4Address(record.get('data'))
                if not public_ip(str(ip)): raise ValueError()
            except (ValueError,TypeError):
                raise LibraryError('DoH返回非公网或无效IPv4地址，已拒绝连接') from None
            addresses.append(str(ip));ttls.append(record.get('TTL',0))
    if not addresses: raise LibraryError('DoH没有返回该域名可用的公网IPv4地址')
    if any(type(ttl) is not int or ttl<0 for ttl in ttls): raise LibraryError('DoH记录TTL无效')
    return tuple(dict.fromkeys(addresses)),min(300,min(ttls))


def resolve(host,port=443,type=socket.SOCK_STREAM):
    host=dns_name(host)
    if port!=443 or type!=socket.SOCK_STREAM: raise LibraryError('DoH仅供资料库HTTPS连接使用')
    # Network boundary still owns the hostname allowlist, including direct helper use.
    from .network import validate_url
    validate_url('https://'+host)
    with _lock:
        cached=_cache.get(host)
        if cached and cached[0]>time.monotonic(): ips=cached[1]
        else:
            ips,ttl=answers(host,query(host))
            if len(_cache)>=256:
                oldest=min(_cache,key=lambda key:_cache[key][0]);_cache.pop(oldest)
            if ttl: _cache[host]=(time.monotonic()+ttl,ips)
    return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(ip,port)) for ip in ips]
