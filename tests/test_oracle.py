"""Offline contract tests: no calls to either inference service."""

from email.message import Message
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import oracle


class Response:
    def __init__(self, body):
        self.body = body
        self.headers = Message()
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size):
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return raw[:size]


def window(identifier="local-only-1"):
    return {"window_id": identifier, "input": "Requirement: E-stop forces zero PWM. Trace: E-stop=1 PWM=0.",
            "ground_truth": "SECRET_TRUE_LABEL", "mutation_id": "SECRET_MUTATION"}


def scored(labels, chosen=None):
    chosen = chosen or labels[0]
    return {"label": chosen, "confidence": 0.71,
            "scores": {label: 0.9 if label == chosen else 0.1 / (len(labels) - 1)
                       for label in labels}}


def classifier_response(request, timeout):
    body = json.loads(request.data)
    return Response({"model": "jev-1.13.0", "results": [scored(body["labels"]) for _ in body["inputs"]],
                     "usage": {"classifications": len(body["inputs"])}})


def http_error(status, code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return HTTPError(oracle.CLASSIFIER_URL, status, "provider error", headers,
                     io.BytesIO(json.dumps({"code": code, "error": "Do not log raw error bodies"}).encode()))


class OracleTests(unittest.TestCase):
    def test_classifier_five_independent_questions_only_text_no_keys(self):
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "KEEP_SECRET"}), \
                patch.object(oracle, "_open", side_effect=classifier_response) as opened:
            result = oracle.classify_windows([window()])
        self.assertEqual(opened.call_count, 5)
        self.assertEqual(result["errors"], [])
        for call in opened.call_args_list:
            request = call.args[0]
            self.assertEqual(request.full_url, oracle.CLASSIFIER_URL)
            self.assertIsNone(request.get_header("Authorization"))
            body = json.loads(request.data)
            self.assertEqual(body["inputs"], [window()["input"]])
            self.assertEqual(body["tier"], "fast")
            for secret in ("local-only-1", "SECRET_TRUE_LABEL", "SECRET_MUTATION", "KEEP_SECRET"):
                self.assertNotIn(secret, request.data.decode())
        prediction = result["predictions"][0]
        self.assertEqual(prediction["window_id"], "local-only-1")
        self.assertEqual(prediction["model"], "jev-1.13.0")
        self.assertAlmostEqual(prediction["p_anomaly"], 0.1)
        self.assertEqual(prediction["questions"]["anomaly"]["confidence"], 0.71)
        self.assertEqual(sum(r["count"] for r in result["requests"]), 5)
        self.assertTrue(all(r["cost_usd"] is None for r in result["requests"]))

    def test_batch_order_and_per_item_fallback_provenance(self):
        def mixed(request, timeout):
            body = json.loads(request.data)
            rows = [dict(scored(body["labels"]), model="jev-1.13.0"),
                    {"label": body["labels"][-1], "confidence": None, "scores": None,
                     "model": "other/fallback-model", "unscored": "fallback"}]
            return Response({"model": "mixed", "modelsUsed": ["jev-1.13.0", "other/fallback-model"],
                             "results": rows[:len(body["inputs"])]})
        with patch.object(oracle, "_open", side_effect=mixed) as opened:
            result = oracle.classify_windows([window("a"), window("b"), window("c")], batch_size=2)
        self.assertEqual(opened.call_count, 10)
        self.assertEqual([p["window_id"] for p in result["predictions"]], ["a", "b", "c"])
        fallback = result["predictions"][1]
        self.assertEqual(fallback["model"], "other/fallback-model")
        self.assertIsNone(fallback["p_anomaly"])
        self.assertIsNone(fallback["questions"]["anomaly"]["confidence"])
        self.assertEqual(fallback["questions"]["anomaly"]["unscored"], "fallback")
        self.assertIsNone(result["requests"][0]["usage"])

    def test_typesafe_single_request_five_questions_and_raw_confidence(self):
        def direct(request, timeout):
            body = json.loads(request.data)
            self.assertEqual(request.full_url, oracle.TYPESAFE_URL)
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
            self.assertEqual(body["state"], window()["input"])
            self.assertEqual(len(body["questions"]), 5)
            self.assertNotIn("SECRET_MUTATION", request.data.decode())
            answers = {}
            for name, question in body["questions"].items():
                result = scored(list(question["criteria"]))
                answers[name] = {"type": "choice", "choice": result["label"],
                                 "confidence": result["confidence"], "probabilities": result["scores"]}
            return Response({"model": "jev-resolved-version", "answers": answers,
                             "usage": {"input_tokens": 500, "output_tokens": 50}})
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-key"}), \
                patch.object(oracle, "_open", side_effect=direct) as opened:
            result = oracle.classify_windows([window()], provider="typesafe")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["predictions"][0]["model"], "jev-resolved-version")
        self.assertEqual(result["requests"][0]["usage"]["input_tokens"], 500)

    def test_malformed_one_item_abstains_without_corrupting_other_items(self):
        def malformed(request, timeout):
            body = json.loads(request.data)
            bad = scored(body["labels"])
            bad["scores"][body["labels"][0]] = True
            return Response({"model": "jev-test", "results": [bad, scored(body["labels"])]})
        with patch.object(oracle, "_open", side_effect=malformed):
            result = oracle.classify_windows([window("bad"), window("good")])
        self.assertEqual(len(result["errors"]), 5)
        for answer in result["predictions"][0]["questions"].values():
            self.assertTrue(answer["abstained"])
            self.assertIsNone(answer["label"])
        self.assertFalse(result["predictions"][1]["questions"]["anomaly"]["abstained"])

    def test_truncated_response_cannot_shift_window_mapping(self):
        with patch.object(oracle, "_open", return_value=Response({"results": []})):
            result = oracle.classify_windows([window("a"), window("b")])
        self.assertEqual(len(result["errors"]), 10)
        self.assertTrue(all(p["questions"]["anomaly"]["abstained"] for p in result["predictions"]))

    def test_retries_obey_retry_after_and_record_attempts(self):
        calls = 0

        def transient(request, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise http_error(429, "rate_limit_minute", 2)
            return classifier_response(request, timeout)
        with patch.object(oracle, "_open", side_effect=transient), patch.object(oracle.time, "sleep") as sleep:
            result = oracle.classify_windows([window()])
        sleep.assert_called_once_with(2.0)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["requests"]), 6)
        self.assertEqual(result["requests"][0]["status"], 429)
        self.assertEqual(result["requests"][0]["retry_wait_ms"], 2000)
        self.assertEqual(result["requests"][1]["attempt"], 2)

    def test_daily_or_long_rate_limit_stops_without_early_retry(self):
        for code, wait in (("rate_limit_day", 86400), ("rate_limit_minute", 55)):
            with self.subTest(code=code), \
                    patch.object(oracle, "_open", side_effect=http_error(429, code, wait)) as opened, \
                    patch.object(oracle.time, "sleep") as sleep:
                result = oracle.classify_windows([window("a"), window("b")], batch_size=1)
            self.assertEqual(opened.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(result["errors"][0]["retry_after"], wait)
            self.assertEqual(result["predictions"][1]["questions"]["anomaly"]["error"], "run_stopped")

    def test_retry_attempts_bounded_and_errors_redacted(self):
        def failure(request, timeout):
            raise http_error(502, "upstream_other")
        with patch.object(oracle, "_open", side_effect=failure) as opened, patch.object(oracle.time, "sleep"):
            result = oracle.classify_windows([window()])
        self.assertEqual(opened.call_count, 15)
        self.assertEqual(len(result["errors"]), 5)
        self.assertNotIn("Do not log raw error bodies", json.dumps(result))

    def test_invalid_input_limits_and_nonfinite_answers(self):
        invalid = [dict(window("empty"), input=""), dict(window("large"), input="x" * 32001),
                   dict(window("unicode"), input="\U0001f680" * 16001)]
        with patch.object(oracle, "_open") as opened:
            result = oracle.classify_windows(invalid)
        opened.assert_not_called()
        self.assertEqual(len(result["errors"]), 3)
        for bad in (float("nan"), float("inf"), -0.1, 1.1, True):
            raw = scored(list(oracle.QUESTIONS["anomaly"]["criteria"]))
            raw["confidence"] = bad
            with self.subTest(confidence=bad), self.assertRaises(ValueError):
                oracle._parse_answer(raw, "anomaly", "classifier", "jev")

    def test_local_budget_counts_all_five_questions(self):
        with patch.object(oracle, "FAST_PER_DAY", 3), patch.object(oracle, "_open", side_effect=classifier_response):
            result = oracle.classify_windows([window()])
        self.assertEqual(len(result["requests"]), 3)
        self.assertEqual(result["errors"][0]["code"], "local_daily_limit")

    def test_configuration_rejected_before_network(self):
        with patch.object(oracle, "_open") as opened:
            for settings in ({"provider": "unknown"}, {"batch_size": 0}, {"batch_size": 1001},
                             {"timeout": float("nan")}, {"timeout": True}):
                with self.subTest(settings=settings), self.assertRaises(ValueError):
                    oracle.classify_windows([window()], **settings)
            with self.assertRaises(ValueError):
                oracle.classify_windows([window(), window()])
            with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
                oracle.classify_windows([window()], provider="typesafe")
        opened.assert_not_called()

    def test_rounded_union_above_one_is_not_silently_capped(self):
        raw = {"label": "FAULT", "confidence": 0.7,
               "scores": {"NORMAL": 0, "SUSPICIOUS": 0.5001, "FAULT": 0.5001}}
        parsed = oracle._parse_answer(raw, "anomaly", "classifier", "jev")
        self.assertIsNone(parsed["p_anomaly"])
        self.assertEqual(parsed["scores"], raw["scores"])

    def test_redirects_are_not_followed(self):
        handler = oracle._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://other.example/"))


if __name__ == "__main__":
    unittest.main()
