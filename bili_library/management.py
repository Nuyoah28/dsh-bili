"""Local library management and persistent favorite-list annotations."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import time
import uuid

from .common import DEFAULTS, LibraryError, private_dir, read_json, write_json, number
from .network import BV, validate_url

RULES = [
    {'category':'数学与考研','keywords':['数学','高数','线性代数','矩阵','积分','微分','概率','张宇','考研']},
    {'category':'计算机与编程','keywords':['408','数据结构','计算机','计组','计网','编程','python','linux','算法','操作系统']},
    {'category':'AI 与工具','keywords':['deepseek','dsh','codex','大模型','人工智能','插件','chatgpt']},
    {'category':'英语与语言','keywords':['英语','英文','阅读','单词','语法','english']},
    {'category':'影视与音乐','keywords':['音乐','翻唱','主题曲','电影','乐团','lemon','老友记','声之形']},
]


def init_tables(lib):
    lib.db.executescript('''
      CREATE TABLE IF NOT EXISTS favorite_folders(id INTEGER PRIMARY KEY,title TEXT NOT NULL,synced REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS favorite_items(
        key TEXT PRIMARY KEY,folder_id INTEGER NOT NULL REFERENCES favorite_folders(id),
        resource_id TEXT NOT NULL,resource_type INTEGER NOT NULL,bvid TEXT,url TEXT,title TEXT NOT NULL,
        status TEXT NOT NULL,download_supported INTEGER NOT NULL,category TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '[]',note TEXT NOT NULL DEFAULT '',present INTEGER NOT NULL DEFAULT 1,
        updated REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS favorite_folder_index ON favorite_items(folder_id,present);
      CREATE TABLE IF NOT EXISTS trash(id TEXT PRIMARY KEY,video TEXT NOT NULL,deleted REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS web_jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,state TEXT NOT NULL,
        result TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS remote_plans(id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL,
        created REAL NOT NULL,result TEXT);
    ''')


def bounded_text(value, limit, name):
    if not isinstance(value,str) or len(value)>limit:
        raise LibraryError(name+'格式或长度无效')
    return value.strip()


def save_config(lib, incoming):
    if not isinstance(incoming,dict) or set(incoming)-set(DEFAULTS):
        raise LibraryError('配置仅允许修改已定义的资源限制字段')
    result={**lib.config,**incoming}
    for key in DEFAULTS:
        value=number(result[key],key,0 if key=='min_free_bytes' else 1)
        if int(value)!=value: raise LibraryError('资源配置必须为整数')
        result[key]=int(value)
    if result['initial_frames']>20 or result['max_extra_frames']>20:
        raise LibraryError('截图数量上限为 20')
    with lib.lock(): write_json(lib.config_path,result)
    return result


def delete_video(lib,video_id):
    with lib.lock():
        video=lib.video(video_id)
        source=lib.home/'videos'/video_id
        if source.is_symlink() or source.resolve().parent != (lib.home/'videos').resolve():
            raise LibraryError('视频目录异常，拒绝删除')
        trash_id=uuid.uuid4().hex
        destination=private_dir(lib.home/'trash')/trash_id
        moved=False
        try:
            if source.exists(): source.rename(destination); moved=True
            with lib.db:
                sources={video['source'],*(r['source'] for r in lib.rows('SELECT source FROM tasks WHERE video_id=?',(video_id,)))}
                for source_url in sources:
                    lib.db.execute("UPDATE tasks SET state='cancelled',error='关联视频已移入回收站',updated=? WHERE source=? AND state='queued'",(time.time(),source_url))
                lib.db.execute('INSERT INTO trash VALUES(?,?,?)',(trash_id,json.dumps(video,ensure_ascii=False),time.time()))
                lib.db.execute("UPDATE tasks SET video_id=NULL,stage=CASE WHEN state='completed' THEN 'removed' ELSE stage END WHERE video_id=?",(video_id,))
                lib.db.execute('DELETE FROM videos WHERE id=?',(video_id,))
        except Exception:
            if moved: destination.rename(source)
            raise
        return {'ok':True,'trash_id':trash_id,'message':'视频已移入本地回收站，可恢复；磁盘空间在永久删除后释放'}


def trash_action(lib,trash_id,restore):
    with lib.lock():
        rows=lib.rows('SELECT * FROM trash WHERE id=?',(trash_id,))
        if not rows: raise LibraryError('回收站条目不存在')
        video=json.loads(rows[0]['video'])
        source=lib.home/'trash'/trash_id
        if source.is_symlink() or source.resolve().parent != (lib.home/'trash').resolve():
            raise LibraryError('回收站路径异常')
        if restore:
            if lib.rows('SELECT id FROM videos WHERE id=?',(video['id'],)):
                raise LibraryError('资料库已有同一视频，不能覆盖恢复')
            destination=lib.home/'videos'/video['id']
            if destination.exists(): raise LibraryError('视频目录已存在，不能覆盖恢复')
            moved=False
            try:
                if source.exists(): source.rename(destination);moved=True
                with lib.db:
                    lib.db.execute('INSERT INTO videos('+','.join(video)+') VALUES('+','.join('?' for _ in video)+')',tuple(video.values()))
                    lib.db.execute('DELETE FROM trash WHERE id=?',(trash_id,))
            except Exception:
                if moved: destination.rename(source)
                raise
        else:
            if source.exists(): shutil.rmtree(source)
            with lib.db: lib.db.execute('DELETE FROM trash WHERE id=?',(trash_id,))
        return {'ok':True}


def import_favorites(lib,data):
    if not isinstance(data,dict): raise LibraryError('请导入收藏夹 JSON')
    groups=data.get('folders') if data.get('format')=='bili-library-organized-v1' else [data]
    if not isinstance(groups,list) or not 1<=len(groups)<=100: raise LibraryError('收藏夹分组无效，最多 100 组')
    if sum(len(g.get('items',[])) for g in groups if isinstance(g,dict))>20000: raise LibraryError('导入最多 20000 条')
    snapshots=[]
    for group in groups:
        if not isinstance(group,dict) or not isinstance(group.get('items'),list): raise LibraryError('收藏夹条目格式无效')
        fid=int(number(group.get('folder_id'),'收藏夹 ID',1))
        title=bounded_text(group.get('title','收藏夹'),300,'收藏夹标题')
        prepared=[]
        for item in group['items']:
            if not isinstance(item,dict): raise LibraryError('收藏条目必须为对象')
            rid=str(item.get('resource_id') or '')
            if not re.fullmatch(r'[1-9][0-9]*',rid): raise LibraryError('收藏条目缺少有效资源 ID，请重新同步')
            kind=int(number(item.get('resource_type',2),'资源类型',1))
            bvid=item.get('bvid')
            if bvid is not None and not re.fullmatch(BV,str(bvid)): raise LibraryError('BV 号格式无效')
            url=item.get('url')
            if url:
                u=validate_url(str(url))
                if u.hostname not in ('www.bilibili.com','bilibili.com'): raise LibraryError('收藏条目链接不是 B站播放地址')
                if not re.fullmatch(r'/video/'+BV+r'/?|/bangumi/play/ep[0-9]+/?',u.path): raise LibraryError('收藏条目播放链接无效')
            state=str(item.get('status','unsupported'))
            if state not in ('available','unavailable','unsupported'): raise LibraryError('收藏条目状态无效')
            key=f'{fid}:{kind}:{rid}'
            category=bounded_text(item.get('category',''),100,'分类')
            note=bounded_text(item.get('note',''),2000,'备注')
            tags=item.get('tags',[])
            if not isinstance(tags,list) or len(tags)>20: raise LibraryError('标签最多 20 个')
            tags=json.dumps([bounded_text(t,40,'标签') for t in tags],ensure_ascii=False)
            prepared.append((key,fid,rid,kind,bvid,url,bounded_text(item.get('title','未命名'),1000,'视频标题'),state,
                             int(kind==2 and bool(bvid) and bool(url) and state=='available'),category,tags,note,time.time()))
        snapshots.append((fid,title,prepared))
    # Validate every group before starting the single transaction.
    with lib.db:
        for fid,title,prepared in snapshots:
            lib.db.execute('INSERT INTO favorite_folders VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,synced=excluded.synced',(fid,title,time.time()))
            if data.get('format')!='bili-library-organized-v1':
                lib.db.execute('UPDATE favorite_items SET present=0 WHERE folder_id=?',(fid,))
            for values in prepared:
                lib.db.execute('''INSERT INTO favorite_items(key,folder_id,resource_id,resource_type,bvid,url,title,status,download_supported,category,tags,note,updated)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title,url=excluded.url,
                  bvid=excluded.bvid,status=excluded.status,download_supported=excluded.download_supported,present=1,updated=excluded.updated''',values)
    return {'ok':True,'folder_id':snapshots[0][0] if len(snapshots)==1 else None,'count':sum(len(g[2]) for g in snapshots),'folders':len(snapshots)}


def favorite_items(lib, fid=None):
    sql='SELECT * FROM favorite_items WHERE present=1'
    args=()
    if fid is not None: sql+=' AND folder_id=?';args=(int(fid),)
    items=lib.rows(sql+' ORDER BY rowid',args)
    counts={row['bvid']:row['n'] for row in lib.rows('SELECT bvid,count(*) n FROM favorite_items WHERE present=1 AND bvid IS NOT NULL GROUP BY bvid')}
    for item in items:
        item['tags']=json.loads(item['tags']);item['duplicate_count']=counts.get(item['bvid'],1)
    return items


def selection(lib,keys):
    if not isinstance(keys,list) or not 1<=len(keys)<=1000 or not all(isinstance(k,str) for k in keys):
        raise LibraryError('请选择 1～1000 条收藏')
    keys=list(dict.fromkeys(keys))
    rows=[]
    for key in keys:
        found=lib.rows('SELECT * FROM favorite_items WHERE key=? AND present=1',(key,))
        if not found: raise LibraryError('所选收藏已变更，请刷新列表')
        rows.append(found[0])
    return rows


def annotate(lib,keys,patch):
    rows=selection(lib,keys)
    if not isinstance(patch,dict) or not patch or set(patch)-{'category','tags','note'}: raise LibraryError('无效的本地整理字段')
    changes={}
    for key in ('category','note'):
        if key in patch: changes[key]=bounded_text(patch[key],100 if key=='category' else 2000,key)
    if 'tags' in patch:
        tags=patch['tags']
        if not isinstance(tags,list) or len(tags)>20: raise LibraryError('标签最多 20 个')
        changes['tags']=json.dumps(list(dict.fromkeys(bounded_text(t,40,'标签') for t in tags if t)),ensure_ascii=False)
    with lib.db:
        for row in rows:
            lib.db.execute('UPDATE favorite_items SET '+','.join(k+'=?' for k in changes)+' WHERE key=?',(*changes.values(),row['key']))
    return {'ok':True,'count':len(rows)}


def rules(lib):
    return read_json(lib.home/'organize-rules.json',RULES)


def save_rules(lib,value):
    if not isinstance(value,list) or len(value)>50: raise LibraryError('分类规则最多 50 组')
    normalized=[]
    for rule in value:
        category=bounded_text(rule.get('category'),100,'分类')
        words=rule.get('keywords')
        if not category or not isinstance(words,list) or not 1<=len(words)<=100: raise LibraryError('每条规则需要分类和关键词列表')
        normalized.append(dict(category=category,keywords=[bounded_text(w,80,'关键词') for w in words]))
    write_json(lib.home/'organize-rules.json',normalized)
    return normalized


def classify(lib,keys,apply=False):
    rows=selection(lib,keys)
    proposals=[]
    configured_rules=rules(lib)
    for item in rows:
        if item['category']: continue  # preserve every manual/existing category
        match=next((r for r in configured_rules if any(w and w.lower() in item['title'].lower() for w in r['keywords'])),None)
        if match: proposals.append({'key':item['key'],'title':item['title'],'category':match['category']})
    if apply:
        with lib.db:
            for p in proposals: lib.db.execute("UPDATE favorite_items SET category=? WHERE key=? AND category=''",(p['category'],p['key']))
    return {'proposals':proposals,'applied':apply,'count':len(proposals)}
