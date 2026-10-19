"""Leakage-aware grouping and auditable, atomic training exports.

Internal boxes are zero-based half-open. No source file is ever modified.
Only Pillow and the Python standard library are required.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path
import random
import shutil
import tempfile
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

from PIL import Image

SPLITS = ("train", "val", "test")
_INDEX_CACHE = []


def _entries(dataset):
    return {str(x["code"]): x for x in dataset.get("mapping", {}).get("entries", [])}


def _live(image):
    return [x for x in image.get("objects", []) if not x.get("deleted")]


def _valid_box(box, width, height):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in box)
            and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)


def _signature(image):
    return json.dumps(sorted(json.dumps((str(o.get("code", "")), o.get("bbox"),
                              bool(o.get("truncated")), bool(o.get("difficult"))), sort_keys=True)
                             for o in _live(image)))


def _index(dataset):
    """Snapshots are immutable within a revision; avoid quadratic list browsing."""
    revision = dataset.get("revision")
    for item in _INDEX_CACHE:
        if item[0] is dataset and item[1] == revision:
            return item[2]
    buckets = {}
    for im in dataset.get("images", []):
        if im.get("exclude_reason"):
            continue
        for field in ("sha256", "pixel_sha256"):
            value = im.get(field)
            if value:
                buckets.setdefault((field, value), []).append(im)
    conflicts = set()
    for bucket in buckets.values():
        if len(bucket) > 1 and len({_signature(im) for im in bucket}) > 1:
            conflicts.update(im["id"] for im in bucket)
    result = {"conflicts": conflicts, "mapping": _entries(dataset)}
    _INDEX_CACHE.append((dataset, revision, result))
    del _INDEX_CACHE[:-4]
    return result


def image_issues(dataset, image):
    """Return structural errors and advisory review notes for one image."""
    index = _index(dataset)
    mapping = index["mapping"]
    issues = []

    def add(kind, detail, obj=None, severity="error"):
        value = {"kind": kind, "detail": detail}
        if severity != "error":
            value["severity"] = severity
        if obj is not None:
            value["object_id"] = obj.get("id")
        issues.append(value)

    if image["id"] in index["conflicts"]:
        add("duplicate_conflict", "Identical image content has conflicting current annotations")
    object_ids = [obj.get("id") for obj in image.get("objects", [])]
    if len(object_ids) != len(set(object_ids)) or any(not oid for oid in object_ids):
        add("invalid_object_id", "Object identifiers must be present and unique within each image")
    for issue in image.get("import_issues", []):
        add("import_error", str(issue), severity="warning" if issue == "identity_without_evidence" else "error")
    for obj in image.get("objects", []):
        entry = mapping.get(str(obj.get("code", "")))
        if obj.get("deleted"):
            # Deletion is an operator decision, not a claim of false annotation.
            # The working record and audit history preserve it without a reason.
            continue
        if not _valid_box(obj.get("bbox"), image.get("width", 0), image.get("height", 0)):
            add("invalid_box", "Box must be finite, positive, and within image bounds", obj)
        if entry is None:
            add("unknown_code", "No dictionary definition for the current label", obj)
        elif entry.get("role") in ("ignore", "unresolved"):
            add(entry["role"], "Whole image deferred: ordinary output trainers cannot safely ignore this object", obj)
        elif entry.get("role") == "out_of_scope":
            if not entry.get("evidence") or not dataset.get("rules", {}).get("task_definition"):
                add("out_of_scope_rule", "Out-of-scope task definition or supporting evidence is not recorded", obj, "warning")
        elif entry.get("role") != "cell":
            add("unknown_role", "Unsupported dictionary role", obj)
        elif not str(entry.get("train_name") or entry.get("name") or "").strip():
            add("missing_train_name", "Training class has no name", obj)
        elif not str(entry.get("evidence", "")).strip():
            add("missing_label_evidence", "Training class definition evidence is not recorded", obj, "warning")
        if obj.get("question"):
            add("question", "Object has an unresolved question", obj, "warning")
    return issues


def make_groups(dataset):
    images = dataset.get("images", [])
    parent = {im["id"]: im["id"] for im in images}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        if a in parent and b in parent:
            a, b = find(a), find(b)
            parent[max(a, b)] = min(a, b)

    seen = {}
    for im in images:
        meta = im.get("metadata", {})
        for key in ("patient_id", "slide_id", "source_image_id", "sha256", "pixel_sha256"):
            value = (im if key.endswith("sha256") else meta).get(key)
            if value:
                token = (key, str(value))
                if token in seen:
                    union(im["id"], seen[token])
                else:
                    seen[token] = im["id"]
        source = meta.get("source_image_id")
        if source in parent:
            union(im["id"], source)
    for relation in dataset.get("relations", []):
        if relation.get("evidence") and relation.get("kind") in ("source", "derived", "crop", "augmentation", "overlap", "near_duplicate", "duplicate", "same_source"):
            union(relation.get("a"), relation.get("b"))
    roots = sorted({find(x) for x in parent})
    names = {root: f"group-{n:06d}" for n, root in enumerate(roots, 1)}
    return {key: names[find(key)] for key in parent}


def _identity_errors(images, scope):
    errors = []
    if scope not in ("patient", "slide", "component"):
        return ["Scope must be patient, slide, or component"]
    required = {"patient": "patient_id", "slide": "slide_id"}.get(scope)
    if required and any(not im.get("metadata", {}).get(required) for im in images):
        errors.append(f"{scope} scope requires complete identity for all included images")
    slides = {}
    for im in images:
        meta = im.get("metadata", {})
        if meta.get("slide_id") and meta.get("patient_id"):
            slides.setdefault(meta["slide_id"], set()).add(meta["patient_id"])
    if any(len(patients) > 1 for patients in slides.values()):
        errors.append("A slide is linked to conflicting patient identities")
    return errors


def _identity_warnings(images):
    if any(any(im.get("metadata", {}).get(key) for key in ("patient_id", "slide_id", "source_image_id"))
           and not str(im.get("metadata", {}).get("metadata_evidence", "")).strip() for im in images):
        return ["Identity/source values are supplied without recorded supporting evidence; their accuracy is the operator's responsibility"]
    return []


def plan_split(dataset, ratios, seed, scope):
    if (not isinstance(ratios, (list, tuple)) or len(ratios) != 3
            or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                   or not math.isfinite(v) or v <= 0 for v in ratios)):
        raise ValueError("Three finite positive split ratios are required")
    images = [im for im in dataset.get("images", []) if not im.get("exclude_reason")]
    errors = _identity_errors(images, scope)
    if errors:
        raise ValueError("; ".join(errors))
    groups = make_groups(dataset)
    members = {}
    for im in images:
        members.setdefault(groups[im["id"]], []).append(im["id"])
    if len(members) < 3:
        raise ValueError("At least three independent groups are required; groups cannot be split for coverage")
    total = sum(ratios)
    ratios = [float(v) / total for v in ratios]
    rng = random.Random(seed)
    ordered = list(sorted(members))
    rng.shuffle(ordered)
    ordered.sort(key=lambda g: -len(members[g]))
    assignments, used = {}, [0, 0, 0]
    # Seed all requested splits; greedily minimize the incremental squared error.
    for n, group in enumerate(ordered):
        size = len(members[group])
        empty = [i for i in range(3) if used[i] == 0]
        candidates = empty if len(ordered) - n == len(empty) else range(3)
        i = min(candidates, key=lambda j: ((used[j] + size - len(images) * ratios[j]) ** 2
                                         - (used[j] - len(images) * ratios[j]) ** 2) / ratios[j])
        used[i] += size
        assignments.update({iid: SPLITS[i] for iid in members[group]})
    return {"version": int(dataset.get("split", {}).get("version", 0)) + 1,
            "confirmed": False, "scope": scope, "seed": seed, "ratios": ratios,
            "groups": groups, "assignments": assignments, "warnings": _identity_warnings(images)}


def _asset(workspace, image):
    root = Path(workspace).resolve(strict=True)
    relative = Path(image.get("asset_path", ""))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("Unsafe asset path")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Asset escapes workspace or is not a file")
    return path


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _classes(dataset):
    names = []
    for entry in dataset.get("mapping", {}).get("entries", []):
        name = str(entry.get("train_name") or entry.get("name") or "").strip()
        if entry.get("role") == "cell" and name and name not in names:
            names.append(name)
    return [{"id": n, "name": name, "coco_id": n + 1} for n, name in enumerate(names)]


def _class_id(obj, entries, classes):
    entry = entries[str(obj["code"])]
    name = str(entry.get("train_name") or entry.get("name")).strip()
    return next(c["id"] for c in classes if c["name"] == name)


def _crop_candidate(image, obj, margin, minimum, mode):
    x1, y1, x2, y2 = obj["bbox"]
    w, h = image["width"], image["height"]
    dx, dy = (x2 - x1) * margin, (y2 - y1) * margin
    box = [max(0, math.floor(x1 - dx)), max(0, math.floor(y1 - dy)),
           min(w, math.ceil(x2 + dx)), min(h, math.ceil(y2 + dy))]
    flags, reasons = [], []
    if box[2] - box[0] < minimum or box[3] - box[1] < minimum:
        reasons.append("crop_too_small")
    if obj.get("truncated") or x1 == 0 or y1 == 0 or x2 == w or y2 == h:
        flags.append("edge_or_truncation")
    for other in _live(image):
        if other["id"] == obj["id"]:
            continue
        a, b, c, d = other["bbox"]
        intersection = max(0, min(box[2], c) - max(box[0], a)) * max(0, min(box[3], d) - max(box[1], b))
        if intersection > 0:
            flags.append("adjacent_object")
        if intersection >= .5 * (c - a) * (d - b) and intersection >= .25 * (x2 - x1) * (y2 - y1):
            flags.append("multiple_main_objects")
    flags = sorted(set(flags))
    review = obj.get("crop_review")
    if "multiple_main_objects" in flags:
        # A confirmed checkbox cannot make a visibly multi-target crop a
        # single-cell example. Fix the annotation/crop or defer classification.
        reasons.append("multiple_main_objects")
    if review == "exclude":
        reasons.append("crop_manually_excluded")
    elif mode == "strict" and review != "include":
        reasons.append("crop_not_confirmed")
    if "edge_or_truncation" in flags:
        if review != "include":
            reasons.append("edge_or_truncation")
    return box, flags, sorted(set(reasons))


def preflight(dataset, options, workspace):
    mode = options.get("mode", "basic")
    formats = options.get("formats", ["voc", "coco", "yolo"])
    classify = bool(options.get("classification"))
    report = {"ready": False, "blockers": [], "warnings": [], "included": [], "excluded": [],
              "classes": _classes(dataset), "counts": {s: {"images": 0, "objects": 0} for s in SPLITS},
              "classification": {"included": [], "excluded": []},
              "reviewed_images": 0, "total_images": len(dataset.get("images", [])),
              "scope": dataset.get("split", {}).get("scope", "component")}
    blockers = report["blockers"]
    if mode not in ("basic", "strict"):
        blockers.append("Unknown export mode")
    if not isinstance(formats, list) or any(f not in ("voc", "coco", "yolo") for f in formats):
        blockers.append("Unknown detection format")
    if not formats and not classify:
        blockers.append("Select detection formats or classification")
    try:
        margin = float(options.get("crop_margin", 0))
        minimum = int(options.get("min_crop_size", 8))
        if not math.isfinite(margin) or margin < 0 or margin > 2 or minimum < 1:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        blockers.append("Crop margin must be 0..2 and minimum size a positive integer")
        margin, minimum = 0, 8
    rules = dataset.get("rules", {})
    if rules.get("research_permission") != "verified" or not str(rules.get("permission_evidence", "")).strip():
        report["warnings"].append("Research permission or supporting evidence is not verified in this workspace; export does not grant permission to use or redistribute data")
    if "coordinate_verified" in rules and (rules.get("coordinate_verified") != "verified"
            or not str(rules.get("coordinate_evidence", "")).strip()):
        report["warnings"].append("Source coordinate interpretation has not been recorded as verified against image overlays")
    exhaustive = rules.get("annotation_policy") == "exhaustive" and bool(str(rules.get("annotation_evidence", "")).strip())
    if not exhaustive:
        report["warnings"].append("Exhaustive annotation policy or supporting evidence is not recorded; unmarked objects may be treated as background by detection trainers")
    split = dataset.get("split", {})
    if not split.get("confirmed"):
        blockers.append("Confirm the fixed group split before export")
    groups = make_groups(dataset)
    entries = _entries(dataset)
    images_by_id = {im["id"]: im for im in dataset.get("images", [])}
    if len(images_by_id) != len(dataset.get("images", [])):
        blockers.append("Duplicate image identifiers")
    if not report["classes"]:
        blockers.append("No defined training classes")
    advisory_counts = {}
    for image in dataset.get("images", []):
        iid = image["id"]
        reasons = []
        if image.get("exclude_reason"):
            reasons.append("explicitly_excluded")
        else:
            issues = image_issues(dataset, image)
            reasons.extend(sorted({x["kind"] for x in issues if x.get("severity") != "warning"}))
            for kind in {x["kind"] for x in issues if x.get("severity") == "warning"}:
                advisory_counts[kind] = advisory_counts.get(kind, 0) + 1
            if mode == "strict" and image.get("review_state") != "confirmed":
                reasons.append("image_not_confirmed")
            if not image.get("annotations_complete"):
                advisory_counts["annotation_completeness_unconfirmed"] = advisory_counts.get("annotation_completeness_unconfirmed", 0) + 1
            if split.get("assignments", {}).get(iid) not in SPLITS:
                reasons.append("missing_split_assignment")
            if split.get("groups", {}).get(iid) != groups[iid]:
                reasons.append("stale_group_assignment")
            try:
                path = _asset(workspace, image)
                if not image.get("sha256") or _sha(path) != image["sha256"]:
                    reasons.append("file_hash_mismatch")
                with Image.open(path) as decoded:
                    if decoded.size != (image.get("width"), image.get("height")):
                        reasons.append("dimension_mismatch")
                    decoded.load()
                    if image.get("pixel_sha256"):
                        rgb = decoded.convert("RGB")
                        pixel_hash = hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()
                        if pixel_hash != image["pixel_sha256"]:
                            reasons.append("pixel_hash_mismatch")
            except (OSError, ValueError, Image.DecompressionBombError):
                reasons.append("unreadable_or_unsafe_asset")
        if reasons:
            report["excluded"].append({"image_id": iid, "reasons": sorted(set(reasons))})
            continue
        report["included"].append(iid)
        if image.get("review_state") == "confirmed":
            report["reviewed_images"] += 1
        s = split["assignments"][iid]
        targets = [o for o in _live(image) if entries[str(o["code"])]["role"] == "cell"]
        report["counts"][s]["images"] += 1
        report["counts"][s]["objects"] += len(targets)
        if classify:
            for obj in targets:
                box, flags, why = _crop_candidate(image, obj, margin, minimum, mode)
                if why:
                    report["classification"]["excluded"].append({"image_id": iid, "object_id": obj["id"], "reasons": why})
                else:
                    report["classification"]["included"].append({"image_id": iid, "object_id": obj["id"],
                        "bbox": box, "split": s, "class_id": _class_id(obj, entries, report["classes"]), "flags": flags})
    included_images = [images_by_id[i] for i in report["included"]]
    blockers.extend(_identity_errors(included_images, report["scope"]))
    report["warnings"].extend(_identity_warnings(included_images))
    advisory_text = {
        "missing_label_evidence": "training class definition evidence is not recorded",
        "out_of_scope_rule": "out-of-scope task definition or supporting evidence is not recorded",
        "question": "objects retain question marks; review notes remain in the local workspace",
        "import_error": "identity/source evidence was not recorded at import",
        "annotation_completeness_unconfirmed": "annotation completeness is not separately asserted; completing review does not assert exhaustive annotation",
    }
    for kind, count in sorted(advisory_counts.items()):
        report["warnings"].append(f"{count} images: {advisory_text[kind]} (advisory)")
    seen = {}
    for iid in report["included"]:
        group, s = groups[iid], split["assignments"][iid]
        if group in seen and seen[group] != s:
            blockers.append("A connected identity/source/duplicate group crosses splits")
            break
        seen[group] = s
    for s in SPLITS:
        if not report["counts"][s]["images"]:
            blockers.append(f"No eligible images remain in {s}")
        if classify and not any(c["split"] == s for c in report["classification"]["included"]):
            blockers.append(f"No eligible classification crops remain in {s}")
    if not sum(c["objects"] for c in report["counts"].values()):
        blockers.append("No trainable target objects remain")
    if report["scope"] == "component":
        report["warnings"].append("Patient independence is unknown; component grouping supports exploratory claims only")
    if mode == "basic":
        report["warnings"].append("Basic export is exploratory; automatic checks do not establish annotation correctness")
    report["warnings"].append("Exact duplicate checks do not rule out unrecorded overlapping fields or near duplicates")
    for cls in report["classes"]:
        coverage = {groups[im["id"]] for im in included_images for o in _live(im)
                    if entries[str(o["code"])]["role"] == "cell" and _class_id(o, entries, report["classes"]) == cls["id"]}
        if len(coverage) < 2:
            report["warnings"].append(f"Class {cls['id']} has {len(coverage)} independent groups: cross-group performance cannot be reliably estimated")
    if any(im.get("metadata", {}).get("historical_exposure") or rules.get("historical_exposure") for im in included_images):
        report["warnings"].append("Historical experimental exposure is recorded; renaming a split test does not establish unseen evaluation")
    report["blockers"] = list(dict.fromkeys(blockers))
    report["ready"] = not report["blockers"]
    return report


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            # Spreadsheet programs can execute formula-looking free text.
            writer.writerow({key: ("'" + value if isinstance(value, str) and value[:1] in "=+-@" else value)
                             for key, value in row.items()})


def build_export(dataset, options, workspace, destination, progress=None):
    destination = Path(destination).absolute()
    workspace = Path(workspace).resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Export destination must be a new directory")
    # Exports may live in the workspace export area, never below image assets.
    resolved_dest = destination.resolve()
    for image in dataset.get("images", []):
        try:
            source = _asset(workspace, image)
        except (ValueError, OSError):
            continue
        if resolved_dest == source or source.is_relative_to(resolved_dest):
            raise ValueError("Export cannot contain source assets")
    report = preflight(dataset, options, workspace)
    if not report["ready"]:
        raise ValueError("Export blocked: " + "; ".join(report["blockers"]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".workbench-export-", dir=destination.parent))
    try:
        result = _write_export(dataset, options, workspace, staging, report, progress)
        if destination.exists():
            raise ValueError("Export destination appeared during build")
        staging.rename(destination)
        return {"path": str(destination), "report": report, "files": result}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _write_export(dataset, options, workspace, root, report, progress):
    formats = options.get("formats", ["voc", "coco", "yolo"])
    classes, entries = report["classes"], _entries(dataset)
    images = {im["id"]: im for im in dataset["images"]}
    # Never expose input IDs, identity strings, source hashes, or local paths.
    ids = {iid: f"image-{n:06d}" for n, iid in enumerate(sorted(images), 1)}
    object_ids = {iid: {o["id"]: f"object-{n:06d}" for n, o in enumerate(im["objects"], 1)} for iid, im in images.items()}
    groups = make_groups(dataset)
    splits = dataset["split"]["assignments"]
    coco = {s: {"images": [], "annotations": [], "categories": [{"id": c["coco_id"], "name": c["name"]} for c in classes]} for s in SPLITS}
    voc_lists = {s: [] for s in SPLITS}
    manifest_images, crop_rows = [], []
    crops_by_image = {}
    for crop in report["classification"]["included"]:
        crops_by_image.setdefault(crop["image_id"], []).append(crop)
    annotation_id = 1
    for index, iid in enumerate(report["included"], 1):
        im = images[iid]
        safe_id, s = ids[iid], splits[iid]
        filename = safe_id + ".png"
        voc_filename = safe_id + ".jpg"
        source = _asset(workspace, im)
        # Re-encode losslessly: preserves decoded RGB pixels and strips metadata.
        with Image.open(source) as decoded:
            decoded.load()
            rgb = decoded.convert("RGB")
            encoded = io.BytesIO()
            rgb.save(encoded, format="PNG")
        data = encoded.getvalue()
        jpeg_data = None
        if "voc" in formats:
            jpeg_encoded = io.BytesIO()
            rgb.save(jpeg_encoded, format="JPEG", quality=100, subsampling=0)
            jpeg_data = jpeg_encoded.getvalue()
        # Verify original after decode too, to catch source replacement mid-build.
        if _sha(source) != im["sha256"]:
            raise ValueError("Source asset changed during build")
        targets = [o for o in _live(im) if entries[str(o["code"])]["role"] == "cell"]
        for fmt in formats:
            out = (root / "voc" / "JPEGImages" / voc_filename if fmt == "voc"
                   else root / fmt / "images" / s / filename)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(jpeg_data if fmt == "voc" else data)
        if "voc" in formats:
            xml = ET.Element("annotation")
            ET.SubElement(xml, "filename").text = voc_filename
            size = ET.SubElement(xml, "size")
            for name, value in (("width", im["width"]), ("height", im["height"]), ("depth", 3)):
                ET.SubElement(size, name).text = str(value)
            for obj in targets:
                node = ET.SubElement(xml, "object")
                ET.SubElement(node, "name").text = classes[_class_id(obj, entries, classes)]["name"]
                ET.SubElement(node, "truncated").text = str(int(bool(obj.get("truncated"))))
                ET.SubElement(node, "difficult").text = str(int(bool(obj.get("difficult"))))
                b = obj["bbox"]
                box = ET.SubElement(node, "bndbox")
                for name, value in zip(("xmin", "ymin", "xmax", "ymax"), (math.floor(b[0]) + 1, math.floor(b[1]) + 1, math.ceil(b[2]), math.ceil(b[3]))):
                    ET.SubElement(box, name).text = str(value)
            out = root / "voc" / "Annotations" / (safe_id + ".xml")
            out.parent.mkdir(parents=True, exist_ok=True)
            ET.ElementTree(xml).write(out, encoding="utf-8", xml_declaration=True)
            voc_lists[s].append(safe_id)
        if "coco" in formats:
            image_id = index
            coco[s]["images"].append({"id": image_id, "file_name": filename, "width": im["width"], "height": im["height"]})
            for obj in targets:
                a, b, c, d = obj["bbox"]
                coco[s]["annotations"].append({"id": annotation_id, "image_id": image_id,
                    "category_id": _class_id(obj, entries, classes) + 1, "bbox": [a, b, c - a, d - b],
                    "area": (c - a) * (d - b), "iscrowd": 0})
                annotation_id += 1
        if "yolo" in formats:
            rows = []
            for obj in targets:
                a, b, c, d = obj["bbox"]
                rows.append(f"{_class_id(obj, entries, classes)} {(a+c)/2/im['width']:.12g} {(b+d)/2/im['height']:.12g} {(c-a)/im['width']:.12g} {(d-b)/im['height']:.12g}")
            label = root / "yolo" / "labels" / s / (safe_id + ".txt")
            label.parent.mkdir(parents=True, exist_ok=True)
            label.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
        for crop in crops_by_image.get(iid, []):
            oid = object_ids[iid][crop["object_id"]]
            relative = Path("classification") / s / str(crop["class_id"]) / f"{safe_id}-{oid}.png"
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            rgb.crop(crop["bbox"]).save(root / relative, format="PNG")
            obj = next(o for o in im["objects"] if o["id"] == crop["object_id"])
            crop_rows.append({"file": relative.as_posix(), "parent_image_id": safe_id, "object_id": oid,
                "class_id": crop["class_id"], "split": s, "original_bbox": json.dumps(obj["bbox"]),
                "crop_bbox": json.dumps(crop["bbox"]), "flags": ";".join(crop["flags"]),
                "decision": obj.get("crop_review") or "basic_geometry", "parent_revision": dataset.get("revision")})
        manifest_images.append({"image_id": safe_id, "group_id": groups[iid], "split": s,
                                "width": im["width"], "height": im["height"], "objects": len(targets)})
        if progress:
            progress(index / len(report["included"]), f"Exported {index}/{len(report['included'])} images")
    for s in SPLITS:
        if "voc" in formats:
            out = root / "voc" / "ImageSets" / "Main" / (s + ".txt")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(voc_lists[s]) + "\n", encoding="utf-8")
        if "coco" in formats:
            _json(root / "coco" / "annotations" / ("instances_" + s + ".json"), coco[s])
    if "yolo" in formats:
        # JSON string quoting is valid YAML, including punctuation/newlines.
        (root / "yolo" / "data.yaml").write_text("train: images/train\nval: images/val\ntest: images/test\nnames:\n" + "".join(f"  {c['id']}: {json.dumps(c['name'], ensure_ascii=False)}\n" for c in classes), encoding="utf-8")
    _json(root / "classes.json", classes)
    _csv(root / "classes.csv", ["id", "name", "coco_id"], classes)
    _csv(root / "split.csv", ["image_id", "group_id", "split", "width", "height", "objects"], manifest_images)
    _csv(root / "excluded-images.csv", ["image_id", "reasons"], [{"image_id": ids[x["image_id"]], "reasons": ";".join(x["reasons"])} for x in report["excluded"]])
    _csv(root / "excluded-objects.csv", ["image_id", "object_id", "reasons"], [{"image_id": ids[x["image_id"]], "object_id": object_ids[x["image_id"]][x["object_id"]], "reasons": ";".join(x["reasons"])} for x in report["classification"]["excluded"]])
    _csv(root / "crops.csv", ["file", "parent_image_id", "object_id", "class_id", "split", "original_bbox", "crop_bbox", "flags", "decision", "parent_revision"], crop_rows)
    coverage = []
    for cls in classes:
        for s in SPLITS:
            objects = [(iid, obj) for iid in report["included"] if splits[iid] == s for obj in _live(images[iid])
                       if entries[str(obj["code"])]["role"] == "cell" and _class_id(obj, entries, classes) == cls["id"]]
            coverage.append({"class_id": cls["id"], "split": s, "objects": len(objects),
                             "images": len({i for i, _ in objects}), "groups": len({groups[i] for i, _ in objects})})
    _csv(root / "group-coverage.csv", ["class_id", "split", "objects", "images", "groups"], coverage)
    safe_rules = {"research_permission": dataset.get("rules", {}).get("research_permission", "pending"),
                  "permission_evidence_recorded_locally": bool(str(dataset.get("rules", {}).get("permission_evidence", "")).strip()),
                  "coordinate_verified": dataset.get("rules", {}).get("coordinate_verified", "legacy_unspecified"),
                  "coordinate_evidence_recorded_locally": bool(str(dataset.get("rules", {}).get("coordinate_evidence", "")).strip()),
                  "annotation_policy": dataset.get("rules", {}).get("annotation_policy"),
                  "annotation_evidence_recorded_locally": bool(dataset.get("rules", {}).get("annotation_evidence")),
                  "task_definition_recorded_locally": bool(dataset.get("rules", {}).get("task_definition")),
                  "historical_exposure_recorded_locally": bool(dataset.get("rules", {}).get("historical_exposure")),
                  "crop_margin": options.get("crop_margin", 0), "min_crop_size": options.get("min_crop_size", 8),
                  "crop_multiple_main_rule": "other visible area >=50% of other box and >=25% of target box area",
                  "crop_multiple_main_override": "not permitted; annotation or crop must be corrected",
                  "crop_edge_rule": "touching image edge or truncated requires explicit include; note is optional",
                  "unsupported_ignore_rule": "defer whole detection image",
                  "coordinate_mode": "zero-based half-open; VOC floor(min)+1, ceil(max); crops floor(min), ceil(max)"}
    _json(root / "rules.json", safe_rules)
    _json(root / "review-summary.json", {"mode": options.get("mode", "basic"), "included_images": len(report["included"]),
          "excluded_images": len(report["excluded"]), "reviewed_included_images": report["reviewed_images"],
          "review_coverage": report["reviewed_images"] / len(report["included"]), "counts": report["counts"],
          "completeness_asserted_included_images": sum(bool(images[i].get("annotations_complete")) for i in report["included"]),
          "review_meaning": "Operator marked review complete; completeness and clinical correctness are not inferred",
          "classification_crops": len(crop_rows), "classification_excluded": len(report["classification"]["excluded"]),
          "warnings": report["warnings"]})
    _json(root / "manifest.json", {"tool": "marrow-dataset-workbench", "schema_version": 1,
          "dataset_version": "snapshot-" + hashlib.sha256((str(dataset.get("id", "")) + ":" + str(dataset.get("revision"))).encode()).hexdigest()[:20],
          "revision": dataset.get("revision"), "mapping_version": dataset.get("mapping", {}).get("version"),
          "split_version": dataset["split"].get("version"), "rule_version": dataset.get("rules", {}).get("version", dataset.get("revision")),
          "created_at": datetime.now(timezone.utc).isoformat(), "mode": options.get("mode", "basic"),
          "formats": formats, "scope": report["scope"], "images": manifest_images,
          "split_seed": dataset["split"].get("seed"), "split_ratios": dataset["split"].get("ratios"),
          "input_snapshot_digest": hashlib.sha256(json.dumps([(ids[i], images[i].get("sha256")) for i in sorted(images)], sort_keys=True).encode()).hexdigest(),
          "image_encoding": {"voc": "metadata-stripped RGB JPEG quality=100 subsampling=0; lossy transcoding, no resizing",
                             "coco": "metadata-stripped PNG from decoded RGB; lossless, no resizing",
                             "yolo": "metadata-stripped PNG from decoded RGB; lossless, no resizing",
                             "classification": "metadata-stripped PNG from decoded RGB crops; lossless, no resizing"}, "rules": safe_rules,
          "private_provenance": "Input identity, source paths, hashes and evidence remain in the local workspace snapshot"})
    (root / "dataset-card.md").write_text("# Training dataset export\n\n"
        f"Mode: {options.get('mode', 'basic')}. Grouping scope: {report['scope']}.\n\n"
        "This package is for the fixed research task recorded in the local source snapshot. "
        "See classes.json, rules.json, split.csv, group-coverage.csv and review-summary.json. "
        "Actual permission status and evidence-presence flags are in rules.json; any supporting documents and private provenance remain local. "
        "This is not an authorization to redistribute. Pixel content requires separate privacy review; "
        "metadata removal and generated IDs do not establish image de-identification. "
        "Basic exports are exploratory. Review completion records the operator's decision; it does not assert exhaustive annotation or clinical ground truth. "
        "Do not treat previously exposed samples as unseen merely by changing their split names.\n\n"
        "VOC uses 1-based inclusive integer boxes; COCO uses 0-based xywh; YOLO uses "
        "0-based class IDs and normalized center/size. Fractional VOC boxes are conservatively expanded. "
        "VOC JPEGImages contains standard .jpg files for conventional VOC loaders, including "
        "torchvision VOCDetection; JPEG is lossy even at quality 100 and subsampling 0. "
        "COCO, YOLO and classification use lossless decoded-RGB PNG instead. "
        "Images are not resized. load_examples.py provides standalone readers and validation. "
        "For YOLO, point the loader to yolo/data.yaml and resolve images relative to that directory. "
        "Classification folders contain original-resolution crops; no resizing is applied.\n", encoding="utf-8")
    (root / "load_examples.py").write_text(_LOAD_EXAMPLES, encoding="utf-8")
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    _json(root / "checksums.json", {name: _sha(root / name) for name in files})
    return files + ["checksums.json"]


_LOAD_EXAMPLES = '''"""Standalone training-package readers. Requires Pillow, no workbench install.

