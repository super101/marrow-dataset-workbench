"""Persistent workflow and local HTTP boundaries, with generated data only."""
import copy
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from PIL import Image

from workbench.core import Store, Conflict, create_demo, digest, import_dataset, patch_image, patch_mapping
from workbench.server import serve


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'workspace', [self.root/'source'])
        self.dataset = create_demo(self.store)
        self.server = serve(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.open(self.base+'/').read()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.app.executor.shutdown(wait=True)
        self.thread.join()
        self.store.db.close()
        self.tmp.cleanup()

    def request(self, suffix, data=None, method=None, headers=None):
        h = dict(headers or {})
        if data is not None:
            h.update({'Content-Type': 'application/json', 'X-Workbench': '1'})
        req = urllib.request.Request(self.base+suffix, json.dumps(data).encode() if data is not None else None, h, method=method)
        return json.loads(self.opener.open(req).read())

    def test_edit_restart_stale_write_and_confirmation(self):
        ds = self.dataset
        image = copy.deepcopy(ds['images'][0])
        raw_code = image['objects'][0]['raw_code']
        asset = self.store.workspace/image['asset_path']
        original = digest(asset)
        image['objects'][0]['bbox'][0] += 4
        path = f"/api/datasets/{ds['id']}/images/{image['id']}"
        result = self.request(path, {'expected_revision': 1, 'objects': image['objects']}, method='PATCH')
        self.assertEqual(result['dataset']['revision'], 2)
        self.assertEqual(result['image']['review_state'], 'in_progress')
        self.assertFalse(result['dataset']['split']['confirmed'])
        self.assertEqual(result['image']['objects'][0]['raw_code'], raw_code)
        self.assertEqual(digest(asset), original)
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.request(path, {'expected_revision': 1, 'objects': image['objects']}, method='PATCH')
        self.assertEqual(err.exception.code, 409)
        second = Store(self.store.workspace)
        try:
            self.assertEqual(second.get(ds['id'])['images'][0]['objects'][0]['bbox'], image['objects'][0]['bbox'])
        finally:
            second.db.close()
        result = self.request(path, {'expected_revision': 2, 'annotations_complete': True, 'review_state': 'confirmed'}, method='PATCH')
        self.assertEqual(result['image']['review_state'], 'confirmed')
        altered = copy.deepcopy(result['image']['objects'])
        altered[0]['raw_code'] = 'FORGED'
        with self.assertRaises(urllib.error.HTTPError):
            self.request(path, {'expected_revision': 3, 'objects': altered}, method='PATCH')
        self.assertEqual(self.store.get(ds['id'])['revision'], 3)

    def test_origin_session_host_and_path_rejections(self):
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(self.base+'/api/bootstrap')
        self.assertEqual(err.exception.code, 403)
        for headers in [{'Origin': 'https://example.invalid'}, {'Host': 'remote.invalid'}, {'Sec-Fetch-Site': 'cross-site'}]:
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/api/bootstrap', headers=headers)
            self.assertEqual(err.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.request('/static/../../workbench.sqlite3')
        self.assertEqual(err.exception.code, 403 if err.exception.code == 403 else 400)
        req = urllib.request.Request(self.base+'/api/datasets/demo', b'{}', {'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.opener.open(req)
        self.assertEqual(err.exception.code, 403)

    def test_voc_import_copy_coordinates_and_missing_annotations(self):
        source = self.root/'source'
        (source/'JPEGImages').mkdir(parents=True)
        (source/'Annotations').mkdir()
        Image.new('RGB',(20,20),(1,2,3)).save(source/'JPEGImages'/'a.png')
        Image.new('RGB',(20,20),(4,5,6)).save(source/'JPEGImages'/'unlabeled.png')
        xml = source/'Annotations'/'a.xml'
        xml.write_text('<annotation><size><width>20</width><height>20</height></size><object><name>01</name><bndbox><xmin>1</xmin><ymin>1</ymin><xmax>1</xmax><ymax>1</ymax></bndbox></object></annotation>')
        dictionary=source/'dict.csv';dictionary.write_text('code,name,role\n01,Synthetic,cell\n')
        before={p.name:digest(p) for p in source.rglob('*') if p.is_file()}
        ds=import_dataset(self.store,str(source),'voc','Example','voc',str(dictionary))
        self.assertEqual(ds['images'][0]['objects'][0]['bbox'],[0,0,1,1])
        self.assertEqual(ds['images'][0]['objects'][0]['raw_code'],'01')
        self.assertIn('missing_annotation',ds['images'][1]['import_issues'])
        self.assertEqual(before,{p.name:digest(p) for p in source.rglob('*') if p.is_file()})
        self.assertEqual(ds['rules']['research_permission'],'pending')
        outside=self.root/'not-allowed';outside.mkdir()
        with self.assertRaises(ValueError):self.store.source(outside)

    def test_split_export_snapshot_job_and_zip(self):
        ds=self.dataset
        plan=self.request(f"/api/datasets/{ds['id']}/split",dict(expected_revision=1,scope='patient',ratios=[.6,.2,.2],seed=2,confirm=True))
        options=dict(expected_revision=plan['dataset']['revision'],mode='strict',formats=['voc','coco','yolo'],classification=True,crop_margin=0,min_crop_size=8)
        report=self.request(f"/api/datasets/{ds['id']}/export-check",options)
        self.assertTrue(report['ready'],report)
        job=self.request(f"/api/datasets/{ds['id']}/exports",options)
        self.server.app.executor.shutdown(wait=True)
        result=self.request('/api/jobs/'+job['job_id'])
        self.assertEqual(result['status'],'complete',result)
        archive=self.store.export_path(result['result']['export_id'])
        import zipfile
        with zipfile.ZipFile(archive) as z:
            names=z.namelist()
            self.assertIn('manifest.json',names)
            self.assertTrue(any(n.startswith('classification/train/') for n in names))
            manifest=z.read('manifest.json')
            self.assertNotIn(str(self.store.workspace).encode(),manifest)
        self.assertEqual(len(self.store.exports(ds['id'])),1)

    def test_crop_context_invalidates_all_confirmations_and_keeps_schemes(self):
        ds=self.dataset
        image=copy.deepcopy(ds['images'][0])
        image['objects'][0]['bbox'][0]+=2
        result=self.store.update(ds['id'],1,'edit',lambda d:patch_image(d,image['id'],{'objects':image['objects']}),image['id'])
        self.assertTrue(all(o['crop_review']=='pending' for o in result['images'][0]['objects']))
        self.assertTrue(all(o['crop_review']=='include' for o in result['images'][1]['objects']))
        mapping=copy.deepcopy(result['mapping'])
        mapping['name']='Alternative'
        mapping['entries'][0]['code']=' A '
        result=self.store.update(ds['id'],2,'mapping',lambda d:patch_mapping(d,{'mapping':mapping}))
        self.assertEqual(result['mapping']['entries'][0]['code'],'A')
        self.assertEqual(len(result['mapping_schemes']),2)
        self.assertTrue(all(o['crop_review']=='pending' for i in result['images'] for o in i['objects']))
        self.assertEqual(self.dataset['images'][0]['objects'][0]['bbox'][0],55, 'Cached old snapshot must remain immutable')

    def test_explicit_import_repair_and_cancellation(self):
        from workbench.server import Cancelled
        ds=self.dataset
        image=ds['images'][0]
        result=self.store.update(ds['id'],1,'synthetic damaged annotation',lambda d:d['images'][0].update(import_issues=['missing_annotation']))
        with self.assertRaises(ValueError):
            self.store.update(ds['id'],2,'repair',lambda d:patch_image(d,image['id'],{'acknowledge_import_repair':True,'annotations_complete':True}))
        result=self.store.update(ds['id'],2,'repair',lambda d:patch_image(d,image['id'],{'acknowledge_import_repair':True,'annotations_complete':True,'repair_evidence':'Synthetic reviewed exhaustive boxes'}))
        self.assertEqual(result['images'][0]['import_issues'],[])
        self.assertEqual(len(result['images'][0]['repair_records']),1)
        entered,release=threading.Event(),threading.Event()
        def task(progress):
            entered.set();release.wait(timeout=3);progress(.5,'check cancellation')
        job=self.server.app.job(task)
        self.assertTrue(entered.wait(timeout=3))
        self.request('/api/jobs/'+job['job_id']+'/cancel',{})
        release.set()
        self.server.app.executor.shutdown(wait=True)
        self.assertEqual(self.server.app.jobs[job['job_id']]['status'],'cancelled')


if __name__=='__main__':unittest.main()
