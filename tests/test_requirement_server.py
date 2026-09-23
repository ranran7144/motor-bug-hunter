"""Exercise requirement review HTTP boundaries without external inference."""
from pathlib import Path
import json
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launch


class RequirementServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = launch.create_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, data=None, headers=None):
        request = Request(self.base + path,
                          data=None if data is None else json.dumps(data).encode(),
                          headers=headers or {'Content-Type': 'application/json'})
        try:
            with urlopen(request) as response:
                return response.status, response.read(), response.headers
        except HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def test_catalogue_and_separate_status(self):
        status, raw, _ = self.request('/api/requirements/catalogue')
        self.assertEqual(status, 200)
        catalogue = json.loads(raw)
        self.assertEqual(len(catalogue['requirements']), 51)
        self.assertEqual(len(catalogue['questions']), 8)
        self.assertEqual(self.request('/api/requirements/status')[0], 200)
        self.assertEqual(self.request('/api/status')[0], 200)

    def test_custom_standalone_never_inherits_document_context(self):
        with patch.object(self.server.review_state, 'start', return_value=True) as start:
            status, _, _ = self.request('/api/requirements/run', {'mode': 'custom', 'text': 'Requirement example'})
        self.assertEqual(status, 202)
        requirements, provider, context_mode = start.call_args.args
        self.assertEqual(requirements[0]['text'], 'Requirement example')
        self.assertEqual(requirements[0]['context'], '')
        self.assertEqual((provider, context_mode), ('classifier', 'standalone'))

    def test_selected_preserves_ids_and_explicit_context(self):
        ids = ['MEC-SAF-001', 'MEC-STA-006']
        with patch.object(self.server.review_state, 'start', return_value=True) as start:
            status, _, _ = self.request('/api/requirements/run', {
                'mode': 'selected', 'requirement_ids': ids, 'context_mode': 'document'})
        self.assertEqual(status, 202)
        requirements, _, context_mode = start.call_args.args
        self.assertEqual([row['requirement_id'] for row in requirements], ids)
        self.assertEqual(context_mode, 'document')
        self.assertTrue(requirements[0]['context'])

    def test_invalid_selection_never_starts(self):
        with patch.object(self.server.review_state, 'start') as start:
            for data in [[], {'text': ''}, {'text': True}, {'text': 'x' * 4001},
                         {'provider': 'off', 'text': 'x'}, {'mode': 'other'},
                         {'mode': 'selected', 'requirement_ids': []},
                         {'mode': 'selected', 'requirement_ids': ['../oracle.py']},
                         {'mode': 'selected', 'requirement_ids': ['MEC-SAF-001'] * 2},
                         {'mode': 'selected', 'requirement_ids': [True]},
                         {'text': 'x', 'context_mode': 'unknown'}]:
                with self.subTest(data=str(data)[:100]):
                    self.assertEqual(self.request('/api/requirements/run', data)[0], 400)
        start.assert_not_called()

    def test_all_and_busy(self):
        with patch.object(self.server.review_state, 'start', return_value=True) as start:
            self.assertEqual(self.request('/api/requirements/run', {'mode': 'all', 'context_mode': 'document'})[0], 202)
            self.assertEqual(len(start.call_args.args[0]), 51)
        with patch.object(self.server.review_state, 'start', return_value=False):
            self.assertEqual(self.request('/api/requirements/run', {'mode': 'custom', 'text': 'x'})[0], 409)

    def test_cross_origin_is_blocked(self):
        with patch.object(self.server.review_state, 'start') as start:
            status, _, _ = self.request('/api/requirements/run', {'text': 'x'},
                                       {'Content-Type': 'application/json', 'Origin': 'https://example.org'})
        self.assertEqual(status, 403)
        start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
