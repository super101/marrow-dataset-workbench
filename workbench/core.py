"""Private workspace persistence, traceable imports and optimistic revisions."""
from __future__ import annotations

import collections
import copy
import csv
import hashlib
import io
import json
import math
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return prefix + uuid.uuid4().hex[:16]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))


def beneath(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError('文件路径超出登记的数据目录')
    return path


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, workspace, allowed_roots=()):
        self.workspace = Path(workspace).resolve()
        checkout = Path(__file__).resolve().parents[1]
        if self.workspace.is_relative_to(checkout) or checkout.is_relative_to(self.workspace):
            raise ValueError('工作数据目录必须在代码仓库外，并且不能包含代码仓库')
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = [Path(p).resolve() for p in allowed_roots]
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.workspace / 'workbench.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions(dataset_id TEXT,revision INTEGER,at TEXT,action TEXT,image_id TEXT,payload TEXT,PRIMARY KEY(dataset_id,revision));
            CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY,dataset_id TEXT,created_at TEXT,revision INTEGER,mode TEXT,path TEXT);
        ''')
        self.db.commit()
        self._cache = {}
        self._data_version = self.db.execute('PRAGMA data_version').fetchone()[0]

    def source(self, value):
        path = Path(value).resolve(strict=True)
        if not any(path.is_relative_to(root) for root in self.allowed_roots):
            raise ValueError('来源路径未登记；请通过启动参数 --allow-root 登记可信数据根目录')
        if path == self.workspace or self.workspace.is_relative_to(path) or path.is_relative_to(self.workspace):
            raise ValueError('入库来源必须与工作目录分离且互不嵌套')
        return path

    def get(self, dataset_id):
        with self.lock:
            version = self.db.execute('PRAGMA data_version').fetchone()[0]
            if version != self._data_version:
                self._cache.clear()
                self._data_version = version
            if dataset_id in self._cache:
                return self._cache[dataset_id]
            row = self.db.execute('SELECT payload FROM datasets WHERE id=?', (dataset_id,)).fetchone()
            if not row:
                raise KeyError('数据集不存在')
            result = json.loads(row[0])
            if len(self._cache) >= 3:
                self._cache.pop(next(iter(self._cache)))
            self._cache[dataset_id] = result
            return result

    def list(self):
        with self.lock:
            rows = self.db.execute('SELECT id FROM datasets ORDER BY rowid DESC').fetchall()
        return [self.get(row[0]) for row in rows]

    def create(self, dataset):
        dataset.setdefault('mapping_schemes', [copy.deepcopy(dataset['mapping'])])
        raw = json.dumps(dataset, ensure_ascii=False, allow_nan=False)
        with self.lock, self.db:
            self.db.execute('INSERT INTO datasets VALUES(?,?)', (dataset['id'], raw))
            self.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (dataset['id'], 1, now(), '导入独立工作版本', '', raw))
        return dataset

    def update(self, dataset_id, expected_revision, action, mutate, image_id=''):
        with self.lock:
            dataset = copy.deepcopy(self.get(dataset_id))
            if dataset['revision'] != expected_revision:
                raise Conflict('数据已有更新，请重新加载，避免覆盖其他窗口的修改')
            mutate(dataset)
            dataset['revision'] += 1
            dataset['updated_at'] = now()
            raw = json.dumps(dataset, ensure_ascii=False, allow_nan=False)
            if image_id:
                audit = next(i for i in dataset['images'] if i['id'] == image_id)
            else:
                audit = {k: v for k, v in dataset.items() if k != 'images'}
            with self.db:
                cursor = self.db.execute("UPDATE datasets SET payload=? WHERE id=? AND json_extract(payload,'$.revision')=?", (raw, dataset_id, expected_revision))
                if cursor.rowcount != 1:
                    self._cache.pop(dataset_id, None)
                    raise Conflict('其他服务已更新此版本，请重新加载')
                self.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (dataset_id, dataset['revision'], now(), action, image_id, json.dumps(audit, ensure_ascii=False, allow_nan=False)))
            self._cache[dataset_id] = dataset
            return dataset

    def history(self, dataset_id):
        with self.lock:
            rows = self.db.execute('SELECT at,revision,action,image_id FROM revisions WHERE dataset_id=? ORDER BY revision DESC LIMIT 100', (dataset_id,)).fetchall()
        return [dict(zip(('at', 'revision', 'action', 'image_id'), r)) for r in rows]

    def exports(self, dataset_id):
        with self.lock:
            rows = self.db.execute('SELECT id,created_at,revision,mode FROM exports WHERE dataset_id=? ORDER BY created_at DESC', (dataset_id,)).fetchall()
        return [dict(zip(('id', 'created_at', 'revision', 'mode'), r)) for r in rows]

    def register_export(self, export_id, dataset, options, path):
        records = self.workspace / 'export_records'
        records.mkdir(exist_ok=True)
        source_map = []
        for number, image in enumerate(sorted(dataset['images'], key=lambda x: x['id']), 1):
            source_map.append({'export_image_id': f'image-{number:06d}', 'image_id': image['id'],
                               'source_id': image.get('source_id', ''),
                               'objects': {f'object-{n:06d}': obj['id'] for n, obj in enumerate(image['objects'], 1)}})
        private = {'dataset_snapshot': dataset, 'options': options, 'source_map': source_map,
                   'warning': 'PRIVATE local provenance. Not part of downloadable training package.'}
        with (records / (export_id + '.private.json')).open('x', encoding='utf-8') as f:
            json.dump(private, f, ensure_ascii=False, allow_nan=False)
        with self.lock, self.db:
            self.db.execute('INSERT INTO exports VALUES(?,?,?,?,?,?)', (export_id, dataset['id'], now(), dataset['revision'], options['mode'], str(path)))

    def export_path(self, export_id):
        with self.lock:
            row = self.db.execute('SELECT path FROM exports WHERE id=?', (export_id,)).fetchone()
        if not row:
            raise KeyError('训练包不存在')
        return beneath(self.workspace, row[0])


def new_dataset(name, kind, coordinate):
    return dict(id=uid('ds-'), name=str(name).strip()[:150] or '未命名数据集', revision=1, created_at=now(),
                source_kind=kind, coordinate_mode=coordinate, mapping={'version': 1, 'name': '原始细分类', 'entries': []},
                rules={'research_permission': 'pending', 'permission_evidence': '', 'annotation_policy': 'unknown',
                       'annotation_evidence': '', 'task_definition': '', 'historical_exposure': 'unknown',
                       'coordinate_verified': 'pending', 'coordinate_evidence': ''},
                images=[], relations=[], split={'version': 0, 'confirmed': False, 'scope': 'component', 'assignments': {}, 'groups': {}})


def import_dataset(store, source_value, kind, name, coordinate, dictionary_value=None, progress=None):
    if coordinate not in {'zero', 'voc'}:
        raise ValueError('必须明确选择源坐标约定')
    if kind not in {'workset', 'voc'}:
        raise ValueError('首版支持 VOC 或已整理工作集')
    source = store.source(source_value)
    dataset = new_dataset(name or source.name, kind, coordinate)
    folder = store.workspace / 'datasets' / dataset['id']
    folder.mkdir(parents=True)
    imagefolder = source / ('images' if kind == 'workset' else 'JPEGImages')
    xmlfolder = source / ('annotations' if kind == 'workset' else 'Annotations')
    if not imagefolder.is_dir() or not xmlfolder.is_dir():
        raise ValueError('未找到图像与标注目录，请检查来源格式')
    dictionary = source / 'dictionary.csv' if kind == 'workset' and not dictionary_value else store.source(dictionary_value) if dictionary_value else None
    if dictionary is None or not dictionary.is_file():
        raise ValueError('请选择 code,name,role 类别字典 CSV')
    if kind == 'workset' and not dictionary_value:
        beneath(source, dictionary)
    entries = csv_rows(dictionary.read_text(encoding='utf-8-sig'))
    seen = set()
    for entry in entries:
        code = str(entry.get('code', '')).strip()
        if not code or code in seen or entry.get('role') not in {'cell', 'ignore', 'unresolved'} or not entry.get('name', '').strip():
            raise ValueError('类别字典必须提供唯一编码、名称和合法角色')
        seen.add(code)
        dataset['mapping']['entries'].append(dict(code=code, name=entry['name'], train_name=entry['name'] if entry['role'] == 'cell' else '', role=entry['role'], evidence='来自导入字典；医学定义由使用者核实'))
    meta_file = source / 'image_manifest.csv' if kind == 'workset' else source / 'metadata.csv'
    supplied = {r['image_id']: r for r in csv_rows(meta_file.read_text(encoding='utf-8-sig'))} if meta_file.is_file() else {}
    paths = sorted(p for p in imagefolder.iterdir() if p.suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'})
    if not paths:
        raise ValueError('来源没有可读取图像')
    if len({p.stem for p in paths}) != len(paths):
        raise ValueError('同名图像主干存在歧义')
    (folder / 'assets').mkdir()
    (folder / 'original_annotations').mkdir()
    shutil.copyfile(dictionary, folder / 'dictionary.original.csv')
    for number, path in enumerate(paths):
        beneath(source, path)
        key = f'im-{number + 1:06d}'
        target = folder / 'assets' / (key + path.suffix.lower())
        before = digest(path)
        shutil.copyfile(path, target)
        if digest(target) != before or digest(path) != before:
            raise ValueError('复制校验失败，源文件可能在入库期间发生变化')
        with Image.open(target) as pil:
            rgb = pil.convert('RGB')
            w, h = rgb.size
            pixel = hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()
        problems, objects = [], []
        xml = xmlfolder / (path.stem + '.xml')
        if xml.is_file():
            beneath(source, xml)
            original = folder / 'original_annotations' / (key + '.xml')
            xmlhash = digest(xml)
            shutil.copyfile(xml, original)
            if digest(original) != xmlhash or digest(xml) != xmlhash:
                raise ValueError('标注复制校验失败')
            try:
                if xml.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError('XML 文件过大')
                raw = xml.read_bytes()
                if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                    raise ValueError('不支持包含实体或 DTD 的 XML')
                root = ET.fromstring(raw)
                if (float(root.findtext('size/width', '0')), float(root.findtext('size/height', '0'))) != (w, h):
                    problems.append('image_xml_size_mismatch')
                for j, element in enumerate(root.findall('object')):
                    code = (element.findtext('name') or '').strip()
                    if code not in seen:
                        seen.add(code)
                        dataset['mapping']['entries'].append(dict(code=code, name='未定义编码 ' + code, train_name='', role='unresolved', evidence=''))
                    try:
                        box = [float(element.findtext('bndbox/' + k)) for k in ('xmin', 'ymin', 'xmax', 'ymax')]
                        if not all(math.isfinite(v) for v in box):
                            raise ValueError('nonfinite')
                        if coordinate == 'voc':
                            box[0] -= 1
                            box[1] -= 1
                    except (TypeError, ValueError):
                        box = [None] * 4
                    objects.append(dict(id=f'{key}-o{j+1:04d}', raw_code=code, code=code, bbox=box,
                                        deleted=False, delete_reason='', question='', crop_review='pending', crop_note='',
                                        truncated=element.findtext('truncated', '0') == '1', difficult=element.findtext('difficult', '0') == '1'))
            except (ET.ParseError, ValueError) as exc:
                problems.append('invalid_xml')
        else:
            problems.append('missing_annotation')
        row = supplied.get(path.stem, {})
        metadata = {k: str(row.get(k, '')).strip() for k in ('patient_id', 'slide_id', 'acquisition_batch_id', 'source_image_id', 'metadata_evidence', 'historical_exposure')}
        if any(metadata[k] for k in ('patient_id', 'slide_id', 'source_image_id')) and not metadata['metadata_evidence']:
            problems.append('identity_without_evidence')
        dataset['images'].append(dict(id=key, source_id=path.stem, asset_path=str(target.relative_to(store.workspace)).replace('\\', '/'),
                                     sha256=before, pixel_sha256=pixel, width=w, height=h, objects=objects,
                                     original_objects=json.loads(json.dumps(objects)), review_state='unseen', annotations_complete=False,
                                     exclude_reason='', note='', metadata=metadata, import_issues=problems))
        if progress:
            progress((number + 1) / len(paths), f'复制并校验 {number+1}/{len(paths)}')
    xml_orphans = [p.stem for p in xmlfolder.glob('*.xml') if p.stem not in {x.stem for x in paths}]
    dataset['import_report'] = {'orphan_annotations': xml_orphans, 'files': len(paths), 'source_unchanged_during_copy': True}
    # Normalize explicit source references after assigning generated image IDs.
    aliases = {i['source_id']: i['id'] for i in dataset['images']}
    for image in dataset['images']:
        value = image['metadata']['source_image_id']
        if value in aliases:
            image['metadata']['source_image_id'] = aliases[value]
    return store.create(dataset)


def create_demo(store, progress=None):
    dataset = new_dataset('合成示例 · 可训练导出演示', 'synthetic', 'zero')
    dataset['mapping']['entries'] = [dict(code=c, name='合成类别 ' + c, train_name='Synthetic ' + c, role='cell', evidence='几何合成示例') for c in ('A', 'B', 'C')]
    dataset['rules'].update(research_permission='verified', permission_evidence='本软件生成的几何合成图，不含患者资料', annotation_policy='exhaustive', annotation_evidence='所有生成目标均有矩形标注', task_definition='几何合成目标检测和分类', historical_exposure='synthetic_demo',coordinate_verified='verified',coordinate_evidence='生成坐标为零起点半开区间')
    folder = store.workspace / 'datasets' / dataset['id'] / 'assets'
    folder.mkdir(parents=True)
    for n in range(12):
        key = f'im-{n+1:06d}'
        image = Image.new('RGB', (800, 600), (240, 233, 233))
        draw = ImageDraw.Draw(image)
        objects = []
        for j in range(6):
            x, y = 55 + (j % 3) * 250 + n * 2, 75 + (j // 3) * 255
            box = [x, y, x+100, y+110]
            code = 'ABC'[(j+n)%3]
            draw.ellipse(box, fill={'A': (201, 182, 214), 'B': (205, 163, 187), 'C': (180, 191, 212)}[code])
            draw.ellipse((x+22, y+20, x+75, y+86), fill=(132, 105, 151))
            draw.text((12, 12), f'SYNTHETIC {n+1}', fill=(90, 90, 90))
            objects.append(dict(id=f'{key}-o{j+1:04d}', raw_code=code, code=code, bbox=box, deleted=False, delete_reason='', question='', crop_review='include', crop_note='合成对象规则已知', truncated=False, difficult=False))
        target = folder / (key + '.png')
        image.save(target)
        dataset['images'].append(dict(id=key, source_id=key, asset_path=str(target.relative_to(store.workspace)).replace('\\', '/'), sha256=digest(target),
                                     pixel_sha256=hashlib.sha256(str(image.size).encode()+image.tobytes()).hexdigest(), width=800, height=600,
                                     objects=objects, original_objects=json.loads(json.dumps(objects)), review_state='confirmed', annotations_complete=True,
                                     exclude_reason='', note='', import_issues=[], metadata={'patient_id': f'SYN-P{n//2+1:02d}', 'slide_id': f'SYN-S{n//2+1:02d}',
                                     'acquisition_batch_id': 'SYN-BATCH', 'source_image_id': '', 'metadata_evidence': '合成身份，仅示例', 'historical_exposure': 'synthetic_demo'}))
        if progress:
            progress((n+1)/12, f'生成合成图 {n+1}/12')
    return store.create(dataset)


def clear_split(dataset):
    dataset['split']['confirmed'] = False


def patch_image(dataset, image_id, data):
    from .exporter import image_issues
    image = next(i for i in dataset['images'] if i['id'] == image_id)
    def context(objects):
        return [(o['id'], o['code'], o['bbox'], bool(o.get('deleted')), bool(o.get('truncated'))) for o in objects]
    original_context = context(image['objects'])
    old = {o['id']: o for o in image['objects']}
    if 'objects' in data:
        objects = data['objects']
        if not isinstance(objects, list) or len(objects) > 10000:
            raise ValueError('对象清单无效')
        result, ids = [], set()
        for candidate in objects:
            key = str(candidate.get('id', ''))
            if key in ids:
                raise ValueError('对象 ID 重复')
            ids.add(key)
            if key not in old and not key.startswith('new-'):
                raise ValueError('新增对象需要新的临时 ID')
            if key in old:
                obj = dict(old[key])
                if candidate.get('raw_code', obj['raw_code']) != obj['raw_code']:
                    raise ValueError('原始标签不能修改')
            else:
                obj = dict(id=uid('obj-'), raw_code='[manual]', code='', bbox=[None]*4, deleted=False, delete_reason='', question='', crop_review='pending', crop_note='', truncated=False, difficult=False)
            for field in ('code', 'delete_reason', 'question', 'crop_review', 'crop_note'):
                if field in candidate:
                    obj[field] = str(candidate[field])[:4000]
            for field in ('deleted', 'truncated', 'difficult'):
                if field in candidate:
                    if not isinstance(candidate[field], bool):
                        raise ValueError('对象标志必须为布尔值')
                    obj[field] = candidate[field]
            if 'bbox' in candidate:
                box = candidate['bbox']
                if not isinstance(box, list) or len(box) != 4 or any(v is not None and (not isinstance(v, (int, float)) or not math.isfinite(v)) for v in box):
                    raise ValueError('框坐标必须是四个有限数值')
                obj['bbox'] = box
            if obj['crop_review'] not in {'pending', 'include', 'exclude'}:
                raise ValueError('分类裁剪状态无效')
            if obj['deleted'] and not obj['delete_reason'].strip():
                raise ValueError('删除框必须填写原因')
            if key in old and (obj['bbox'] != old[key]['bbox'] or obj['code'] != old[key]['code']):
                obj['crop_review'] = 'pending'
            result.append(obj)
        if not set(old).issubset(ids):
            raise ValueError('必须保留原对象记录，通过软删除移除框')
        image['objects'] = result
        if context(result) != original_context:
            for obj in image['objects']:
                obj['crop_review'] = 'pending'
    for field in ('note', 'exclude_reason'):
        if field in data:
            image[field] = str(data[field])[:10000]
    if 'annotations_complete' in data:
        if not isinstance(data['annotations_complete'], bool):
            raise ValueError('标注完整性必须明确确认')
        image['annotations_complete'] = data['annotations_complete']
    if data.get('acknowledge_import_repair'):
        evidence = str(data.get('repair_evidence', '')).strip()
        if not evidence or not image['annotations_complete']:
            raise ValueError('结构修复需要依据并确认整图标注完整')
        for obj in image['objects']:
            if obj.get('deleted'):
                continue
            box = obj['bbox']
            if not all(isinstance(x, (int, float)) for x in box) or not (0<=box[0]<box[2]<=image['width'] and 0<=box[1]<box[3]<=image['height']):
                raise ValueError('需先修复全部无效框')
        repaired = {'missing_annotation', 'invalid_xml', 'image_xml_size_mismatch'}
        previous = image.get('import_issues', [])
        image.setdefault('repair_records', []).append({'at': now(), 'issues': [x for x in previous if x in repaired], 'evidence': evidence})
        image['import_issues'] = [x for x in previous if x not in repaired]
    image['review_state'] = 'excluded' if image['exclude_reason'] else 'in_progress'
    clear_split(dataset)
    if data.get('review_state') == 'confirmed':
        # Cache invalidation for exporter duplicate indexes before revision increments.
        checking = dict(dataset, revision=dataset['revision'] + 1)
        problems = image_issues(checking, image)
        if problems or not image['annotations_complete'] or image['exclude_reason']:
            raise ValueError('无法确认：请解决阻断问题并确认本图标注完整性')
        image['review_state'] = 'confirmed'
    dataset['last_image_id'] = image_id


def patch_mapping(dataset, data):
    value = data.get('mapping', {})
    entries = value.get('entries', [])
    if not entries:
        raise ValueError('类别方案不能为空')
    seen = set()
    for row in entries:
        code = str(row.get('code', '')).strip()
        if not code or code in seen or row.get('role') not in {'cell', 'ignore', 'unresolved', 'out_of_scope'}:
            raise ValueError('类别编码重复或处理方式无效')
        if not row.get('name', '').strip():
            raise ValueError('类别名称不能为空')
        if row['role'] in {'cell', 'out_of_scope'} and not row.get('evidence', '').strip():
            raise ValueError('纳入训练或任务外类别必须记录定义依据')
        if row['role'] == 'cell' and not row.get('train_name', '').strip():
            raise ValueError('训练类别名称不能为空')
        seen.add(code)
        row['code'] = code
        for field in ('name', 'train_name', 'evidence'):
            row[field] = str(row.get(field, '')).strip()[:10000]
    oldcodes = {r['code'] for r in dataset['mapping']['entries']}
    if not oldcodes.issubset(seen):
        raise ValueError('不能删除原始编码；请修改处理方式')
    dataset['mapping'] = {'version': dataset['mapping']['version']+1, 'name': str(value.get('name', '类别方案'))[:150], 'entries': entries}
    dataset.setdefault('mapping_schemes', []).append(copy.deepcopy(dataset['mapping']))
    for image in dataset['images']:
        if image['review_state'] == 'confirmed':
            image['review_state'] = 'in_progress'
        for obj in image['objects']:
            obj['crop_review'] = 'pending'
    clear_split(dataset)


def patch_rules(dataset, data):
    rules = data.get('rules', {})
    for k in dataset['rules']:
        if k in rules:
            dataset['rules'][k] = str(rules[k])[:12000]
    if dataset['rules']['research_permission'] not in {'verified', 'pending', 'not_permitted'}:
        raise ValueError('研究授权状态无效')
    if dataset['rules']['annotation_policy'] not in {'unknown', 'exhaustive'}:
        raise ValueError('标注规则无效')
    if dataset['rules'].get('coordinate_verified') not in {'pending', 'verified'}:
        raise ValueError('坐标核对状态无效')
    clear_split(dataset)


def import_metadata(dataset, text):
    rows = csv_rows(text)
    byid = {i['id']: i for i in dataset['images']}
    aliases = {i.get('source_id', i['id']): i['id'] for i in dataset['images']}
    seen = set()
    for row in rows:
        supplied_id = row.get('image_id', '')
        key = supplied_id if supplied_id in byid else aliases.get(supplied_id)
        if key not in byid or key in seen:
            raise ValueError('身份 CSV 包含未知或重复图像 ID')
        seen.add(key)
        m = dict(byid[key]['metadata'])
        for field in ('patient_id', 'slide_id', 'source_image_id', 'acquisition_batch_id', 'metadata_evidence', 'historical_exposure'):
            if field in row:
                m[field] = row[field].strip()
        if any(m.get(k) for k in ('patient_id', 'slide_id', 'source_image_id', 'acquisition_batch_id')) and not m.get('metadata_evidence'):
            raise ValueError('身份与来源信息必须有证据')
        if m.get('source_image_id') in aliases:
            m['source_image_id'] = aliases[m['source_image_id']]
        byid[key]['metadata'] = m
        byid[key]['import_issues'] = [x for x in byid[key].get('import_issues', []) if x != 'identity_without_evidence']
    slides = collections.defaultdict(set)
    for image in dataset['images']:
        m = image['metadata']
        if m.get('slide_id') and m.get('patient_id'):
            slides[m['slide_id']].add(m['patient_id'])
    if any(len(s)>1 for s in slides.values()):
        raise ValueError('同一玻片出现不同患者，需先解决身份冲突')
    clear_split(dataset)
