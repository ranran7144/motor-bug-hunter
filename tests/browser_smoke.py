"""Exercise the real dashboard and offline C experiment in headless Chrome.

No provider calls; an existing saved report may contain prior real observations.
Run: python tests/browser_smoke.py
"""
from __future__ import annotations

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
from run import run_experiment


def main():
    server = launch.create_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tempfile.TemporaryDirectory(prefix='motor-browser-')
    output = tempfile.TemporaryDirectory(prefix='motor-smoke-results-')
    browser = cdp = None
    screenshots = ROOT / 'screenshots'
    screenshots.mkdir(exist_ok=True)

    def offline_run(provider, seed, progress):
        assert provider == 'off', 'Browser test must not call a provider'
        return run_experiment(provider, seed, progress=progress, output_root=output.name)

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
            time.sleep(0.1)
        debug_port = port_file.read_text().splitlines()[0]
        with urlopen(f'http://127.0.0.1:{debug_port}/json/list') as response:
            pages = json.load(response)
        cdp = CDP(next(page['webSocketDebuggerUrl'] for page in pages if page['type'] == 'page'))
        cdp.call('Runtime.enable')
        cdp.call('Page.enable')
        cdp.call('Emulation.setDeviceMetricsOverride', width=1440, height=1100, deviceScaleFactor=1, mobile=False)
        with patch.object(launch, 'run_experiment', side_effect=offline_run), patch.object(
                oracle, 'classify_windows', side_effect=AssertionError('No network in browser smoke')):
            cdp.call('Page.navigate', url=f'http://127.0.0.1:{server.server_port}/')
            cdp.wait_for('document.querySelector("#run-button") && !document.querySelector("#run-button").disabled')
            if server.experiment_state.report:
                expected = server.experiment_state.report['summary']
                cdp.wait_for('document.querySelector("#metric-activated").textContent === "30"')
                expected_ai = '—' if expected['ai_detected'] is None else str(expected['ai_detected'])
                assert cdp.evaluate('document.querySelector("#metric-ai").textContent') == expected_ai
                assert cdp.evaluate('document.querySelectorAll("#case-table-body tr").length') == 60
                cdp.evaluate('new Promise(resolve => setTimeout(resolve, 900))')
                cdp.screenshot(screenshots / 'dashboard.png')
                cdp.click('[data-filter="false-positive"]')
                assert cdp.evaluate('document.querySelectorAll("#case-table-body tr").length') == expected['confusion']['fp']
                cdp.click('[data-filter="healthy"]')
                assert cdp.evaluate('document.querySelectorAll("#case-table-body tr").length') == 30
                cdp.click('[data-filter="ai-only"]')
                expected_added = sum(c['truth'] and c['ai_detected'] is True and not c['conventional_detected']
                                     for c in server.experiment_state.report['cases'])
                assert cdp.evaluate('document.querySelectorAll("#case-table-body tr").length') == expected_added
                if not expected_added:
                    cdp.click('[data-filter="healthy"]')
                cdp.click('#case-table-body tr')
                assert not cdp.evaluate('document.querySelector("#inspector-content").hidden')
                assert cdp.evaluate('document.querySelectorAll("#questions-grid > *").length') == 5
                cdp.evaluate('document.querySelector("#cycle-index").value=40; document.querySelector("#cycle-index").dispatchEvent(new Event("input"))')
                assert cdp.evaluate('document.querySelector("#cycle-index").value') == '40'
                assert cdp.evaluate('document.querySelector("#cycle-label").textContent').startswith('cycle ')
                cdp.call('Emulation.setDeviceMetricsOverride', width=390, height=844, deviceScaleFactor=1, mobile=True)
                cdp.evaluate('window.dispatchEvent(new Event("resize"))')
                cdp.screenshot(screenshots / 'mobile.png')
                assert cdp.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'Page overflows mobile viewport'
                cdp.call('Emulation.setDeviceMetricsOverride', width=1440, height=1100, deviceScaleFactor=1, mobile=False)
            cdp.evaluate('document.querySelector("#provider").value="off"; document.querySelector("#provider").dispatchEvent(new Event("change")); document.querySelector("#seed").value="20260920"')
            cdp.click('#run-button')
            cdp.wait_for('document.querySelector("#run-button").disabled')
            cdp.wait_for('!document.querySelector("#run-button").disabled && document.querySelector("#metric-activated").textContent === "30"', timeout=30)
            assert server.experiment_state.report['provider'] == 'off'
            assert server.experiment_state.report['summary']['ai_detected'] is None
            assert cdp.evaluate('document.querySelector("#metric-ai").textContent') == '—'
            assert not cdp.evaluate('document.querySelector("#error-banner").textContent')
            assert not cdp.js_errors, cdp.js_errors
            print('PASS: real report, filters, five questions, trace slider, mobile layout, real offline C run, null AI; no browser errors.')
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
        for directory in (profile, output):
            for attempt in range(10):
                try:
                    directory.cleanup()
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.2)


if __name__ == '__main__':
    main()
