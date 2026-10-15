"""Create a versioned, independently copied VOC workset; never rewrite a source.

Python 3.11+. Pillow is optional for decoded-pixel duplicate checks and thumbnails.
No network, model inference, medical label guessing, or automatic split assignment.
"""
import argparse
import collections
import csv
import hashlib
import html
import json
import math
from pathlib import Path
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def csv_read(path):
    if path is None:
        return []
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def csv_write(path, rows, columns):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


class Groups:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def root(self, i):
        while i != self.parent[i]:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a, b):
        a, b = self.root(a), self.root(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def build(args):
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Source and workset must be separate, non-nested directories')
    if output.exists():
        raise ValueError('Use a new output directory; existing versions are never overwritten')
    if args.dictionary.resolve().is_relative_to(output):
        raise ValueError('Dictionary must exist outside the new output')
    dictionary = {}
    for row in csv_read(args.dictionary):
        code = row['code'].strip()
        if not code or code in dictionary or row['role'] not in {'cell', 'ignore', 'unresolved'}:
            raise ValueError('Dictionary requires unique code, name and role cell/ignore/unresolved')
        if not row['name'].strip():
            raise ValueError('Dictionary name must not be empty')
        dictionary[code] = row
    annotations = sorted((source / 'Annotations').glob('*.xml'))
    if not annotations:
        raise ValueError('No Annotations/*.xml found')
    image_paths = sorted(p for p in (source / 'JPEGImages').iterdir()
                         if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    bystem = {}
    for p in image_paths:
        if p.stem in bystem:
            raise ValueError('Ambiguous image stem: ' + p.stem)
        bystem[p.stem] = p
    ids = [p.stem for p in annotations]
    metadata = {}
    for row in csv_read(args.metadata):
        i = row['image_id']
        if i not in ids or i in metadata:
            raise ValueError('Unknown or duplicate metadata image_id')
        for key in ('patient_id', 'slide_id', 'acquisition_batch_id', 'source_image_id'):
            row[key] = row.get(key, '').strip()
        if any(row.get(k) for k in ('patient_id', 'slide_id', 'acquisition_batch_id', 'source_image_id')) and not row.get('metadata_evidence', '').strip():
            raise ValueError('Supplied identity/source IDs require metadata_evidence')
        metadata[i] = row
    slide_patients = collections.defaultdict(set)
    for m in metadata.values():
        if m.get('slide_id') and m.get('patient_id'):
            slide_patients[m['slide_id']].add(m['patient_id'])
    if any(len(v) > 1 for v in slide_patients.values()):
        raise ValueError('Conflicting slide/patient mapping')
    try:
        from PIL import Image
    except ImportError:
        Image = None
    output.mkdir(parents=True)
    for folder in ['images', 'annotations', 'review', 'thumbnails']:
        (output / folder).mkdir()
    # Freeze input configuration as supplied; do not claim its semantics are verified.
    config_files = [('dictionary.csv', args.dictionary), ('metadata_supplied.csv', args.metadata)]
    config_fingerprints = {}
    for name, path in config_files:
        if path:
            before = digest(path)
            shutil.copyfile(path, output / name)
            if digest(path) != before or digest(output / name) != before:
                raise RuntimeError('Input configuration changed during intake')
            config_fingerprints[name] = before
    manifests, objects, issues, source_files, browse = [], [], [], [], []
    groups = Groups(ids)
    seen = {}
    counts = collections.Counter()
    supports = collections.defaultdict(set)
    missing_images = []
    start = time.monotonic()

    def add_issue(i, kind, detail='', idx=''):
        issues.append(dict(image_id=i, object_idx=idx, issue=kind, detail=detail,
                           resolution='', reviewer='', evidence=''))

    def copy_checked(path, target):
        before = digest(path)
        shutil.copyfile(path, target)
        if digest(target) != before or digest(path) != before:
            raise RuntimeError('Source changed or copied bytes differ: ' + path.name)
        source_files.append(dict(source_relative_path=str(path.relative_to(source)),
                                 working_relative_path=str(target.relative_to(output)), sha256=before))
        return before

    for index, xml in enumerate(annotations):
        i = xml.stem
        xml_hash = copy_checked(xml, output / 'annotations' / xml.name)
        p = bystem.get(i)
        if p is None:
            missing_images.append(i)
            add_issue(i, 'missing_image')
            continue
        image_hash = copy_checked(p, output / 'images' / p.name)
        root = ET.parse(xml).getroot()
        w, h = float(root.findtext('size/width', '0')), float(root.findtext('size/height', '0'))
        if not all(math.isfinite(v) and v > 0 for v in (w, h)):
            raise ValueError('Invalid XML dimensions: ' + xml.name)
        pixel_hash = ''
        if Image:
            with Image.open(p) as im:
                rgb = im.convert('RGB')
                pixel_hash = hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()
                if rgb.size != (w, h):
                    add_issue(i, 'image_xml_size_mismatch')
                if args.thumbnails:
                    rgb.thumbnail((420, 420))
                    rgb.save(output / 'thumbnails' / (i + '.jpg'), quality=82)
        local = []
        for obj_index, ob in enumerate(root.findall('object')):
            code = (ob.findtext('name') or '').strip()
            definition = dictionary.get(code, dict(name='UNRESOLVED', role='unresolved'))
            role = definition['role']
            counts[code] += 1
            supports[code].add(i)
            bb = ob.find('bndbox')
            try:
                b = [float(bb.findtext(k)) for k in ('xmin', 'ymin', 'xmax', 'ymax')]
                if not all(math.isfinite(v) for v in b):
                    raise ValueError('Non-finite coordinates')
                valid = 0 <= b[0] < b[2] <= w and 0 <= b[1] < b[3] <= h
            except (TypeError, ValueError, AttributeError):
                b = [None] * 4
                valid = False
            edge = valid and (b[0] <= 2 or b[1] <= 2 or b[2] >= w - 2 or b[3] >= h - 2)
            row = dict(image_id=i, object_idx=obj_index, raw_code=code, cell_name=definition['name'],
                       role=role, xmin=b[0], ymin=b[1], xmax=b[2], ymax=b[3],
                       valid_box=int(valid), edge_2px=int(edge), truncated=ob.findtext('truncated', '0'),
                       difficult=ob.findtext('difficult', '0'), annotation_verification='not_established')
            objects.append(row)
            local.append(row)
            if role == 'unresolved':
                add_issue(i, 'unresolved_code', code, obj_index)
            if not valid:
                add_issue(i, 'invalid_box', code, obj_index)
        for field, value in [('file_hash', image_hash), ('pixel_hash', pixel_hash),
                             *[(k, metadata.get(i, {}).get(k, '')) for k in ('patient_id', 'slide_id', 'source_image_id')]]:
            if value:
                key = (field, value)
                if key in seen:
                    groups.join(i, seen[key])
                else:
                    seen[key] = i
        m = metadata.get(i, {})
        manifests.append(dict(image_id=i, source_image_path=str(p.relative_to(source)),
          image_path='images/' + p.name, annotation_path='annotations/' + xml.name,
          image_sha256=image_hash, annotation_sha256=xml_hash, pixel_sha256=pixel_hash,
          width=w, height=h, objects=len(local),
          **{k: m.get(k, '') for k in ('patient_id', 'slide_id', 'acquisition_batch_id', 'source_image_id', 'metadata_evidence')},
          annotations_complete=m.get('annotations_complete', 'unknown'), historical_exposure=m.get('historical_exposure', 'unknown'),
          group_id='', split='unassigned'))
        browse.append(dict(id=i, image='images/' + quote(p.name, safe=''), thumbnail='thumbnails/' + quote(i + '.jpg', safe='') if args.thumbnails and Image else '',
                           width=w, height=h, objects=local))
        if (index + 1) % 100 == 0:
            print(f'copied and verified {index + 1}/{len(annotations)}', flush=True)
    for row in manifests:
        row['group_id'] = 'component_' + groups.root(row['image_id'])
    byhash = collections.defaultdict(list)
    for row in manifests:
        byhash[row['pixel_sha256'] or row['image_sha256']].append(row)
    for duplicate in byhash.values():
        if len(duplicate) > 1:
            annotation_signatures = []
            for row in duplicate:
                obs = [o for o in objects if o['image_id'] == row['image_id']]
                annotation_signatures.append(sorted((o['raw_code'], str([o[k] for k in ['xmin','ymin','xmax','ymax']]), o['difficult'], o['truncated']) for o in obs))
            conflict = any(x != annotation_signatures[0] for x in annotation_signatures[1:])
            for row in duplicate:
                add_issue(row['image_id'], 'duplicate_annotation_conflict' if conflict else 'exact_duplicate',
                          ';'.join(x['image_id'] for x in duplicate))
    orphans = sorted(set(bystem) - set(ids))
    for i in orphans:
        add_issue(i, 'image_without_xml', str(bystem[i].relative_to(source)))
    labels = [dict(code=k, name=dictionary.get(k, {}).get('name', 'UNRESOLVED'),
                   role=dictionary.get(k, {}).get('role', 'unresolved'), boxes=v,
                   images=len(supports[k]), supporting_components=len({groups.root(i) for i in supports[k]}))
              for k, v in sorted(counts.items())]
    csv_write(output / 'image_manifest.csv', manifests, list(manifests[0]) if manifests else ['image_id'])
    csv_write(output / 'object_manifest.csv', objects, list(objects[0]) if objects else ['image_id'])
    csv_write(output / 'label_counts.csv', labels, ['code','name','role','boxes','images','supporting_components'])
    csv_write(output / 'review' / 'issues.csv', issues, ['image_id','object_idx','issue','detail','resolution','reviewer','evidence'])
    csv_write(output / 'source_snapshot.csv', source_files, ['source_relative_path','working_relative_path','sha256'])
    identity_columns = ['image_id','patient_id','slide_id','acquisition_batch_id','source_image_id','metadata_evidence','annotations_complete','historical_exposure']
    csv_write(output / 'identity_to_complete.csv', manifests, identity_columns)
    identity_scope = 'patient' if manifests and all(x['patient_id'] for x in manifests) else 'slide' if manifests and all(x['slide_id'] for x in manifests) else 'unknown_identity_components_only'
    blocking = ['confirm source permission/ethics applicability and documentation',
                'review annotation completeness and ignore-region evaluation semantics',
                'review near-duplicate/overlapping fields; scan not implemented here',
                'confirm historical exposure and freeze evaluation protocol']
    if identity_scope == 'unknown_identity_components_only':
        blocking.append('patient/slide mapping incomplete: cannot claim patient-independent evaluation')
    if any(r['role'] == 'unresolved' for r in labels):
        blocking.append('unresolved label codes')
    if any(r['issue'] == 'duplicate_annotation_conflict' for r in issues):
        blocking.append('duplicate images have conflicting annotations')
    if missing_images or any(r['issue'] in {'invalid_box','image_xml_size_mismatch'} for r in issues):
        blocking.append('invalid image/annotation structure')
    summary = dict(schema_version='1.0', status='provisional_review_required',
                   source_root=str(source), images=len(manifests), xml_files=len(annotations),
                   raw_boxes=len(objects), unique_file_hashes=len({x['image_sha256'] for x in manifests}),
                   unique_pixel_hashes=len(byhash), pixel_check_available=bool(Image),
                   identity_scope=identity_scope, known_components=len({r['group_id'] for r in manifests}),
                   images_without_xml=orphans, missing_images=missing_images,
                   issue_counts=dict(collections.Counter(r['issue'] for r in issues)),
                   dictionary_sha256=config_fingerprints.get('dictionary.csv'),
                   config_fingerprints=config_fingerprints,
                   snapshot_sha256=digest(output / 'source_snapshot.csv'), script_sha256=digest(Path(__file__)),
                   source_copy_integrity='each copied file byte-verified; original rehashed during copy',
                   splits='not_assigned', blocking_review=blocking,
                   elapsed_s=round(time.monotonic()-start, 2), python=sys.version.split()[0])
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    # Local HTML uses a data script rather than fetch(), so file:// works offline.
    issues_by_image = collections.defaultdict(list)
    for issue in issues:
        issues_by_image[issue['image_id']].append(issue['issue'])
    for image in browse:
        image['issues'] = sorted(set(issues_by_image[image['id']]))
    payload = json.dumps(dict(labels=labels, images=browse), ensure_ascii=False, allow_nan=False).replace('<', '\\u003c')
    template = Path(__file__).with_name('viewer.html').read_text(encoding='utf-8')
    (output / 'browse.html').write_text(template.replace('__DATA__', payload), encoding='utf-8')
    (output / 'review' / 'SOURCE_AND_USE.md').write_text(
        '# Source and use record\n\nProvider: pending\nResearch permission: pending\n'
        'Ethics approval/exemption and applicability: pending\nAcquisition/annotation contributors: pending\n'
        'License / permitted sharing scope: pending\nPatient/slide evidence: pending\n'
        'Historical model exposure: pending\n\nKeep this record local. A file copy does not establish research permission.\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--dictionary', type=Path, required=True)
    p.add_argument('--metadata', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--thumbnails', action='store_true')
    build(p.parse_args())


if __name__ == '__main__':
    main()
