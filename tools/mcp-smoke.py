#!/usr/bin/env python3
"""Dependency-free HTTPS MCP smoke check; credentials are never printed."""
import argparse
import base64
import getpass
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--url',default='https://bili.alaner.space/mcp')
    p.add_argument('--token-file',type=Path,help='服务器本机可指定 secrets/mcp-token，或省略后在密码提示中输入')
    p.add_argument('--video-id',help='可选：验证此视频的实际JPEG图片内容')
    args=p.parse_args()
    target=urlsplit(args.url)
    if target.scheme!='https' or target.path!='/mcp' or target.username or target.password or target.query or target.fragment:
        p.error('URL必须是完整HTTPS /mcp地址，不含凭证或查询参数')
    token=args.token_file.read_text().strip() if args.token_file else getpass.getpass('独立 MCP 令牌：')
    opener=build_opener(NoRedirect())
    def rpc(method,params=None,rid=1):
        message=dict(jsonrpc='2.0',method=method,params=params or {})
        if rid is not None: message['id']=rid
        req=Request(args.url,json.dumps(message).encode(),headers={'Content-Type':'application/json',
            'Accept':'application/json, text/event-stream','Authorization':'Bearer '+token,'MCP-Protocol-Version':'2025-06-18'})
        with opener.open(req,timeout=30) as response:
            raw=response.read(12*1024**2)
            if rid is None:
                if response.status!=202: raise ValueError('notification status')
                return None
            value=json.loads(raw)
        if value.get('error') or value.get('result',{}).get('isError'): raise ValueError('RPC failed')
        return value['result']
    initialized=rpc('initialize',dict(protocolVersion='2025-06-18',capabilities={},clientInfo=dict(name='bili-smoke',version='1')))
    rpc('notifications/initialized',rid=None)
    tools=rpc('tools/list')['tools']
    print('初始化成功：'+initialized['serverInfo']['name'])
    print('工具数量：'+str(len(tools)))
    if args.video_id:
        result=rpc('tools/call',dict(name='bili_frames',arguments=dict(video_id=args.video_id,limit=1)))
        images=[c for c in result['content'] if c['type']=='image']
        if not images or not base64.b64decode(images[0]['data'],validate=True).startswith(b'\xff\xd8\xff'):
            raise ValueError('no JPEG')
        print('实际JPEG图片内容验证成功')
    print('MCP HTTPS检查通过（未提交下载、未修改B站账号）')


if __name__=='__main__':
    try: main()
    except Exception:
        print('MCP检查失败，请核对HTTPS地址、独立MCP令牌、服务状态及视频ID；凭证未输出。',file=sys.stderr)
        sys.exit(1)
