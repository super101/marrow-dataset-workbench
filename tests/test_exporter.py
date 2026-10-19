"""Synthetic fixtures only. Exercise format geometry, grouping and failure gates."""
import hashlib
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from PIL import Image

from workbench.exporter import build_export, image_issues, make_groups, plan_split, preflight


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ds = {"id": "synthetic", "revision": 1, "mapping": {"version": 1, "entries": [
            {"code": "a", "name": "Synthetic cell", "train_name": "Synthetic cell", "role": "cell", "evidence": "synthetic geometry"},
            {"code": "?", "name": "Unknown", "role": "unresolved"},
            {"code": "x", "name": "Ignore", "role": "ignore"}]},
            "rules": {"research_permission": "verified", "permission_evidence": "synthetic license",
                      "annotation_policy": "exhaustive", "annotation_evidence": "synthetic shapes"},
            "images": [], "relations": []}
        for n in range(6):
            path = self.root / f"synthetic-{n}.png"
            Image.new("RGB", (40, 30), (30 + n * 20, 50, 70)).save(path)
            self.ds["images"].append({"id": f"i{n}", "asset_path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "width": 40, "height": 30,
                "objects": [{"id": "o1", "raw_code": "a", "code": "a", "bbox": [3, 4, 13, 16],
                             "crop_review": "include"}], "review_state": "confirmed", "annotations_complete": True,
                "metadata": {"patient_id": f"private-patient-{n}", "slide_id": f"slide-{n}", "metadata_evidence": "synthetic"}})
        self.ds["split"] = plan_split(self.ds, [.6, .2, .2], 42, "patient")
        self.ds["split"]["confirmed"] = True
        self.options = {"mode": "strict", "formats": ["voc", "coco", "yolo"], "classification": True}

    def tearDown(self):
        self.temp.cleanup()

    def revise(self):
        self.ds["revision"] += 1

    def test_all_formats_round_trip_and_crops(self):
        before = {p.name: p.read_bytes() for p in self.root.glob("*.png")}
        output = self.root / "export"
        result = build_export(self.ds, self.options, self.root, output)
        self.assertTrue(result["report"]["ready"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob("*.png")})
        xmls = list((output / "voc" / "Annotations").glob("*.xml"))
        self.assertEqual(len(xmls), 6)
        for path in xmls:
            tree = ET.parse(path)
            self.assertEqual(Path(tree.findtext("filename")).suffix, ".jpg")
            b = tree.find(".//bndbox")
            self.assertEqual([int(b.find(k).text) for k in ("xmin", "ymin", "xmax", "ymax")], [4, 5, 13, 16])
            with Image.open(output / "voc" / "JPEGImages" / tree.findtext("filename")) as img:
                self.assertEqual(img.size, (40, 30))
        for path in (output / "coco" / "annotations").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(data["images"])
            for a in data["annotations"]:
                self.assertEqual(a["bbox"], [3, 4, 10, 12])
                self.assertEqual(a["category_id"], 1)
                self.assertEqual(a["area"], 120)
        for path in (output / "yolo" / "labels").rglob("*.txt"):
            vals = [float(x) for x in path.read_text().split()]
            for actual, expected in zip(vals, [0, .2, 1 / 3, .25, .4]):
                self.assertAlmostEqual(actual, expected, places=10)
        crops = list((output / "classification").rglob("*.png"))
        self.assertEqual(len(crops), 6)
        for path in crops:
            with Image.open(path) as img:
                self.assertEqual(img.size, (10, 12))
        for path in output.rglob("*"):
            if path.is_file() and path.suffix not in {".png", ".jpg"}:
                self.assertNotIn("private-patient", path.read_text(encoding="utf-8"))
                self.assertNotIn(str(self.root), path.read_text(encoding="utf-8"))
        checksums = json.loads((output / "checksums.json").read_text())
        for name, sha in checksums.items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), sha)
        spec = importlib.util.spec_from_file_location("export_load_examples", output / "load_examples.py")
        examples = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(examples)
        for split in ("train", "val", "test"):
            voc = list(examples.load_voc(output, split))
            coco = list(examples.load_coco(output, split))
            yolo = list(examples.load_yolo(output, split))
            classification = list(examples.load_classification(output, split))
            self.assertEqual(len(voc), len(coco))
            self.assertEqual(len(voc), len(yolo))
            self.assertEqual(len(voc), len(classification))
            for records in (voc, coco, yolo, classification):
                for image_path, target in records:
                    self.assertTrue(image_path.is_file())
                    with Image.open(image_path) as decoded:
                        decoded.load()
            for (_, v), (_, c), (_, y) in zip(voc, coco, yolo):
                self.assertEqual(v, c)
                self.assertEqual(v["labels"], y["labels"])
                for actual, expected in zip(y["boxes"][0], v["boxes"][0]):
                    self.assertAlmostEqual(actual, expected, places=8)
        with self.assertRaises(ValueError):
            build_export(self.ds, self.options, self.root, output)

    def test_groups_transitive_closure_source_reference_not_batch(self):
        images = self.ds["images"]
        images[1]["metadata"]["patient_id"] = images[0]["metadata"]["patient_id"]
        images[2]["metadata"]["source_image_id"] = "i1"
        self.ds["relations"] = [{"a": "i2", "b": "i3", "kind": "overlap", "evidence": "manual"},
                                {"a": "i3", "b": "i4", "kind": "near_duplicate", "evidence": ""}]
        for im in images:
            im["metadata"]["acquisition_batch_id"] = "same-batch"
        groups = make_groups(self.ds)
        self.assertEqual(len({groups[f"i{i}"] for i in range(4)}), 1)
        self.assertNotEqual(groups["i3"], groups["i4"])
        split = plan_split(self.ds, [6, 2, 2], 123, "patient")
        self.assertEqual(split, plan_split(self.ds, [6, 2, 2], 123, "patient"))
        self.assertEqual(len({split["assignments"][f"i{i}"] for i in range(4)}), 1)
        self.assertEqual(set(split["assignments"].values()), {"train", "val", "test"})

    def test_unknown_ignore_deletion_without_reason_and_explicit_exclusion(self):
        im = self.ds["images"][0]
        im["objects"][0]["code"] = "?"
        self.revise()
        self.assertIn("unresolved", {x["kind"] for x in image_issues(self.ds, im)})
        im["objects"][0].update(deleted=True, delete_reason="")
        self.revise()
        self.assertFalse(image_issues(self.ds, im))
        report = preflight(self.ds, {**self.options, "classification": False}, self.root)
        self.assertTrue(report["ready"])
        self.assertIn(im["id"], report["included"])
        im["objects"][0]["delete_reason"] = "optional operator note without a special prefix"
        self.revise()
        self.assertFalse(image_issues(self.ds, im))
        im["objects"][0].update(deleted=False, code="x")
        self.revise()
        report = preflight(self.ds, self.options, self.root)
        self.assertNotIn(im["id"], report["included"])
        im["exclude_reason"] = "defer unresolved"
        self.revise()
        self.assertIn("explicitly_excluded", preflight(self.ds, self.options, self.root)["excluded"][0]["reasons"])

    def test_duplicate_conflicts_dynamic_and_excluded_counterpart(self):
        images = self.ds["images"]
        images[1]["sha256"] = images[0]["sha256"]
        images[1]["objects"][0]["bbox"] = [4, 4, 13, 16]
        self.revise()
        self.assertIn("duplicate_conflict", {x["kind"] for x in image_issues(self.ds, images[0])})
        images[1]["objects"][0]["bbox"] = [3, 4, 13, 16]
        self.revise()
        self.assertFalse(image_issues(self.ds, images[0]))
        images[1]["objects"][0]["bbox"] = None
        self.revise()
        self.assertIn("invalid_box", {x["kind"] for x in image_issues(self.ds, images[1])})
        images[1]["exclude_reason"] = "duplicate alternative"
        self.revise()
        self.assertFalse(image_issues(self.ds, images[0]))

    def test_review_selection_optional_completeness_and_crop_risks(self):
        im = self.ds["images"][0]
        im.update(review_state="pending", annotations_complete=False)
        im["objects"][0].pop("crop_review")
        self.revise()
        basic = preflight(self.ds, {**self.options, "mode": "basic"}, self.root)
        self.assertIn("i0", basic["included"])
        strict = preflight(self.ds, self.options, self.root)
        self.assertNotIn("i0", strict["included"])
        self.ds["rules"]["annotation_policy"] = "unknown"
        self.assertIn("i0", preflight(self.ds, {**self.options, "mode": "basic"}, self.root)["included"])
        im.update(review_state="confirmed", annotations_complete=False)
        self.assertIn("i0", preflight(self.ds, self.options, self.root)["included"])
        im["objects"].append({"id": "o2", "code": "a", "bbox": [4, 5, 12, 15]})
        self.revise()
        report = preflight(self.ds, {**self.options, "mode": "basic"}, self.root)
        self.assertIn("i0", report["included"])
        self.assertTrue(any("multiple_main_objects" in x["reasons"] for x in report["classification"]["excluded"]))
        im["objects"][0].update(crop_review="include", crop_note="checkbox cannot change geometry")
        report = preflight(self.ds, self.options, self.root)
        self.assertTrue(any(x["object_id"] == "o1" and "multiple_main_objects" in x["reasons"]
                            for x in report["classification"]["excluded"]))

    def test_hash_path_and_group_gates_with_permission_advisory(self):
        im = self.ds["images"][0]
        im["asset_path"] = "../escape.png"
        report = preflight(self.ds, self.options, self.root)
        self.assertIn("unreadable_or_unsafe_asset", report["excluded"][0]["reasons"])
        im["asset_path"] = "synthetic-0.png"
        (self.root / im["asset_path"]).write_bytes(b"changed")
        self.assertNotIn("i0", preflight(self.ds, self.options, self.root)["included"])
        before_permission_change = preflight(self.ds, self.options, self.root)
        self.ds["rules"]["research_permission"] = "pending"
        after_permission_change = preflight(self.ds, self.options, self.root)
        self.assertEqual(before_permission_change["ready"], after_permission_change["ready"])
        self.assertTrue(any("Research permission" in w for w in after_permission_change["warnings"]))
        self.ds["images"][1]["metadata"]["patient_id"] = self.ds["images"][2]["metadata"]["patient_id"]
        self.revise()
        report = preflight(self.ds, self.options, self.root)
        self.assertTrue(any("stale_group_assignment" in e["reasons"] for e in report["excluded"]))

    def test_atomic_failure_removes_staging_and_no_destination(self):
        output = self.root / "atomic"
        def fail(*args):
            raise RuntimeError("synthetic failure")
        with self.assertRaises(RuntimeError):
            build_export(self.ds, self.options, self.root, output, fail)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.glob(".workbench-export-*")))

    def test_identity_and_insufficient_group_failure(self):
        self.ds["images"][0]["metadata"]["metadata_evidence"] = ""
        split = plan_split(self.ds, [1, 1, 1], 0, "patient")
        self.assertTrue(split["warnings"])
        self.ds["images"][0]["metadata"]["patient_id"] = ""
        with self.assertRaises(ValueError):
            plan_split(self.ds, [1, 1, 1], 0, "patient")
        for im in self.ds["images"]:
            im["metadata"]["patient_id"] = "one-patient"
        with self.assertRaises(ValueError):
            plan_split(self.ds, [1, 1, 1], 0, "component")

    def test_pixel_hash_and_cross_split_group_check(self):
        im = self.ds["images"][0]
        im["pixel_sha256"] = "incorrect"
        self.assertIn("pixel_hash_mismatch", preflight(self.ds, self.options, self.root)["excluded"][0]["reasons"])
        im.pop("pixel_sha256")
        self.ds["images"][1]["metadata"]["source_image_id"] = "i0"
        self.revise()
        self.ds["split"]["groups"] = make_groups(self.ds)
        self.ds["split"]["assignments"].update(i0="train", i1="val")
        self.assertTrue(any("crosses splits" in b for b in preflight(self.ds, self.options, self.root)["blockers"]))

    def test_direct_review_without_operator_evidence_exports_truthful_status(self):
        self.ds["mapping"]["entries"][0]["evidence"] = ""
        self.ds["mapping"]["entries"].append({"code": "outside", "name": "Task-excluded object", "role": "out_of_scope"})
        self.ds["rules"] = {"research_permission": "pending", "permission_evidence": "",
                            "annotation_policy": "unknown", "annotation_evidence": "",
                            "coordinate_verified": "pending", "coordinate_evidence": ""}
        for image in self.ds["images"]:
            image["annotations_complete"] = False
            image["metadata"]["metadata_evidence"] = ""
            image["import_issues"] = ["identity_without_evidence"]
        image = self.ds["images"][0]
        image["objects"][0]["question"] = "private-review-comment"
        image["objects"].append({"id": "outside", "code": "outside", "bbox": [20, 5, 25, 10]})
        self.revise()
        report = preflight(self.ds, self.options, self.root)
        self.assertTrue(report["ready"], report["blockers"])
        self.assertEqual(len(report["included"]), 6)
        self.assertFalse(report["excluded"])
        issues = image_issues(self.ds, image)
        self.assertTrue({"question", "missing_label_evidence", "out_of_scope_rule", "import_error"}.issubset({i["kind"] for i in issues}))
        self.assertTrue(all(i.get("severity") == "warning" for i in issues))
        self.assertTrue(any("question marks" in w for w in report["warnings"]))
        result = build_export(self.ds, self.options, self.root, self.root / "optional-evidence")
        output = Path(result["path"])
        rules = json.loads((output / "rules.json").read_text())
        self.assertEqual(rules["research_permission"], "pending")
        self.assertFalse(rules["permission_evidence_recorded_locally"])
        self.assertEqual(rules["coordinate_verified"], "pending")
        self.assertFalse(rules["coordinate_evidence_recorded_locally"])
        summary = json.loads((output / "review-summary.json").read_text())
        self.assertEqual(summary["reviewed_included_images"], 6)
        self.assertEqual(summary["completeness_asserted_included_images"], 0)
        self.assertNotIn("private-review-comment", (output / "review-summary.json").read_text())

    def test_edge_crop_explicit_include_needs_no_reason(self):
        image = self.ds["images"][0]
        image["objects"][0].update(bbox=[0, 4, 13, 16], crop_review="include", crop_note="")
        self.revise()
        report = preflight(self.ds, self.options, self.root)
        self.assertTrue(report["ready"])
        crop = next(c for c in report["classification"]["included"] if c["image_id"] == image["id"])
        self.assertIn("edge_or_truncation", crop["flags"])
        image["objects"][0]["crop_review"] = "pending"
        self.revise()
        report = preflight(self.ds, self.options, self.root)
        excluded = next(c for c in report["classification"]["excluded"] if c["image_id"] == image["id"])
        self.assertIn("edge_or_truncation", excluded["reasons"])

    def test_component_scope_still_rejects_known_identity_conflict(self):
        self.ds["images"][1]["metadata"]["slide_id"] = self.ds["images"][0]["metadata"]["slide_id"]
        with self.assertRaisesRegex(ValueError, "conflicting patient"):
            plan_split(self.ds, [1, 1, 1], 0, "component")

    def test_coordinate_evidence_is_advisory_when_declared(self):
        self.assertTrue(preflight(self.ds, self.options, self.root)["ready"])
        rules = self.ds["rules"]
        rules.update(coordinate_verified="pending", coordinate_evidence="")
        report = preflight(self.ds, self.options, self.root)
        self.assertTrue(report["ready"])
        self.assertTrue(any("Source coordinate interpretation" in w for w in report["warnings"]))
        rules["coordinate_verified"] = "verified"
        self.assertTrue(preflight(self.ds, self.options, self.root)["ready"])
        rules["coordinate_evidence"] = "Synthetic border box checked against original image overlay"
        report = preflight(self.ds, self.options, self.root)
        self.assertTrue(report["ready"])
        self.assertFalse(any("Source coordinate interpretation" in w for w in report["warnings"]))

    def test_one_pixel_and_fractional_coordinate_semantics(self):
        for im in self.ds["images"]:
            im["objects"][0]["bbox"] = [0, 0, 1, 1]
        self.revise()
        options = {**self.options, "classification": False}
        result = build_export(self.ds, options, self.root, self.root / "one-pixel")
        for path in (Path(result["path"]) / "voc" / "Annotations").glob("*.xml"):
            box = ET.parse(path).find(".//bndbox")
            self.assertEqual([int(box.find(k).text) for k in ("xmin", "ymin", "xmax", "ymax")], [1, 1, 1, 1])


if __name__ == "__main__":
    unittest.main()
