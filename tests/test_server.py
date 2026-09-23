from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launch import create_server


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(0)
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
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def test_status(self):
        status, raw = self.request('/api/status')
        self.assertEqual(status, 200)
        self.assertIn('running', json.loads(raw))

    def test_cross_origin_cannot_start(self):
        with patch.object(self.server.experiment_state, 'start') as start:
            status, _ = self.request('/api/run', {}, {'Content-Type': 'application/json', 'Origin': 'https://example.org'})
        self.assertEqual(status, 403)
        start.assert_not_called()

    def test_bad_host_rejected(self):
        self.assertEqual(self.request('/api/status', headers={'Host': 'evil.example'})[0], 403)

    def test_sources_and_artifacts_not_served(self):
        for path in ['/controller.c', '/oracle.py', '/artifacts/latest.json', '/../oracle.py']:
            self.assertEqual(self.request(path)[0], 404)

    def test_validation_and_single_job(self):
        with patch.object(self.server.experiment_state, 'start', return_value=True) as start:
            self.assertEqual(self.request('/api/run', {'provider': 'off', 'seed': 12})[0], 202)
            start.assert_called_once_with('off', 12)
        with patch.object(self.server.experiment_state, 'start', return_value=False):
            self.assertEqual(self.request('/api/run', {'provider': 'off', 'seed': 12})[0], 409)
        for payload in ([], {'seed': True}, {'provider': 'oops'}, {'seed': -1}):
            self.assertEqual(self.request('/api/run', payload)[0], 400)


if __name__ == '__main__':
    unittest.main()
