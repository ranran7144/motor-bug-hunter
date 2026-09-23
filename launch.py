"""Loopback-only dashboard server and background experiment worker."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
import webbrowser

from run import ROOT, run_experiment


class ExperimentState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.phase = 'ready'
        self.message = '実験を開始できます。'
        self.error = None
        self.report = None
        path = ROOT / 'artifacts' / 'latest.json'
        if path.exists():
            try:
                self.report = json.loads(path.read_text(encoding='utf-8'))
                self.phase = 'complete'
                self.message = '保存済みの実測結果を表示しています。再実行できます。'
            except (OSError, ValueError):
                self.message = '前回レポートを読み込めませんでした。新しい実験を開始できます。'

    def snapshot(self):
        with self.lock:
            return {'running': self.running, 'phase': self.phase, 'message': self.message,
                    'error': self.error, 'report': self.report}

    def start(self, provider, seed):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.phase = 'running'
            self.error = None
            self.message = '実験を準備しています。'
        threading.Thread(target=self._worker, args=(provider, seed), daemon=True).start()
        return True

    def _worker(self, provider, seed):
        def progress(message):
            with self.lock:
                self.message = str(message)
        try:
            report = run_experiment(provider, seed, progress=progress)
            with self.lock:
                self.report = report
                self.phase = 'complete'
                if report['errors']:
                    self.message = '完了。一部API判定は保留です。エラーはJSONレポートに保存しました。'
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                self.phase = 'error'
                self.message = '実験を完了できませんでした。'
        finally:
            with self.lock:
                self.running = False


class RequirementReviewState:
    """A review job has its own artifacts and does not overwrite SIL results."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.phase = 'ready'
        self.message = '要件を選択してJevで判定できます。'
        self.error = None
        self.report = None
        path = ROOT / 'artifacts' / 'requirement_reviews' / 'latest.json'
        if path.exists():
            try:
                self.report = json.loads(path.read_text(encoding='utf-8'))
                self.phase = 'complete'
                self.message = '保存済みの要件レビュー結果を表示しています。'
            except (OSError, ValueError):
                self.message = '保存済みレビューを読めませんでした。新しい判定を開始できます。'

    def snapshot(self):
        with self.lock:
            return {'running': self.running, 'phase': self.phase,
                    'message': self.message, 'error': self.error, 'report': self.report}

    def start(self, requirements, provider, context_mode):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.phase = 'running'
            self.error = None
            self.message = f'{len(requirements)}件 × 8観点の判定を準備しています。'
        threading.Thread(target=self._worker, args=(requirements, provider, context_mode), daemon=True).start()
        return True

    def _worker(self, requirements, provider, context_mode):
        def progress(message):
            with self.lock:
                self.message = str(message)
        try:
            from requirement_review import run_review
            report = run_review(requirements, provider=provider, context_mode=context_mode, progress=progress)
            with self.lock:
                self.report = report
                self.phase = 'complete'
                self.message = ('判定が完了しました。一部は保留です。詳細をレポートで確認してください。'
                                if report.get('errors') else '8観点の判定が完了しました。')
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                self.phase = 'error'
                self.message = '要件判定を完了できませんでした。'
        finally:
            with self.lock:
                self.running = False


