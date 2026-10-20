"""Local-only HTTPS fixture for browser_mcp.cjs; ephemeral credentials/certificate."""
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import json
import signal

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bili_library.webserver import AdminServer


with tempfile.TemporaryDirectory(prefix='bili-browser-oauth-') as temp:
    signal.signal(signal.SIGTERM,lambda *_: sys.exit(0))
    os.umask(0o077)
    directory=Path(temp);cert=directory/'cert.pem';key=directory/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),
                    '-out',str(cert),'-days','1','-subj','/CN=127.0.0.1'],check=True,capture_output=True)
    os.environ.update(BILI_MCP_ENABLED='1',BILI_WEB_PUBLIC_ORIGIN='https://127.0.0.1:1')
    server=AdminServer(directory/'library',port=0,token='ephemeral-browser-fixture-web-token')
    origin='https://127.0.0.1:'+str(server.server_address[1])
    server.public_origin=origin;server.public_host=origin.removeprefix('https://')
    server.mcp.origin=origin;server.mcp.resource=origin+'/mcp'
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    # Pipe consumed by the test harness, never displayed in test output or persisted.
    print(json.dumps(dict(origin=origin,owner=server.mcp.owner)),flush=True)
    try: server.serve_forever()
    finally: server.server_close()
