"""Behavioral checks use only generated non-medical images and labels."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


intake = load('intake', ROOT / 'skills/marrow-dataset-intake/scripts/intake.py')
verifier = load('verify', ROOT / 'skills/marrow-dataset-intake/scripts/verify.py')
installer = load('installer', ROOT / 'install.py')


class IntakeTests(unittest.TestCase):
    def test_nonfinite_box_and_url_safe_viewer(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';(source/'JPEGImages').mkdir(parents=True);(source/'Annotations').mkdir()
            Image.new('RGB',(20,20)).save(source/'JPEGImages/cell#1.png')
            (source/'Annotations/cell#1.xml').write_text('<annotation><size><width>20</width><height>20</height></size><object><name>a</name><bndbox><xmin>nan</xmin><ymin>0</ymin><xmax>10</xmax><ymax>10</ymax></bndbox></object></annotation>')
            dictionary=root/'dict.csv';dictionary.write_text('code,name,role\na,Example,cell\n')
            result=intake.build(types.SimpleNamespace(source=source,output=root/'work',dictionary=dictionary,metadata=None,thumbnails=True))
            self.assertEqual(result['issue_counts']['invalid_box'],1)
            import re
            viewer=(root/'work/browse.html').read_text(encoding='utf-8')
            data=json.loads(re.search(r'<script id="dataset" type="application/json">(.*?)</script>',viewer,re.S).group(1))
            self.assertEqual(data['images'][0]['image'],'images/cell%231.png')
            self.assertEqual(data['images'][0]['objects'][0]['xmin'],None)

    def test_duplicate_difficult_flags_are_conflicts(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';(source/'JPEGImages').mkdir(parents=True);(source/'Annotations').mkdir()
            for i in [0,1]:
                Image.new('RGB',(20,20)).save(source/'JPEGImages'/f'{i}.png')
                (source/'Annotations'/f'{i}.xml').write_text(f'<annotation><size><width>20</width><height>20</height></size><object><name>a</name><difficult>{i}</difficult><bndbox><xmin>1</xmin><ymin>1</ymin><xmax>10</xmax><ymax>10</ymax></bndbox></object></annotation>')
            dictionary=root/'dict.csv';dictionary.write_text('code,name,role\na,Example,cell\n')
            result=intake.build(types.SimpleNamespace(source=source,output=root/'work',dictionary=dictionary,metadata=None,thumbnails=False))
            self.assertEqual(result['issue_counts']['duplicate_annotation_conflict'],2)

    def test_copy_unknown_ignore_duplicate_and_integrity(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            (source / 'JPEGImages').mkdir(parents=True)
            (source / 'Annotations').mkdir()
            for i, code in [('a','cell_a'), ('b','not_defined'), ('c','skip')]:
                Image.new('RGB', (80,60), (30,100,180)).save(source/'JPEGImages'/f'{i}.png')
                (source/'Annotations'/f'{i}.xml').write_text(
                    f'<annotation><size><width>80</width><height>60</height></size><object><name>{code}</name>'
                    '<bndbox><xmin>0</xmin><ymin>1</ymin><xmax>40</xmax><ymax>50</ymax></bndbox></object></annotation>')
            dictionary = root/'dictionary.csv'
            dictionary.write_text('code,name,role\ncell_a,Synthetic cell,cell\nskip,Not supervised,ignore\n')
            before={str(p):intake.digest(p) for p in source.rglob('*') if p.is_file()}
            args=types.SimpleNamespace(source=source, output=root/'work', dictionary=dictionary, metadata=None, thumbnails=True)
            result=intake.build(args)
            self.assertEqual(result['status'],'provisional_review_required')
            self.assertEqual(result['images'],3)
            self.assertEqual(result['known_components'],1)
            self.assertEqual(result['unique_pixel_hashes'],1)
            self.assertEqual(result['issue_counts']['unresolved_code'],1)
            self.assertEqual(result['issue_counts']['duplicate_annotation_conflict'],3)
            self.assertEqual(result['raw_boxes'],3)
            self.assertEqual(before,{str(p):intake.digest(p) for p in source.rglob('*') if p.is_file()})
            self.assertEqual(verifier.verify(source,args.output)['status'],'pass')
            with (args.output/'object_manifest.csv').open(encoding='utf-8-sig') as f:objects=list(csv.DictReader(f))
            self.assertEqual({x['role'] for x in objects},{'cell','unresolved','ignore'})
            with self.assertRaises(ValueError): intake.build(args)
            copied=args.output/'images/a.png'
            copied.write_bytes(b'changed work copy')
            self.assertEqual(intake.digest(source/'JPEGImages/a.png'),before[str(source/'JPEGImages/a.png')])
            with self.assertRaises(ValueError):verifier.verify(source,args.output)

    def test_install_preserves_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);config=home/'config.toml';config.write_text('model = "example-model"\n')
            installer.install(ROOT,home)
            import tomllib
            result=tomllib.loads(config.read_text())
            self.assertEqual(result['model'],'example-model')
            self.assertEqual(len(result['agents']),3)
            self.assertEqual(len(list(home.glob('config.toml.before-marrow-*.bak'))),1)
            installed=home/'skills/marrow-dataset-intake/scripts/intake.py'
            self.assertTrue(installed.exists())
            before=config.read_bytes()
            with self.assertRaises(ValueError):installer.install(ROOT,home)
            self.assertEqual(before,config.read_bytes())


if __name__=='__main__':unittest.main()
