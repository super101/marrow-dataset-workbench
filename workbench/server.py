"""Loopback-only HTTP API. Persistent writes require cookie, origin and header."""
from __future__ import annotations

import collections
import concurrent.futures
import copy
import hmac
import io
import json
import math
import mimetypes
from pathlib import Path
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
import uuid
import zipfile

from PIL import Image

from . import __version__
from .core import (Conflict, Store, beneath, create_demo, import_dataset, import_metadata, now,
                   patch_image, patch_mapping, patch_rules, clear_split, csv_rows)
from .exporter import image_issues, make_groups, plan_split, preflight, build_export


class Cancelled(ValueError):
    pass


class App:
    def __init__(self, store):
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.job_lock = threading.Lock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.static = Path(__file__).with_name('static')
        self.asset_cache = {}

    def job(self, action):
        key = uuid.uuid4().hex
        entry = dict(id=key, status='queued', progress=0, message='等待处理', result=None)
        self.jobs[key] = entry
        def run():
            entry.update(status='running', message='处理中')
            def progress(fraction, text):
                if entry.get('cancel_requested'):
                    raise Cancelled('用户取消任务；未完成结果不进入已入库或已导出列表')
                entry.update(progress=round(fraction*100, 1), message=text)
            try:
                progress(0, '开始处理')
                result = action(progress)
                entry.update(status='complete', progress=100, message='完成', result=result)
            except Cancelled as exc:
                entry.update(status='cancelled', message=str(exc))
            except Exception as exc:
                entry.update(status='failed', message='任务失败，未生成成功产物', error=str(exc))
        self.executor.submit(run)
        return {'job_id': key}

    def detail(self, dataset, image):
        result = {k: v for k, v in image.items() if k not in {'asset_path', 'sha256', 'pixel_sha256'}}
        base = f"/api/datasets/{dataset['id']}/images/{image['id']}/asset"
        return dict(result, issues=image_issues(dataset, image), asset_url=base, thumbnail_url=base+'?thumbnail=1')

    def meta(self, dataset):
        groups = make_groups(dataset)
        labels = {x['code']: dict(code=x['code'], name=x['name'], boxes=0, images=set(), groups=set()) for x in dataset['mapping']['entries']}
        issues = collections.Counter()
        objects = 0
        for image in dataset['images']:
            issues.update({p['kind'] for p in image_issues(dataset, image)})
            for obj in image['objects']:
                if obj.get('deleted'):
                    continue
                objects += 1
                row = labels.setdefault(obj['code'], dict(code=obj['code'], name='未定义', boxes=0, images=set(), groups=set()))
                row['boxes'] += 1
                row['images'].add(image['id'])
                row['groups'].add(groups[image['id']])
        scope = 'patient' if all(i['metadata'].get('patient_id') and i['metadata'].get('metadata_evidence') for i in dataset['images']) else 'slide' if all(i['metadata'].get('slide_id') and i['metadata'].get('metadata_evidence') for i in dataset['images']) else 'component'
        result = {k: dataset[k] for k in ('id', 'name', 'revision', 'created_at', 'source_kind', 'coordinate_mode', 'mapping', 'rules', 'split')}
        result['mapping_schemes'] = dataset.get('mapping_schemes', [dataset['mapping']])
        result['import_report'] = dataset.get('import_report', {})
        result.update(last_image_id=dataset.get('last_image_id'), exports=self.store.exports(dataset['id']), summary=dict(
            images=len(dataset['images']), unique_images=len({i['pixel_sha256'] for i in dataset['images']}), objects=objects,
            classes=len({e['train_name'] for e in dataset['mapping']['entries'] if e['role']=='cell'}),
            reviewed=sum(i['review_state']=='confirmed' for i in dataset['images']), pending=sum(i['review_state']!='confirmed' and not i['exclude_reason'] for i in dataset['images']),
            excluded=sum(bool(i['exclude_reason']) for i in dataset['images']), issue_counts=dict(issues), groups=len(set(groups.values())), identity_scope=scope,
            label_counts=[dict(r, images=len(r['images']), groups=len(r['groups'])) for r in labels.values()]))
        return result


