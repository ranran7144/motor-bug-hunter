"""Headless checks for requirements UI; every newly submitted review is mocked."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cdp_client import CDP, find_browser
import launch
import oracle
import requirement_review


def main():
    server = launch.create_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tempfile.TemporaryDirectory(prefix='requirements-browser-')
    browser = cdp = None
    screenshots = ROOT / 'screenshots'
    screenshots.mkdir(exist_ok=True)
    calls = []

    def mock_review(requirements, provider, context_mode, progress):
        calls.append((requirements, provider, context_mode))
        definitions = requirement_review.load_catalogue()['questions']
        rows = []
        for row in requirements:
            questions = {}
            for definition in definitions:
                labels = definition['labels']
                questions[definition['id']] = {
                    'label': labels[0], 'scores': {labels[0]: .7, labels[1]: .3},
                    'confidence': .4, 'provider': provider, 'model': 'MOCK-NOT-JEV', 'abstained': False,
                }
            rows.append({'requirement_id': row['requirement_id'], 'text': row['text'],
                         'context': row.get('context', '') if context_mode == 'document' else '',
                         'questions': questions, 'needs_review': True, 'models': ['MOCK-NOT-JEV']})
        return {'run_id': 'BROWSER-MOCK-' + str(len(calls)), 'provider': provider,
                'created_at': datetime.now(timezone.utc).isoformat(), 'context_mode': context_mode,
                'results': rows, 'models': ['MOCK-NOT-JEV'], 'question_definitions': definitions,
                'requests': [], 'errors': [], 'document': requirement_review.load_catalogue()['document'],
                'summary': {'requirements': len(rows), 'completed': len(rows), 'abstained': 0,
                            'needs_review': len(rows), 'by_question': {}}, 'artifact_directory': None}

    try:
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        browser = subprocess.Popen([
            find_browser(), '--headless=new', '--disable-gpu', '--in-process-gpu',
            '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
            '--disable-component-update', '--disable-sync', '--metrics-recording-only',
            '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
            f'--user-data-dir={profile.name}', 'about:blank',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **options)
        port_file = Path(profile.name) / 'DevToolsActivePort'
        deadline = time.monotonic() + 20
        while not port_file.exists():
            if browser.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Headless browser did not start')
            time.sleep(.1)
        port = port_file.read_text().splitlines()[0]
        with urlopen(f'http://127.0.0.1:{port}/json/list') as response:
            pages = json.load(response)
        cdp = CDP(next(page['webSocketDebuggerUrl'] for page in pages if page['type'] == 'page'))
        cdp.call('Runtime.enable')
        cdp.call('Page.enable')
        cdp.call('Emulation.setDeviceMetricsOverride', width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        with patch.object(requirement_review, 'run_review', side_effect=mock_review), patch.object(
                oracle, '_open', side_effect=AssertionError('Browser smoke must not contact providers')):
            cdp.call('Page.navigate', url=f'http://127.0.0.1:{server.server_port}/requirements')
            cdp.wait_for('document.querySelector("#req-run") && !document.querySelector("#req-run").disabled')
            assert cdp.evaluate('document.querySelector("#requirement-text").value') == requirement_review.SAMPLE_REQUIREMENT
            assert cdp.evaluate('document.querySelector("#context-mode").value') == 'standalone'
            assert not calls, 'Opening the UI must not launch inference'
            cdp.evaluate('new Promise(resolve => setTimeout(resolve, 800))')
            cdp.screenshot(screenshots / 'requirements.png')
            cdp.call('Emulation.setDeviceMetricsOverride', width=390, height=844, deviceScaleFactor=1, mobile=True)
            cdp.screenshot(screenshots / 'requirements-mobile.png')
            assert cdp.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            cdp.call('Emulation.setDeviceMetricsOverride', width=1440, height=1000, deviceScaleFactor=1, mobile=False)
            cdp.click('#req-run')
            cdp.wait_for('document.body.textContent.includes("BROWSER-MOCK-1")')
            assert len(calls) == 1
            assert calls[0][2] == 'standalone'
            assert calls[0][0][0]['context'] == ''
            assert cdp.evaluate('document.querySelectorAll("#req-results-body tr").length') == 1
            assert cdp.evaluate('document.body.textContent.includes("MOCK-NOT-JEV")')
            cdp.click('#req-results-body tr')
            assert cdp.evaluate('document.querySelector("#req-detail").textContent').strip()
            assert not cdp.js_errors, cdp.js_errors
            print('PASS: requirements UI loads saved report; no automatic inference; standalone sample '
                  'review uses mocked provider; result/detail and mobile layout work; no JS errors.')
            print(f'Screenshots: {screenshots}')
    finally:
        if cdp:
            cdp.close()
        if browser:
            browser.terminate()
            try:
                browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait(timeout=5)
            browser.stderr.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        for attempt in range(10):
            try:
                profile.cleanup()
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(.2)


if __name__ == '__main__':
    main()
