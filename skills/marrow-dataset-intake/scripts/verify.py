"""Recheck a workset against its source snapshot without changing either dataset."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def contained(root, relative):
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Snapshot path escapes expected root')
    return path


def verify(source, workset):
    summary = json.loads((workset / 'summary.json').read_text(encoding='utf-8'))
    if digest(workset / 'source_snapshot.csv') != summary['snapshot_sha256']:
        raise ValueError('Snapshot content differs from recorded fingerprint')
    with (workset / 'source_snapshot.csv').open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    for index, r in enumerate(rows):
        original = contained(source, r['source_relative_path'])
        copy = contained(workset, r['working_relative_path'])
        if digest(original) != r['sha256'] or digest(copy) != r['sha256']:
            raise ValueError('Integrity mismatch: ' + r['working_relative_path'])
        if original.samefile(copy):
            raise ValueError('Working file is not an independent copy')
        if (index + 1) % 500 == 0:
            print('verified', index + 1, '/', len(rows), flush=True)
    for name, expected in summary.get('config_fingerprints', {}).items():
        if digest(contained(workset, name)) != expected:
            raise ValueError('Input configuration snapshot mismatch')
    return dict(status='pass', source_and_copy_files_checked=len(rows),
                independent_copies=True, copied_bytes_match_original_snapshot=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--workset', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(verify(a.source, a.workset), indent=2))
