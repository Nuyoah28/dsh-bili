import copy
import io
import json
import os
import socket
import ssl
import unittest
from unittest.mock import Mock,patch

from bili_library.common import LibraryError
from bili_library import dns_resolver as dns
from bili_library.network import public_addresses,connect_public

HOST='api.bilibili.com'
ANSWER=dict(Status=0,TC=False,Question=[dict(name=HOST+'.',type=1)],
            Answer=[dict(name=HOST,type=1,TTL=60,data='1.1.1.1')])


class DNSTests(unittest.TestCase):
    def setUp(self):
        with dns._lock: dns._cache.clear()

    def test_system_mode_still_rejects_fake_ip(self):
        fake=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('198.18.0.18',443))]
        with patch.dict(os.environ,{'BILI_DNS_MODE':'system'}),patch('socket.getaddrinfo',return_value=fake),self.assertRaises(LibraryError):
            public_addresses(HOST)
        with patch.dict(os.environ,{'BILI_DNS_MODE':'disabled'}),self.assertRaises(LibraryError):
            public_addresses(HOST)

    def test_cloudflare_skips_system_dns_and_pins_the_result(self):
        with patch.dict(os.environ,{'BILI_DNS_MODE':'cloudflare'}),patch('socket.getaddrinfo') as system,patch.object(dns,'query',return_value=ANSWER),patch('bili_library.network.socket.socket') as factory:
            connect_public(HOST)
            system.assert_not_called()
            factory.return_value.connect.assert_called_once_with(('1.1.1.1',443))

    def test_private_mixed_unrelated_truncated_and_malformed_answers_rejected(self):
        for address in ('198.18.0.18','127.0.0.1','10.0.0.1','169.254.169.254','100.64.0.1','224.0.0.1','::1','not-an-ip'):
            data=copy.deepcopy(ANSWER);data['Answer'].append(dict(name=HOST,type=1,TTL=60,data=address))
            with self.subTest(address=address),self.assertRaises(LibraryError): dns.answers(HOST,data)
        for change in [dict(Status=2),dict(TC=True),dict(Question=[dict(name='unrelated.test',type=1)]),
                       dict(Answer=[dict(name='unrelated.test',type=1,TTL=60,data='1.1.1.1')]),
                       dict(Answer=[dict(name=HOST,type=1,TTL='60',data='1.1.1.1')]),dict(Answer=[None])]:
            with self.subTest(change=change),self.assertRaises(LibraryError): dns.answers(HOST,{**ANSWER,**change})

    def test_cname_chain_ttl_cache_and_no_cache_for_bad_results(self):
        data={**ANSWER,'Answer':[dict(name=HOST,type=5,TTL=30,data='alias.cdn.example'),
                                dict(name='alias.cdn.example',type=1,TTL=600,data='1.1.1.1'),
                                dict(name='unrelated.test',type=1,TTL=60,data='10.0.0.1')]}
        self.assertEqual(dns.answers(HOST,data),(('1.1.1.1',),30))
        with patch.object(dns,'query',return_value=data) as query,patch.object(dns.time,'monotonic',return_value=0) as clock:
            dns.resolve(HOST);clock.return_value=10;dns.resolve(HOST)
            self.assertEqual(query.call_count,1)
            clock.return_value=30;dns.resolve(HOST)
            self.assertEqual(query.call_count,2)
        self.setUp()
        bad={**ANSWER,'Answer':[dict(name=HOST,type=1,TTL=60,data='198.18.0.18')]}
        with patch.object(dns,'query',return_value=bad) as query:
            for _ in range(2):
                with self.assertRaises(LibraryError): dns.resolve(HOST)
            self.assertEqual(query.call_count,2);self.assertEqual(dns._cache,{})

    def test_cache_is_bounded_and_zero_ttl_is_not_cached(self):
        with patch.object(dns,'query') as query:
            for index in range(260):
                host=f'node{index}.bilivideo.com'
                query.return_value=dict(Status=0,Question=[dict(name=host,type=1)],Answer=[dict(name=host,type=1,TTL=100,data='1.1.1.1')])
                dns.resolve(host)
            self.assertEqual(len(dns._cache),256)
        self.setUp()
        zero={**ANSWER,'Answer':[dict(name=HOST,type=1,TTL=0,data='1.1.1.1')]}
        with patch.object(dns,'query',return_value=zero) as query:
            dns.resolve(HOST);dns.resolve(HOST);self.assertEqual(query.call_count,2)

    def test_fixed_tls_bootstrap_no_proxy_or_dns_and_no_cookies(self):
        conn=dns.BootstrapHTTPS('1.1.1.1')
        self.assertTrue(conn._context.check_hostname);self.assertEqual(conn._context.verify_mode,ssl.CERT_REQUIRED)
        with patch('socket.getaddrinfo') as system,patch.object(dns.socket,'socket') as factory,patch.object(conn,'_context') as context:
            conn.connect()
            system.assert_not_called()
            factory.return_value.connect.assert_called_once_with(('1.1.1.1',443))
            context.wrap_socket.assert_called_once_with(factory.return_value,server_hostname='cloudflare-dns.com')
        with patch.object(dns.socket,'socket') as factory,self.assertRaises(LibraryError): dns.BootstrapHTTPS('127.0.0.1').connect()
        factory.assert_not_called()
        response=Mock(status=200);response.getheader.return_value='application/dns-json';response.read.return_value=json.dumps(ANSWER).encode()
        client=Mock();client.getresponse.return_value=response
        with patch.dict(os.environ,{'HTTPS_PROXY':'http://127.0.0.1:9999'}),patch.object(dns,'BootstrapHTTPS',return_value=client):
            self.assertEqual(dns.query(HOST),ANSWER)
        client.request.assert_called_once_with('GET','/dns-query?name=api.bilibili.com&type=A',headers={'Accept':'application/dns-json'})

    def test_no_resolver_redirects_or_system_fallback(self):
        response=Mock(status=302);client=Mock();client.getresponse.return_value=response
        with patch.object(dns,'BootstrapHTTPS',return_value=client) as create,patch('socket.getaddrinfo') as system,self.assertRaises(LibraryError):
            dns.query(HOST)
        self.assertEqual(create.call_count,2);system.assert_not_called();response.read.assert_not_called()
        with patch.dict(os.environ,{'BILI_DNS_MODE':'cloudflare'}),patch.object(dns,'query',side_effect=LibraryError('DoH unavailable')),patch('socket.getaddrinfo') as system,self.assertRaises(LibraryError):
            public_addresses(HOST)
        system.assert_not_called()
        with patch.object(dns,'query') as query:
            for host in ('localhost','169.254.169.254','evil.com','www.bilibili.com.evil.com'):
                with self.subTest(host=host),self.assertRaises(LibraryError): dns.resolve(host)
            query.assert_not_called()

    def test_cli_dns_check_is_readonly_and_shows_effective_mode(self):
        from bili_library.__main__ import main
        fake=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('198.18.0.18',443))]
        with patch.dict(os.environ,{'BILI_DNS_MODE':'cloudflare'}),patch('sys.argv',['bili-library','dns-check']),patch('socket.getaddrinfo',return_value=fake),patch.object(dns,'query',return_value=ANSWER),patch('bili_library.__main__.Library') as library,patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(main(),0)
            value=json.loads(output.getvalue());self.assertEqual(value['system_addresses'],['198.18.0.18'])
            self.assertEqual(value['selected_addresses'],['1.1.1.1']);self.assertTrue(value['ok'])
            library.assert_not_called()


if __name__=='__main__': unittest.main()
