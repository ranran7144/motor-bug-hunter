"""Offline contracts for review isolation, prompts, metadata and failure handling."""

from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import oracle
import requirement_review as review


class Response:
    def __init__(self, body):
        self.body, self.headers, self.status = body, Message(), 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size):
        return json.dumps(self.body).encode("utf-8")[:size]


GOOD = {"ambiguity": "clear", "testability": "testable", "atomicity": "atomic",
        "subject": "specified", "trigger": "specified", "completion": "specified",
        "quantitative": "sufficient", "safety": "safety_related"}


def classifier(request, timeout):
    body = json.loads(request.data)
    labels = body["labels"]
    # These labels are a transport fixture, never expectations about the sample.
    label = next((value for value in GOOD.values() if value in labels), labels[0])
    scores = {value: 0.8 if value == label else 0.2 / (len(labels) - 1) for value in labels}
    return Response({"model": "jev-contract-fixture", "results": [
        {"label": label, "confidence": 0.73, "scores": scores} for _ in body["inputs"]]})


def sample(**extra):
    return {"requirement_id": "LOCAL-ONLY-001", "text": review.SAMPLE_REQUIREMENT, **extra}


class CatalogueTests(unittest.TestCase):
    def test_exact_normative_rows_original_text_and_source_hash(self):
        catalogue = review.load_catalogue()
        source = review.SPEC_PATH.read_bytes()
        self.assertEqual(len(catalogue["requirements"]), 51)
        self.assertEqual(len({r["requirement_id"] for r in catalogue["requirements"]}), 51)
        for row in catalogue["requirements"]:
            original = next(line for line in source.decode("utf-8").splitlines()
                            if line.startswith("| " + row["requirement_id"] + " |"))
            self.assertEqual(review._cells(original)[1], row["text"])
            self.assertEqual(review._cells(original)[2], row["observations"])
        self.assertEqual(catalogue["document"]["sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(catalogue["document"]["id"], "SRS-MOTOR-ECU-001")
        self.assertEqual(catalogue["document"]["version"], "0.1")

    def test_context_has_definitions_and_scope_without_authored_grades_or_tests(self):
        catalogue = review.load_catalogue()
        by_id = {r["requirement_id"]: r for r in catalogue["requirements"]}
        for row in catalogue["requirements"]:
            context = row["context"]
            for forbidden in ("D / SC", "D / F", "D / M", "R / SC", "要件の重要度と確認方法"):
                self.assertNotIn(forbidden, context)
            self.assertNotRegex(context, r"(?m)^\| AT-\d{3} \|")
            self.assertLessEqual(len(review._render_input(row, "document").encode("utf-16-le")) // 2,
                                 oracle.MAX_INPUT_CHARS)
        self.assertIn("FAULT/PWM=0/brake=1を意味する", by_id["MEC-SAF-002"]["context"])
        self.assertIn("T >= PWM[k-1]", by_id["MEC-PWM-002"]["context"])
        self.assertIn("delta[k] =", by_id["MEC-ENC-001"]["context"])
        self.assertIn("ハーネス側の要件", by_id["MEC-OBS-001"]["context"])
        self.assertIn("10 ms", catalogue["document_context"])
        self.assertIn("同一周期で競合する要求", catalogue["document_context"])

    def test_exact_eight_label_schemas(self):
        expected = {
            "ambiguity": ["ambiguous", "clear"], "testability": ["testable", "not_testable"],
            "atomicity": ["atomic", "compound"], "subject": ["specified", "missing"],
            "trigger": ["specified", "missing"], "completion": ["specified", "missing"],
            "quantitative": ["sufficient", "insufficient"], "safety": ["safety_related", "ordinary"],
        }
        self.assertEqual({name: list(spec["criteria"]) for name, spec in review.QUESTIONS.items()}, expected)
        self.assertNotIn(review.SAMPLE_REQUIREMENT, json.dumps(review.QUESTIONS, ensure_ascii=False))


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "requirement_reviews"

    def run_review(self, requirements=None, **kwargs):
        return review.run_review(requirements or [sample()], output_root=self.output, **kwargs)

    def test_eight_questions_whitelist_and_standalone_excludes_context(self):
        with patch.object(oracle, "_open", side_effect=classifier) as opened:
            report = self.run_review([sample(context="PRIVATE_CONTEXT", verification="D / SC",
                                              ground_truth="SECRET_LABEL", observations="SECRET_OBSERVATIONS")])
        self.assertEqual(opened.call_count, 8)
        for call in opened.call_args_list:
            body = json.loads(call.args[0].data)
            self.assertEqual(len(body["labels"]), 2)
            content = json.loads(body["inputs"][0])
            self.assertEqual(content, {"context_mode": "standalone", "target_requirement": review.SAMPLE_REQUIREMENT})
            for secret in ("LOCAL-ONLY-001", "PRIVATE_CONTEXT", "SECRET_LABEL", "SECRET_OBSERVATIONS", "D / SC"):
                self.assertNotIn(secret, call.args[0].data.decode())
        self.assertEqual(report["summary"]["completed"], 1)
        self.assertEqual(report["summary"]["abstained"], 0)
        self.assertEqual(report["results"][0]["context"], "")
        self.assertFalse(report["results"][0]["needs_review"])
        self.assertEqual(report["models"], ["jev-contract-fixture"])
        self.assertEqual(report["results"][0]["questions"]["ambiguity"]["confidence"], 0.73)
        self.assertEqual(report["results"][0]["questions"]["ambiguity"]["scores"], {"ambiguous": 0.2, "clear": 0.8})

    def test_document_mode_keeps_explicit_context_and_inputs_are_reproducible(self):
        item = review.load_catalogue()["requirements"][5]
        motor_latest = Path(self.temporary.name) / "latest.json"
        motor_latest.write_text("motor result", encoding="utf-8")
        with patch.object(oracle, "_open", side_effect=classifier) as opened:
            report = self.run_review([item], context_mode="document")
        content = json.loads(json.loads(opened.call_args.args[0].data)["inputs"][0])
        self.assertEqual(content["document_context"], item["context"])
        self.assertEqual(report["document"]["id"], "SRS-MOTOR-ECU-001")
        directory = Path(report["artifact_directory"])
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        for name, digest in config["source_hashes"].items():
            self.assertEqual(hashlib.sha256((directory / "sources" / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(hashlib.sha256((directory / "inputs.jsonl").read_bytes()).hexdigest(), config["inputs_sha256"])
        self.assertEqual(hashlib.sha256((directory / "questions.json").read_bytes()).hexdigest(), config["questions_sha256"])
        self.assertEqual(json.loads((self.output / "latest.json").read_text(encoding="utf-8")), report)
        self.assertTrue((directory / "report.md").is_file())
        self.assertEqual(motor_latest.read_text(encoding="utf-8"), "motor result")

    def test_aggregate_safety_not_a_defect_and_incomplete_does_not_pass(self):
        answers = {name: {"label": label, "abstained": False} for name, label in GOOD.items()}
        self.assertFalse(review._needs_review(answers))
        answers["safety"] = {"label": None, "abstained": True}
        self.assertIsNone(review._needs_review(answers))
        answers["subject"] = {"label": "missing", "abstained": False}
        self.assertTrue(review._needs_review(answers))

    def test_invalid_one_axis_preserves_seven_answers_and_fallback_metadata(self):
        def mixed(request, timeout):
            body = json.loads(request.data)
            if "ambiguous" in body["labels"]:
                return Response({"results": [{"label": "INVALID", "model": "jev-fixture"}]})
            return Response({"model": "other/fallback", "results": [{
                "label": next(value for value in GOOD.values() if value in body["labels"]),
                "confidence": None, "scores": None, "model": "other/fallback", "unscored": "fallback"}]})
        with patch.object(oracle, "_open", side_effect=mixed):
            report = self.run_review()
        self.assertEqual(report["summary"]["completed"], 0)
        self.assertEqual(report["summary"]["abstained"], 1)
        self.assertIsNone(report["results"][0]["needs_review"])
        self.assertTrue(report["results"][0]["questions"]["ambiguity"]["abstained"])
        self.assertEqual(report["results"][0]["questions"]["safety"]["model"], "other/fallback")
        self.assertIsNone(report["results"][0]["questions"]["safety"]["confidence"])
        self.assertEqual(report["summary"]["by_question"]["ambiguity"]["abstained"], 1)

    def test_network_failure_never_returns_canned_predictions(self):
        error = HTTPError(oracle.CLASSIFIER_URL, 403, "private text", Message(),
                          io.BytesIO(b'{"code":"forbidden","message":"KEEP_SECRET"}'))
        with patch.object(oracle, "_open", side_effect=error) as opened:
            report = self.run_review()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(report["summary"]["completed"], 0)
        self.assertEqual(report["summary"]["abstained"], 1)
        self.assertIsNone(report["results"][0]["needs_review"])
        self.assertTrue(all(answer["label"] is None for answer in report["results"][0]["questions"].values()))
        self.assertNotIn("KEEP_SECRET", json.dumps(report))

    def test_typesafe_eight_questions_one_request(self):
        def direct(request, timeout):
            body = json.loads(request.data)
            self.assertEqual(len(body["questions"]), 8)
            answers = {name: {"type": "choice", "choice": GOOD[name], "confidence": 0.73,
                              "probabilities": {label: 0.8 if label == GOOD[name] else 0.2 for label in spec["criteria"]}}
                       for name, spec in body["questions"].items()}
            return Response({"model": "jev-direct-fixture", "answers": answers})
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "fixture-key"}), \
                patch.object(oracle, "_open", side_effect=direct) as opened:
            report = self.run_review(provider="typesafe")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(report["summary"]["completed"], 1)

    def test_concurrent_motor_and_review_do_not_change_global_schema(self):
        original = json.dumps(oracle.QUESTIONS)
        with patch.object(oracle, "_open", side_effect=classifier), ThreadPoolExecutor(max_workers=2) as pool:
            motor = pool.submit(oracle.classify_windows, [{"window_id": "trace", "input": "trace data"}])
            requirement = pool.submit(self.run_review)
            motor_result, review_result = motor.result(), requirement.result()
        self.assertEqual(len(motor_result["predictions"][0]["questions"]), 5)
        self.assertIn("p_anomaly", motor_result["predictions"][0])
        self.assertEqual(len(review_result["results"][0]["questions"]), 8)
        self.assertEqual(json.dumps(oracle.QUESTIONS), original)

    def test_bad_configuration_and_duplicate_ids_fail_without_network(self):
        with patch.object(oracle, "_open") as opened:
            for kwargs in ({"context_mode": "unknown"}, {"provider": "unknown"}, {"batch_size": True}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    self.run_review(**kwargs)
            with self.assertRaises(ValueError):
                self.run_review([sample(), sample()])
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