class Handler(BaseHTTPRequestHandler):
    server_version = 'MarrowWorkbench'
    protocol_version = 'HTTP/1.1'

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # No query strings, local file names, tokens or patient strings in logs.
        pass

    def valid_request(self, write=False):
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        host = self.headers.get('Host', '')
        if host not in allowed:
            raise PermissionError('仅支持本机访问')
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + x for x in allowed}:
            raise PermissionError('来源校验失败')
        entering_home = urlsplit(self.path).path == '/' and self.headers.get('Sec-Fetch-Mode') == 'navigate' and self.headers.get('Sec-Fetch-Dest') == 'document'
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' and not entering_home:
            raise PermissionError('不接受跨站访问')
        parsed = urlsplit(self.path)
        if parsed.path.startswith('/api/'):
            cookies = dict(item.strip().split('=', 1) for item in self.headers.get('Cookie', '').split(';') if '=' in item)
            if not hmac.compare_digest(cookies.get('marrow_session', ''), self.app.token):
                raise PermissionError('请先打开工作台首页建立本机会话')
        if write:
            if self.headers.get('X-Workbench') != '1' or self.headers.get_content_type() != 'application/json':
                raise PermissionError('写入必须来自工作台 JSON 请求')

    def send(self, status, content, content_type='application/json; charset=utf-8', extra=None, cookie=False):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header('Set-Cookie', f'marrow_session={self.app.token}; Path=/; HttpOnly; SameSite=Strict')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def error(self, exc):
        code = 409 if isinstance(exc, Conflict) else 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, (KeyError, StopIteration, FileNotFoundError)) else 400
        self.send(code, {'error': type(exc).__name__, 'message': str(exc) or '记录不存在'})

    def do_GET(self):
        try:
            self.valid_request()
            url = urlsplit(self.path)
            path = [unquote(p) for p in url.path.strip('/').split('/')]
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == '/':
                self.send(200, (self.app.static/'index.html').read_bytes(), 'text/html; charset=utf-8', cookie=True)
                return
            if path[0] == 'static':
                file = beneath(self.app.static, self.app.static.joinpath(*path[1:]))
                ctype = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
                self.send(200, file.read_bytes(), ctype)
                return
            if path == ['api', 'bootstrap']:
                self.send(200, dict(datasets=[self.app.meta(d) for d in self.app.store.list()], version=__version__, registered_roots=[str(p) for p in self.app.store.allowed_roots], workspace=str(self.app.store.workspace)))
                return
            if path == ['api', 'datasets']:
                self.send(200, {'datasets': [self.app.meta(d) for d in self.app.store.list()]})
                return
            if len(path) == 3 and path[:2] == ['api', 'jobs']:
                self.send(200, self.app.jobs[path[2]])
                return
            if len(path) == 4 and path[:2] == ['api', 'exports'] and path[3] == 'download':
                file = self.app.store.export_path(path[2])
                # Stream potentially large zip instead of allocating entire train package in memory.
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Length', str(file.stat().st_size))
                self.send_header('Content-Disposition', f'attachment; filename="training-{path[2]}.zip"')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                with file.open('rb') as f:
                    while chunk := f.read(1024*1024):
                        self.wfile.write(chunk)
                return
            if len(path) >= 3 and path[:2] == ['api', 'datasets']:
                dataset = self.app.store.get(path[2])
                if len(path) == 3:
                    self.send(200, self.app.meta(dataset)); return
                if path[3] == 'history':
                    self.send(200, {'items': self.app.store.history(dataset['id'])}); return
                if path[3] == 'images':
                    if len(path) == 4:
                        rows = []
                        for image in dataset['images']:
                            issues = image_issues(dataset, image)
                            state = query.get('status', 'all')
                            if state == 'pending' and (image['review_state']=='confirmed' or image['exclude_reason']): continue
                            if state == 'confirmed' and image['review_state'] != 'confirmed': continue
                            if state == 'excluded' and not image['exclude_reason']: continue
                            if query.get('issue') and query['issue'] not in {'all',''} and not any(p['kind']==query['issue'] for p in issues): continue
                            if query.get('code') and query['code'] not in {'all',''} and not any(o['code']==query['code'] and not o.get('deleted') for o in image['objects']): continue
                            if query.get('q') and query['q'].lower() not in (image['id']+' '+image.get('source_id','')).lower(): continue
                            rows.append(dict(id=image['id'], source_id=image.get('source_id',''), width=image['width'], height=image['height'], objects=sum(not o.get('deleted') for o in image['objects']), issues=issues,
                                             review_state=image['review_state'], exclude_reason=image['exclude_reason'], thumbnail_url=f"/api/datasets/{dataset['id']}/images/{image['id']}/asset?thumbnail=1"))
                        page, limit = max(1,int(query.get('page',1))), max(1,min(100,int(query.get('limit',24))))
                        self.send(200, dict(items=rows[(page-1)*limit:page*limit], total=len(rows), page=page, limit=limit)); return
                    image = next(i for i in dataset['images'] if i['id']==path[4])
                    if len(path)==5:
                        self.send(200, self.app.detail(dataset,image)); return
                    if len(path)==6 and path[5]=='asset':
                        file = beneath(self.app.store.workspace, self.app.store.workspace/image['asset_path'])
                        cache_key=(str(file),file.stat().st_mtime_ns,query.get('thumbnail'))
                        if query.get('thumbnail') and cache_key in self.app.asset_cache:
                            data=self.app.asset_cache[cache_key]
                        else:
                            with Image.open(file) as p:
                                rgb=p.convert('RGB')
                                if query.get('crop'):
                                    obj=next(o for o in image['objects'] if o['id']==query['crop'])
                                    box=obj['bbox']
                                    if not all(isinstance(x,(int,float)) and math.isfinite(x) for x in box) or not (0<=box[0]<box[2]<=image['width'] and 0<=box[1]<box[3]<=image['height']): raise ValueError('框坐标无效或超出图像范围')
                                    rgb=rgb.crop(tuple(map(int,box)))
                                    rgb.thumbnail((512,512))
                                elif query.get('thumbnail'): rgb.thumbnail((240,180))
                                buf=io.BytesIO();rgb.save(buf,'JPEG',quality=88);data=buf.getvalue()
                            if query.get('thumbnail'):
                                if len(self.app.asset_cache)>256:self.app.asset_cache.clear()
                                self.app.asset_cache[cache_key]=data
                        self.send(200,data,'image/jpeg');return
            raise KeyError('接口不存在')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.error(exc)

    def do_POST(self):
        self.write()

    def do_PATCH(self):
        self.write()

    def write(self):
        try:
            self.valid_request(write=True)
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=20*1024*1024:raise ValueError('请求内容为空或过大')
            data=json.loads(self.rfile.read(size), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('不支持非有限数值')))
            if not isinstance(data,dict):raise ValueError('请求必须为 JSON 对象')
            path=[unquote(p) for p in urlsplit(self.path).path.strip('/').split('/')]
            store=self.app.store
            if len(path)==4 and path[:2]==['api','jobs'] and path[3]=='cancel':
                job=self.app.jobs[path[2]]
                if job['status'] in {'queued','running'}:
                    job['cancel_requested']=True
                    job['message']='正在停止；等待当前文件处理完成'
                self.send(200,job);return
            if path==['api','datasets','demo']:
                self.send(202,self.app.job(lambda progress:{'dataset_id':create_demo(store,progress)['id']}));return
            if path==['api','datasets','import']:
                self.send(202,self.app.job(lambda progress:{'dataset_id':import_dataset(store,data.get('source_path',''),data.get('kind'),data.get('name'),data.get('coordinate_mode'),data.get('dictionary_path'),progress)['id']}));return
            if len(path)<4 or path[:2]!=['api','datasets']:raise KeyError('接口不存在')
            key=path[2]
            dataset=store.get(key)
            expected=data.get('expected_revision')
            if expected!=dataset['revision']:raise Conflict('版本已更新，请重新加载后重试')
            action=path[3]
            if action=='images' and len(path)==5:
                image_id=path[4]
                result=store.update(key,expected,'修订图像标注与复核',lambda d:patch_image(d,image_id,data),image_id)
                self.send(200,dict(dataset=self.app.meta(result),image=self.app.detail(result,next(i for i in result['images'] if i['id']==image_id))));return
            if action in {'mapping','rules','metadata','relations'}:
                def mutate(d):
                    if action=='mapping':patch_mapping(d,data)
                    elif action=='rules':patch_rules(d,data)
                    elif action=='metadata':import_metadata(d,data.get('csv_text',''))
                    else:
                        rows=csv_rows(data.get('csv_text',''));ids={i['id'] for i in d['images']}
                        for r in rows:
                            if r.get('a') not in ids or r.get('b') not in ids or not r.get('evidence','').strip() or r.get('kind') not in {'source','derived','crop','augmentation','overlap','near_duplicate','duplicate','same_source'}:
                                raise ValueError('来源关系需要有效图像、关系类型与依据')
                        d['relations']=rows;clear_split(d)
                result=store.update(key,expected,{'mapping':'修订类别方案','rules':'修订来源与任务规则','metadata':'导入身份映射','relations':'登记已确认来源关系'}[action],mutate)
                self.send(200,{'dataset':self.app.meta(result)});return
            if action=='split':
                plan=plan_split(dataset,data.get('ratios',[.7,.15,.15]),data.get('seed',42),data.get('scope','component'))
                if data.get('assignments_csv'):
                    rows=csv_rows(data['assignments_csv']);assign={r['image_id']:r['split'] for r in rows}
                    if set(assign)!=set(plan['groups']) or len(rows)!=len(assign) or any(s not in {'train','val','test'} for s in assign.values()):raise ValueError('划分 CSV 必须且只能包含全部图像，每图一行')
                    group_splits=collections.defaultdict(set)
                    for i,s in assign.items():group_splits[plan['groups'][i]].add(s)
                    if any(len(x)>1 for x in group_splits.values()) or set(assign.values())!={'train','val','test'}:raise ValueError('划分存在组泄漏或空集合')
                    plan['assignments']=assign
                if data.get('confirm'):
                    plan['confirmed']=True
                    result=store.update(key,expected,'封存分组划分',lambda d:d.update(split=plan))
                    self.send(200,{'dataset':self.app.meta(result),'split':plan})
                else:self.send(200,{'dataset':self.app.meta(dataset),'split':plan})
                return
            if action=='export-check':
                self.send(200,preflight(dataset,data,store.workspace));return
            if action=='exports':
                options=copy.deepcopy(data)
                options['mode']=options.get('mode','basic')
                def run(progress):
                    export_id=uuid.uuid4().hex
                    destination=store.workspace/'exports'/export_id
                    report=build_export(dataset,options,store.workspace,destination,progress)
                    progress(.95,'生成训练包压缩文件')
                    archive=destination.with_suffix('.zip')
                    temporary=archive.with_suffix('.zip.partial')
                    with zipfile.ZipFile(temporary,'x',zipfile.ZIP_DEFLATED) as z:
                        for f in sorted(destination.rglob('*')):
                            if f.is_file():
                                progress(.98,'打包文件，保留不可变导出快照')
                                z.write(f,str(f.relative_to(destination)).replace('\\','/'))
                    temporary.rename(archive)
                    store.register_export(export_id,dataset,options,archive)
                    return {'export_id':export_id,'download_url':f'/api/exports/{export_id}/download','revision':dataset['revision']}
                self.send(202,self.app.job(run));return
            raise KeyError('接口不存在')
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as exc:self.error(exc)


def serve(store, port):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    server.app=App(store)
    return server
