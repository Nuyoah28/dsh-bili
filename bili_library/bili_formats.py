"""Select only policy-compliant addresses BEFORE yt-dlp chooses media formats.

Backup URLs must come from the same Bili play-info stream, never from guessing,
user configuration, or an extra request. All TCP connections still use GuardProxy.
"""
from __future__ import annotations

from copy import deepcopy

from .network import URLPolicyError, validate_url


def media_url(value):
    if not isinstance(value,str):
        raise URLPolicyError('url_missing')
    if value.startswith('http://'):
        value='https://'+value[7:]
    elif value.startswith('//'):
        value='https:'+value
    validate_url(value)
    return value


def safe_play_info(play_info, report=lambda *args,**kwargs:None):
    data=deepcopy(play_info)
    fallback=0
    rejected=0
    seen=set()

    def select(node):
        nonlocal fallback
        if not isinstance(node,dict):
            return None
        primary=next((node.get(k) for k in ('baseUrl','base_url','url') if node.get(k)),None)
        candidates=[primary]
        for key in ('backupUrl','backup_url'):
            backups=node.get(key)
            if isinstance(backups,list):
                candidates.extend(backups)
        # Bili normally returns only a few backups; bound work even for malformed payloads.
        for index,value in enumerate(candidates[:17]):
            try:
                selected=media_url(value)
            except URLPolicyError as error:
                key=(error.diagnostic_code,tuple(sorted(error.diagnostic_details.items())))
                if key not in seen:
                    report(error.diagnostic_code,'warning',**error.diagnostic_details)
                    seen.add(key)
                continue
            if index:
                fallback+=1
            for name in ('baseUrl','base_url','url'):
                node[name]=selected
            return node
        return None

    def clean_list(parent,key):
        nonlocal rejected
        nodes=parent.get(key)
        if not isinstance(nodes,list):
            return
        kept=[]
        for node in nodes:
            selected=select(node)
            if selected is None:
                rejected+=1
            else:
                kept.append(selected)
        parent[key]=kept

    dash=data.get('dash')
    if isinstance(dash,dict):
        had_audio=bool(dash.get('audio') or (dash.get('dolby') or {}).get('audio') or (dash.get('flac') or {}).get('audio'))
        clean_list(dash,'video')
        clean_list(dash,'audio')
        dolby=dash.get('dolby')
        if isinstance(dolby,dict):
            clean_list(dolby,'audio')
        flac=dash.get('flac')
        if isinstance(flac,dict) and flac.get('audio'):
            flac['audio']=select(flac['audio'])
            if flac['audio'] is None:
                rejected+=1
        has_audio=bool(dash.get('audio') or (dash.get('dolby') or {}).get('audio') or (dash.get('flac') or {}).get('audio'))
        if had_audio and not has_audio:
            # Upstream otherwise marks video-only DASH as muxed when audio is absent.
            # Never turn an audio-address failure into a successful silent archive.
            rejected+=len(dash.get('video') or [])
            dash['video']=[]
    durl=data.get('durl')
    if isinstance(durl,list):
        selected=[select(node) for node in durl]
        # Dropping one fragment would produce an incomplete video, so drop this format.
        if any(node is None for node in selected):
            data['durl']=[]
            rejected+=1
        else:
            data['durl']=selected
    if fallback:
        report('address_fallback','info',fallback_streams=fallback)
    if rejected:
        report('format_skipped','warning',rejected_formats=rejected)
    return data


def install_safe_bili_extractor(ydl, library, task_id):
    # Replace only this downloader's Bili extractor; no global monkey patch or
    # modifications to the installed yt-dlp package, URL allowlist, or DNS policy.
    from yt_dlp.extractor.bilibili import BiliBiliIE
    from .diagnostics import record

    class LibraryBiliIE(BiliBiliIE):
        @classmethod
        def ie_key(cls):
            return BiliBiliIE.ie_key()

        def extract_formats(self, play_info):
            cleaned=safe_play_info(play_info,lambda code,level,**details:
                                   record(library,task_id,code,level,**details))
            formats=super().extract_formats(cleaned)
            if not formats or not any(f.get('vcodec') != 'none' for f in formats):
                raise URLPolicyError('no_safe_format')
            return formats

    ydl.add_info_extractor(LibraryBiliIE())