def review_selection(payload):
    """Only the fixed local specification and explicit textarea text are inputs."""
    if not isinstance(payload, dict):
        raise ValueError('JSONオブジェクトが必要です。')
    provider = payload.get('provider', 'classifier')
    context_mode = payload.get('context_mode', 'standalone')
    mode = payload.get('mode', 'custom')
    if provider not in ('classifier', 'typesafe'):
        raise ValueError('判定プロバイダが不正です。')
    if context_mode not in ('standalone', 'document'):
        raise ValueError('文脈の指定が不正です。')
    from requirement_review import load_catalogue
    catalogue = load_catalogue()
    if mode == 'custom':
        text = payload.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('要件文は1〜4000文字で入力してください。')
        # Document context is an explicit choice, never silently added to sample text.
        context = catalogue.get('document_context', '') if context_mode == 'document' else ''
        selected = [{'requirement_id': 'CUSTOM-001', 'text': text.strip(), 'context': context}]
    elif mode == 'all':
        selected = catalogue['requirements']
    elif mode == 'selected':
        ids = payload.get('requirement_ids')
        if (not isinstance(ids, list) or not ids or len(ids) > len(catalogue['requirements'])
                or any(not isinstance(identifier, str) for identifier in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('仕様書から1件以上の重複しない要件IDを選択してください。')
        lookup = {row['requirement_id']: row for row in catalogue['requirements']}
        if any(identifier not in lookup for identifier in ids):
            raise ValueError('仕様書に存在しない要件IDです。')
        selected = [lookup[identifier] for identifier in ids]
    else:
        raise ValueError('実行モードが不正です。')
    return selected, provider, context_mode


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def state(self):
        return self.server.experiment_state

    def send(self, status, body, mime='application/json; charset=utf-8', download=False,
             filename='motor-bug-hunter-report.json'):
        data = (json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
                if not isinstance(body, bytes) else body)
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        if download:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def local_request(self):
        expected_port = self.server.server_address[1]
        try:
            host = urlsplit('http://' + self.headers.get('Host', ''))
            if host.hostname not in ('127.0.0.1', 'localhost') or host.port != expected_port:
                return False
            origin = self.headers.get('Origin')
            if origin and origin != f'http://{self.headers.get("Host")}':
                return False
            return True
        except ValueError:
            return False

    def do_GET(self):
        if not self.local_request():
            return self.send(403, {'error': 'Local origin required'})
        path = urlsplit(self.path).path
        if path == '/api/requirements/catalogue':
            from requirement_review import load_catalogue
            try:
                return self.send(200, load_catalogue())
            except (ValueError, OSError) as exc:
                return self.send(500, {'error': str(exc)})
        if path == '/api/requirements/status':
            return self.send(200, self.server.review_state.snapshot())
        if path == '/api/requirements/report':
            report = self.server.review_state.snapshot()['report']
            return self.send(200 if report else 404, report or {'error': 'No review yet'},
                             download=bool(report), filename='requirement-review.json')
        if path == '/api/status':
            return self.send(200, self.state.snapshot())
        if path == '/api/report':
            report = self.state.snapshot()['report']
            return self.send(200 if report else 404, report or {'error': 'No report yet'}, download=bool(report))
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/index.html': ('index.html', 'text/html; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/requirements': ('requirements.html', 'text/html; charset=utf-8'),
                 '/requirements.html': ('requirements.html', 'text/html; charset=utf-8'),
                 '/requirements.js': ('requirements.js', 'text/javascript; charset=utf-8'),
                 '/requirements.css': ('requirements.css', 'text/css; charset=utf-8')}
        if path not in files:
            return self.send(404, {'error': 'Not found'})
        name, mime = files[path]
        self.send(200, (ROOT / name).read_bytes(), mime)

    def do_POST(self):
        if not self.local_request():
            return self.send(403, {'error': 'Local origin required'})
        if urlsplit(self.path).path == '/api/requirements/run':
            return self.post_requirement_review()
        if urlsplit(self.path).path != '/api/run':
            return self.send(404, {'error': 'Not found'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error': 'application/json required'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048:
                raise ValueError('Invalid request length')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Expected object')
            provider = payload.get('provider', 'classifier')
            seed = payload.get('seed', 20260920)
            if provider not in ('off', 'classifier', 'typesafe'):
                raise ValueError('Unknown provider')
            if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
                raise ValueError('Seed must be an integer between 0 and 4294967295')
        except (ValueError, TypeError):
            return self.send(400, {'error': 'Invalid provider or seed'})
        if not self.state.start(provider, seed):
            return self.send(409, {'error': 'An experiment is already running'})
        self.send(202, {'started': True})

    def post_requirement_review(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error': 'application/json required'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 131072:
                raise ValueError('リクエストのサイズが不正です。')
            payload = json.loads(self.rfile.read(length))
            requirements, provider, context_mode = review_selection(payload)
        except (ValueError, TypeError) as exc:
            return self.send(400, {'error': str(exc)})
        if not self.server.review_state.start(requirements, provider, context_mode):
            return self.send(409, {'error': '要件レビューを実行中です。完了後に再実行してください。'})
        self.send(202, {'started': True})


def create_server(port=8767):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.experiment_state = ExperimentState()
    server.review_state = RequirementReviewState()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    try:
        server = create_server(args.port)
    except OSError as exc:
        raise SystemExit(f'Port {args.port} cannot be opened: {exc}. Try --port 8768.') from None
    url = f'http://127.0.0.1:{server.server_address[1]}'
    print(f'Motor ECU Bug Hunter: {url}', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
