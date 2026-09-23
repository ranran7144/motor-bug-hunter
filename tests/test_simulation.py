"""Behavioral contract tests against the compiled C implementation."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import simulation


class SimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = simulation.run_suite(seed=20260920)
        cls.mutants = {case["bug_id"]: case for case in cls.suite["cases"] if case["truth"]}
        cls.references = {case["scenario"]: case for case in cls.suite["cases"] if not case["truth"]}

    def test_all_thirty_mutations_have_observable_requirement_witnesses(self):
        self.assertEqual(set(self.mutants), set(range(1, 31)))
        self.assertEqual(len(self.references), 30)
        for bug_id, case in self.mutants.items():
            with self.subTest(bug_id=bug_id):
                self.assertTrue(case["activation_verified"])
                self.assertEqual(len(case["trace"]), 100)
                witness = case["ground_truth_witness"]
                self.assertIn(witness["cycle"], case["divergence_cycles"])
                self.assertNotEqual(witness["observed"], witness["reference"])

    def test_reference_is_clean_and_coasting_is_permitted(self):
        coasting = []
        for case in self.references.values():
            self.assertFalse(case["conventional"]["detected"], case["scenario"])
            self.assertFalse(case["truth"])
            coasting.extend(row for row in case["trace"] if row["pwm"] == 0 and row["motor_speed"] > 5)
        self.assertTrue(coasting, "The plant should coast after a stop or safety trip")

    def test_reference_and_mutant_replay_identical_inputs(self):
        for case in self.mutants.values():
            normal = self.references[case["scenario"]]
            self.assertEqual(case["reference_trace"], normal["trace"])
            for observed, reference in zip(case["trace"], normal["trace"]):
                self.assertEqual({key: observed[key] for key in simulation.INPUT_FIELDS},
                                 {key: reference[key] for key in simulation.INPUT_FIELDS})

    def test_seed_replays_exactly(self):
        repeated = simulation.run_suite(seed=20260920)
        self.assertEqual(self.suite, repeated)
        changed = simulation.run_suite(seed=20260921)
        original_inputs = {case["scenario"]: case["trace"] for case in self.suite["cases"] if not case["truth"]}
        changed_inputs = {case["scenario"]: case["trace"] for case in changed["cases"] if not case["truth"]}
        self.assertNotEqual(original_inputs, changed_inputs)

    def test_one_tick_estop_failure_is_real_and_then_clears(self):
        case = self.mutants[1]
        trace = case["trace"]
        index = next(i for i, row in enumerate(trace) if row["emergency_stop"])
        self.assertGreater(trace[index]["pwm"], 0)
        self.assertEqual(trace[index + 1]["pwm"], 0)
        self.assertEqual(case["reference_trace"][index]["pwm"], 0)
        self.assertGreater(trace[index]["motor_speed"], 0)

    def test_counter_fixtures_activate_real_c_boundaries(self):
        wrap = self.mutants[3]
        bad_wrap = next(row for row in wrap["trace"] if row["cycle"] == 65536)
        good_wrap = next(row for row in wrap["reference_trace"] if row["cycle"] == 65536)
        self.assertEqual(bad_wrap["state"], "FAULT")
        self.assertEqual(good_wrap["state"], "STARTING")
        thousand = self.mutants[15]
        witness_cycle = thousand["ground_truth_witness"]["cycle"]
        bad_start = next(row for row in thousand["trace"] if row["cycle"] == witness_cycle)
        self.assertEqual(bad_start["start_count"], 1000)
        self.assertEqual(bad_start["state"], "OFF")
        self.assertIn("999", thousand["public_context"])

    def test_mutation_selection_without_symptom_is_not_ground_truth(self):
        mutations = json.loads((simulation.ROOT / "mutations.json").read_text(encoding="utf-8"))["mutations"]
        for mutation in mutations:
            reference = self.references[mutation["scenario"]]["trace"]
            with self.subTest(bug_id=mutation["id"]):
                with self.assertRaises(AssertionError):
                    simulation.activation_witness(mutation, reference, reference)

    def test_model_disclosures_do_not_leak_case_labels_into_public_context(self):
        self.assertEqual({case["bug_id"] for case in self.mutants.values() if case["model_only"]}, {11, 12})
        for case in self.mutants.values():
            normal = self.references[case["scenario"]]
            self.assertEqual(case["public_context"], normal["public_context"])
            self.assertNotIn("mutation", case["public_context"].lower())
            self.assertNotIn("volatile", case["public_context"].lower())


if __name__ == "__main__":
    unittest.main()
