"""Ephemeral loopback fixture; never uses the real library or Bilibili credentials."""
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bili_library.common import Library
from bili_library.diagnostics import record
from bili_library.webserver import AdminServer

with tempfile.TemporaryDirectory(prefix='bili-history-browser-') as temp:
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
    os.environ.pop('BILI_WEB_PUBLIC_ORIGIN',None)
    os.environ.pop('BILI_MCP_ENABLED',None)
    home=Path(temp)/'library';lib=Library(home)
    first=lib.submit('https://www.bilibili.com/video/BV1234567890?p=1')
    lib.update_task(first['id'],state='running')
    lib.update_task(first['id'],state='failed',error='yt-dlp 下载失败 [http_403]：服务拒绝访问')
    record(lib,first['id'],'http_403','error')
    second=lib.submit('https://www.bilibili.com/video/BV1234567890?p=2')
    lib.update_task(second['id'],state='completed')
    record(lib,second['id'],'completed')
    lib.close()
    server=AdminServer(home,port=0,token='ephemeral-history-browser-token-123456')
    print(json.dumps({'url':'http://127.0.0.1:'+str(server.server_address[1]),'first':first['id'],'second':second['id']}),flush=True)
    try: server.serve_forever()
    finally: server.server_close()
