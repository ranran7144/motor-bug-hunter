import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluation import assemble_report, make_windows, wilson


class EvaluationTests(unittest.TestCase):
    def suite(self):
        return {'seed': 1, 'requirements': 'E-stop requires pwm=0.', 'cases': [
            {'case_id': 'PRIVATE_ID', 'bug_id': int(truth), 'title': 'SECRET_MUTATION_NAME',
             'category': 'INTERLOCK', 'severity': 'SAFETY_CRITICAL', 'truth': truth,
             'activation_verified': truth, 'trace': [{'cycle': 0, 'pwm': 20 if truth else 0,
             'state': 'RUNNING', 'emergency_stop': True, 'secret': 'DO_NOT_SEND'}],
             'conventional': {'detected': truth, 'violations': ['SECRET_RULE_RESULT'] if truth else []}}
            for truth in (True, False)]}

    def test_blinding(self):
        windows = make_windows(self.suite())
        for window in windows:
            for forbidden in ('SECRET', 'PRIVATE_ID', 'DO_NOT_SEND', 'bug_id', 'truth', 'conventional'):
                self.assertNotIn(forbidden, window['input'])
        self.assertNotEqual(windows[0]['window_id'], windows[1]['window_id'])

    def test_abstention_is_not_true_negative(self):
        suite = self.suite()
        report = assemble_report(suite, make_windows(suite), {}, provider='off', run_id='x', created_at='x')
        self.assertEqual(report['summary']['confusion'], {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0, 'abstained': 2})
        self.assertIsNone(report['summary']['ai_false_positive_rate'])
        self.assertIsNone(report['summary']['ai_detected'])

    def test_probability_mass_and_confusion(self):
        suite = self.suite()
        windows = make_windows(suite)
        result = {'predictions': [{'window_id': w['window_id'], 'questions': {'anomaly': {
            'label': 'NORMAL', 'confidence': 0.4,
            'scores': {'NORMAL': 0.4, 'SUSPICIOUS': 0.3, 'FAULT': 0.3}}}} for w in windows]}
        report = assemble_report(suite, windows, result, provider='classifier', run_id='x', created_at='x')
        self.assertEqual(report['summary']['confusion']['tp'], 1)
        self.assertEqual(report['summary']['confusion']['fp'], 1)
        self.assertEqual(report['summary']['ai_false_positive_rate'], 1)
        self.assertAlmostEqual(report['summary']['brier_score'], 0.26)
        self.assertIsNone(report['summary']['estimated_million_cost_usd'])

    def test_zero_false_positives_still_has_uncertainty(self):
        low, high = wilson(0, 30)
        self.assertAlmostEqual(low, 0)
        self.assertGreater(high, 0.1)

    def test_silent_truncation_forbidden(self):
        suite = self.suite()
        suite['requirements'] = 'x' * 33000
        with self.assertRaises(ValueError):
            make_windows(suite)


if __name__ == '__main__':
    unittest.main()
