from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bili_library.bili_formats import safe_play_info, install_safe_bili_extractor, media_url
from bili_library.common import Library, LibraryError
from bili_library.diagnostics import classify, record
from bili_library.management import task_logs
from bili_library.network import URLPolicyError, validate_url, public_addresses

GOOD='https://upos-sz-mirrorcosov.bilivideo.com/media?sign=secret-query'
BAD='https://peer.mcdn.bilivideo.cn:4483/media?sign=secret-query'


def payload(primary=BAD,backup=GOOD):
    return {'dash':{'video':[{'id':64,'baseUrl':primary,'backupUrl':[backup],'height':720,'width':1280,'codecs':'avc1','mimeType':'video/mp4','bandwidth':900000}],
                    'audio':[{'id':30280,'base_url':primary,'backup_url':[backup],'codecs':'mp4a','mime_type':'audio/mp4','bandwidth':100000}]},
            'support_formats':[{'quality':64,'new_description':'720P'}]}


class BiliFormatTests(unittest.TestCase):
    def test_same_stream_backup_before_format_selection_preserves_input(self):
        raw=payload();before=deepcopy(raw);events=[]
        cleaned=safe_play_info(raw,lambda *a,**kw:events.append((a,kw)))
        self.assertEqual(raw,before)
        self.assertEqual(cleaned['dash']['video'][0]['baseUrl'],GOOD)
        self.assertEqual(cleaned['dash']['audio'][0]['base_url'],GOOD)
        self.assertEqual([e[1]['fallback_streams'] for e in events if e[0][0]=='address_fallback'],[2])
        self.assertNotIn('secret-query',json.dumps(events))
        self.assertTrue(any(e[0][0]=='url_port' and e[1]['media_port']==4483 for e in events))

    def test_compliant_primary_is_kept_no_extra_requests_or_rewrite(self):
        raw=payload(GOOD,'https://evil.invalid/secret')
        events=[]
        with patch('socket.getaddrinfo',side_effect=AssertionError('no extra DNS')):
            cleaned=safe_play_info(raw,lambda *a,**kw:events.append((a,kw)))
        self.assertEqual(cleaned['dash']['video'][0]['baseUrl'],GOOD)
        self.assertEqual(events,[])

    def test_rejected_backups_are_never_selected_and_audio_not_silently_lost(self):
        for backup in ('https://evil.invalid/media','https://bilibili.com.evil.invalid/', 'https://user:secret@upos-sz.bilivideo.com/x','file:///etc/passwd', BAD):
            raw=payload(BAD,backup)
            raw['dash']['video'].append({**raw['dash']['video'][0],'baseUrl':GOOD})
            cleaned=safe_play_info(raw)
            self.assertEqual(cleaned['dash']['audio'],[])
            self.assertEqual(cleaned['dash']['video'],[])

    def test_other_valid_quality_survives_and_fragment_format_is_atomic(self):
        raw=payload()
        raw['dash']['video'][0]['backupUrl']=[]
        raw['dash']['video'].append({**raw['dash']['video'][0],'id':32,'height':480,'baseUrl':GOOD})
        raw['durl']=[{'url':GOOD},{'url':BAD,'backup_url':[]}]
        cleaned=safe_play_info(raw)
        self.assertEqual([v['height'] for v in cleaned['dash']['video']],[480])
        self.assertEqual(cleaned['durl'],[])
        raw['durl'][1]['backup_url']=[GOOD]
        self.assertEqual(len(safe_play_info(raw)['durl']),2)

    def test_https_normalization_keeps_signature_and_rejects_non443(self):
        self.assertEqual(media_url(GOOD.replace('https:','http:')),GOOD)
        self.assertEqual(media_url(GOOD[6:]),GOOD)
        for value,code in [(BAD,'url_port'),('https://evil.invalid/','url_domain'),('ftp://www.bilibili.com/a','url_scheme'),('','url_missing'),('https://u:secret@www.bilibili.com/a','url_credentials')]:
            with self.assertRaises(URLPolicyError) as raised: validate_url(value)
            self.assertEqual(classify(raised.exception),code)
            self.assertNotIn('secret',str(raised.exception))
        with patch('bili_library.network.socket.getaddrinfo',return_value=[(2,1,6,'',('198.18.0.18',443))]):
            with self.assertRaises(LibraryError): public_addresses('upos-sz-mirrorcosov.bilivideo.com')

    def test_real_ytdlp_instance_adapter_scoped_and_selector_uses_backup(self):
        try:
            from yt_dlp import YoutubeDL
        except ImportError: self.skipTest('yt-dlp unavailable')
        with tempfile.TemporaryDirectory() as temp:
            lib=Library(Path(temp));self.addCleanup(lib.close)
            task=lib.submit('https://www.bilibili.com/video/BV1234567890')
            with YoutubeDL({'quiet':True,'format':'bestvideo[height<=720]+bestaudio/best[height<=720]'}) as ydl:
                original=ydl.get_info_extractor('BiliBili')
                install_safe_bili_extractor(ydl,lib,task['id'])
                extractor=ydl.get_info_extractor('BiliBili')
                self.assertIsNot(extractor,original)
                formats=extractor.extract_formats(payload())
                info=ydl.process_ie_result({'id':'BV1234567890','title':'fixture','formats':formats},download=False)
                self.assertEqual(len(info['requested_formats']),2)
                self.assertTrue(all(f['url']==GOOD for f in info['requested_formats']))
                with self.assertRaises(URLPolicyError): extractor.extract_formats(payload(BAD,'https://evil.invalid/'))
            with YoutubeDL({'quiet':True}) as other:
                self.assertIs(type(other.get_info_extractor('BiliBili')),type(original))
            events=json.dumps(task_logs(lib,task['id'])['logs'])
            self.assertNotIn('secret-query',events)
            self.assertNotIn('https:',events)


if __name__=='__main__': unittest.main()
