"""Human-reviewed, one-use plans for changes to the logged-in owner's favorites."""
from __future__ import annotations

import json
import time
import uuid

from .bilibili import export_favorites, folders, load_cookies
from .common import LibraryError, write_json
from .management import import_favorites, selection
from .network import Network


def identity(network):
    if not network.cookies.get('SESSDATA') or not network.cookies.get('bili_jct'):
        raise LibraryError('请先导入有效 B站登录凭证（需要 SESSDATA 和 bili_jct）')
    account=network.api('/x/web-interface/nav')
    if not account.get('isLogin') or not account.get('mid'):
        raise LibraryError('B站登录已失效，请重新登录')
    uid=int(account['mid'])
    owned=folders(network,str(uid))['folders']
    return uid,{int(f['id']):f for f in owned}


def preview(lib,keys,action,target=None,network=None):
    if action not in ('move','delete'): raise LibraryError('仅支持移动或删除收藏条目')
    items=selection(lib,keys)
    if len(items)>50: raise LibraryError('一次远程操作最多 50 条，请分批整理')
    source=int(items[0]['folder_id'])
    if any(i['folder_id']!=source for i in items): raise LibraryError('一次只能整理同一个源收藏夹')
    if any(i['resource_type']!=2 for i in items): raise LibraryError('远程移动/删除暂仅支持普通 BV 视频，影视条目请在 B站操作')
    network=network or Network(load_cookies(lib))
    with lib.lock():
        uid,owned=identity(network)
        if source not in owned: raise LibraryError('只能修改当前登录账号自己创建的收藏夹')
        if action=='move':
            try: target=int(target)
            except (ValueError,TypeError): raise LibraryError('请选择目标收藏夹') from None
            if target not in owned or target==source: raise LibraryError('目标必须是自己的另一个收藏夹')
        else: target=None
        current=export_favorites(network,str(source))
        current_keys={(str(i['resource_id']),int(i.get('resource_type',2))) for i in current['items']}
        if any((i['resource_id'],i['resource_type']) not in current_keys for i in items):
            raise LibraryError('源收藏夹已发生变化，请先同步再预览')
        plan_id=uuid.uuid4().hex
        plan=dict(id=plan_id,action=action,source=source,source_title=owned[source]['title'],target=target,
                  target_title=owned[target]['title'] if target else None,uid=uid,
                  items=[{k:i[k] for k in ('key','title','resource_id','resource_type','bvid','url','category','tags','note')} for i in items],
                  confirmation=('移动' if action=='move' else '删除')+str(len(items))+'条',expires=time.time()+600)
        # Keep a rollback/reference inventory before any mutation. It contains no credential material.
        write_json(lib.home/'exports'/f'before-remote-{plan_id}.json',current)
        with lib.db: lib.db.execute('INSERT INTO remote_plans VALUES(?,?,?,?,?)',(plan_id,json.dumps(plan,ensure_ascii=False),'prepared',time.time(),None))
        return plan


def commit(lib,plan_id,confirmation,network=None):
    network=network or Network(load_cookies(lib))
    with lib.lock():
        with lib.db:
            lib.db.execute('BEGIN IMMEDIATE')
            rows=lib.rows('SELECT * FROM remote_plans WHERE id=?',(plan_id,))
            if not rows or rows[0]['state']!='prepared': raise LibraryError('操作预览已使用或不存在，请重新预览')
            plan=json.loads(rows[0]['payload'])
            if time.time()>plan['expires']: raise LibraryError('操作预览已过期，请重新预览')
            if confirmation!=plan['confirmation']: raise LibraryError('确认文字不匹配')
            # Consume before the first network call, preventing replay even on timeout/restart.
            lib.db.execute("UPDATE remote_plans SET state='executing' WHERE id=?",(plan_id,))
        try:
            uid,owned=identity(network)
            if uid!=plan['uid'] or plan['source'] not in owned or (plan['target'] and plan['target'] not in owned):
                raise LibraryError('账号或收藏夹归属已变化，请重新预览')
            before=export_favorites(network,str(plan['source']))
            membership={(str(i['resource_id']),int(i.get('resource_type',2))) for i in before['items']}
            if any((i['resource_id'],i['resource_type']) not in membership for i in plan['items']):
                raise LibraryError('源条目已变化，未发送写请求，请重新同步和预览')
            resources=','.join(i['resource_id']+':2' for i in plan['items'])
            params={'resources':resources,'platform':'web'}
            if plan['action']=='move':
                params.update(src_media_id=plan['source'],tar_media_id=plan['target'],mid=uid)
                path='/x/v3/fav/resource/move'
            else:
                params.update(media_id=plan['source']);path='/x/v3/fav/resource/batch-del'
            network.post_favorite(path,params)
            after=export_favorites(network,str(plan['source']))
            source_ids={(str(i['resource_id']),int(i.get('resource_type',2))) for i in after['items']}
            target_data=export_favorites(network,str(plan['target'])) if plan['target'] else None
            target_ids={(str(i['resource_id']),int(i.get('resource_type',2))) for i in target_data['items']} if target_data else set()
            verified=[];uncertain=[]
            for item in plan['items']:
                key=(item['resource_id'],item['resource_type'])
                (verified if key not in source_ids and (not target_data or key in target_ids) else uncertain).append(item['key'])
            import_favorites(lib,after)
            if target_data:
                import_favorites(lib,target_data)
                # Carry local annotations across a verified move.
                with lib.db:
                    for item in plan['items']:
                        if item['key'] in verified:
                            lib.db.execute('UPDATE favorite_items SET category=?,tags=?,note=? WHERE key=?',
                              (item['category'],item['tags'],item['note'],f'{plan["target"]}:2:{item["resource_id"]}'))
            result={'ok':not uncertain,'verified':verified,'uncertain':uncertain,'plan_id':plan_id,
                    'message':'操作已复核' if not uncertain else '部分结果未能确认，请同步源和目标收藏夹；不会自动重试'}
            with lib.db: lib.db.execute('UPDATE remote_plans SET state=?,result=? WHERE id=?',('completed' if not uncertain else 'uncertain',json.dumps(result,ensure_ascii=False),plan_id))
            return result
        except Exception as error:
            message=str(error) if isinstance(error,LibraryError) else '操作结果未确认，请同步收藏夹后核对；不会自动重试'
            result={'ok':False,'error':message,'plan_id':plan_id}
            with lib.db: lib.db.execute("UPDATE remote_plans SET state='uncertain',result=? WHERE id=?",(json.dumps(result,ensure_ascii=False),plan_id))
            raise LibraryError(message) from None