All readers yield (image_path, target), with boxes in 0-based half-open xyxy.
Use these records in your framework Dataset; choose resize/augmentation in training.
Classification can also use torchvision.datasets.ImageFolder(root / "classification" / split).
ImageFolder remaps folder names lexicographically: inspect class_to_idx and use
classes.json to retain original numeric IDs, especially with 10 or more classes.
VOCDetection users can arrange voc/ as VOCdevkit/VOC2007, set year="2007",
image_set="train"/"val"/"test", download=False. VOC2012 supports train/val
in common torchvision versions; use the supplied VOC reader for custom splits.
"""
import argparse
import csv
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from PIL import Image


def load_voc(root, split):
    root = Path(root)
    classes = json.loads((root / "classes.json").read_text(encoding="utf-8"))
    ids = {c["name"]: c["id"] for c in classes}
    voc = root / "voc"
    for key in (voc / "ImageSets" / "Main" / (split + ".txt")).read_text().split():
        xml = ET.parse(voc / "Annotations" / (key + ".xml"))
        boxes, labels = [], []
        for obj in xml.findall("object"):
            box = obj.find("bndbox")
            a, b, c, d = [float(box.findtext(k)) for k in ("xmin", "ymin", "xmax", "ymax")]
            boxes.append([a - 1, b - 1, c, d])
            labels.append(ids[obj.findtext("name")])
        yield voc / "JPEGImages" / (key + ".jpg"), {"boxes": boxes, "labels": labels}


def load_coco(root, split):
    root = Path(root)
    data = json.loads((root / "coco" / "annotations" / ("instances_" + split + ".json")).read_text(encoding="utf-8"))
    annotations = {}
    for item in data["annotations"]:
        annotations.setdefault(item["image_id"], []).append(item)
    for image in data["images"]:
        boxes, labels = [], []
        for item in annotations.get(image["id"], []):
            x, y, w, h = item["bbox"]
            boxes.append([x, y, x + w, y + h])
            labels.append(item["category_id"] - 1)
        yield root / "coco" / "images" / split / image["file_name"], {"boxes": boxes, "labels": labels}


def load_yolo(root, split):
    root = Path(root) / "yolo"
    for image in sorted((root / "images" / split).glob("*.png")):
        with Image.open(image) as decoded:
            w, h = decoded.size
        boxes, labels = [], []
        for line in (root / "labels" / split / (image.stem + ".txt")).read_text().splitlines():
            cls, x, y, bw, bh = map(float, line.split())
            labels.append(int(cls))
            boxes.append([(x - bw / 2) * w, (y - bh / 2) * h, (x + bw / 2) * w, (y + bh / 2) * h])
        yield image, {"boxes": boxes, "labels": labels}


def load_classification(root, split):
    root = Path(root)
    with (root / "crops.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["split"] == split:
                yield root / row["file"], {"label": int(row["class_id"]), "parent_image_id": row["parent_image_id"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Decode images and validate a training export split")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--format", choices=["voc", "coco", "yolo", "classification"], required=True)
    parser.add_argument("--split", choices=["train", "val", "test"], default="train")
    args = parser.parse_args()
    images, objects = 0, 0
    for path, target in globals()["load_" + args.format](args.root, args.split):
        with Image.open(path) as image:
            image.load()
            w, h = image.size
        for a, b, c, d in target.get("boxes", []):
            assert -1e-7 <= a < c <= w + 1e-7 and -1e-7 <= b < d <= h + 1e-7, (path, target)
        images += 1
        objects += len(target["boxes"]) if "boxes" in target else 1
    print(json.dumps({"format": args.format, "split": args.split, "images": images, "objects": objects}))
'''
