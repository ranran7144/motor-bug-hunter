"""Reproducible experiment runner; no third-party Python packages required."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import hashlib
import platform
from pathlib import Path
import random
import uuid

from evaluation import assemble_report, make_windows

ROOT = Path(__file__).resolve().parent


def save_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def run_experiment(provider='off', seed=20260920, batch_size=10, progress=print,
                   output_root=None, max_windows=None, static_analysis=True):
    if provider not in ('off', 'classifier', 'typesafe'):
        raise ValueError('Unknown provider')
    from simulation import run_suite
    now = datetime.now(timezone.utc)
    run_id = now.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    directory = Path(output_root or ROOT / 'artifacts') / run_id
    directory.mkdir(parents=True, exist_ok=False)
    snapshot = directory / 'sources'
    snapshot.mkdir()
    source_hashes = {}
    for name in ('controller.c', 'simulation.py', 'requirements.json', 'mutations.json',
                 'oracle.py', 'evaluation.py', 'run.py', 'static_analysis.py'):
        source = ROOT / name
        if source.exists():
            content = source.read_bytes()
            (snapshot / name).write_bytes(content)
            source_hashes[name] = hashlib.sha256(content).hexdigest()
    save_json(directory / 'manifest.json', {'seed': seed, 'provider': provider,
              'batch_size': batch_size, 'max_windows': max_windows, 'source_sha256': source_hashes,
              'python': platform.python_version(), 'platform': platform.platform()})
    progress('Cコントローラをビルドし、正常版と30変異のSILを実行しています。')
    suite = run_suite(seed=seed)
    suite['seed'] = seed
    save_json(directory / 'suite.json', suite)
    analysis = {'status': 'not_run', 'detected': None}
    if static_analysis:
        from static_analysis import analyze_controller
        progress('Cの静的解析を実行しています。診断と変異検出数は区別します。')
        analysis = analyze_controller()
        save_json(directory / 'static-analysis.json', analysis)
    windows = make_windows(suite)
    with (directory / 'windows.jsonl').open('w', encoding='utf-8') as handle:
        for window in windows:
            handle.write(json.dumps(window, ensure_ascii=False) + '\n')
    result = {'predictions': [], 'requests': [], 'errors': []}
    selected = windows[:]
    random.Random(seed).shuffle(selected)  # Blinded order; no class grouping in API batches.
    if max_windows is not None:
        selected = selected[:max_windows]
    if provider != 'off':
        from oracle import classify_windows
        progress(f'{len(selected)}ウィンドウ × 5問をAPIで評価しています。')
        result = classify_windows(selected, provider=provider, batch_size=batch_size, progress=progress)
    save_json(directory / 'oracle.json', result)
    report = assemble_report(suite, windows, result, provider=provider,
                             run_id=run_id, created_at=now.isoformat())
    report['static_analysis'] = analysis
    report['artifact_directory'] = str(directory)
    save_json(directory / 'report.json', report)
    save_json(directory.parent / 'latest.json', report)
    (directory / 'report.md').write_text(markdown_report(report), encoding='utf-8')
    progress('完了。実測レポートと入力・応答・トレースを保存しました。')
    return report


def markdown_report(report):
    s = report['summary']
    n = s['bug_types_total']
    def count(value):
        return '未評価' if value is None else f'{value}/{n}'
    fpr = s['ai_false_positive_rate']
    lines = ['# Motor ECU Bug Hunter — 実測レポート', '',
             f"Run: `{report['run_id']}` / seed: `{report['seed']}` / provider: `{report['provider']}`", '',
             '| 項目 | 結果 |', '|---|---|',
             f"| 不具合の活性化確認 | {s['activated_bug_types']}/{n} |",
             f"| 従来ルール | {count(s['conventional_detected'])} |",
             f"| AI | {count(s['ai_detected'])} |",
             f"| 従来 + AI | {count(s['combined_detected'])} |",
             f"| AI採点済み | {s['ai_evaluated']}/{s['ai_total']} windows |",
             f"| 正常ケース誤検出率 | {'未評価' if fpr is None else f'{fpr:.1%}'} (n={s['ai_healthy_evaluated']}) |",
             f"| 静的解析 | {report['static_analysis']['status']}（種類別検出数は未測定） |", '',
             'Models: ' + (', '.join(report['models']) or '未評価'), '',
             '## 評価上の制約', '']
    lines.extend('- ' + note for note in report['notes'])
    if report['errors']:
        lines.extend(['', '## APIエラー', '', json.dumps(report['errors'], ensure_ascii=False, indent=2)])
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['off', 'classifier', 'typesafe'], default='off')
    parser.add_argument('--seed', type=int, default=20260920)
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--max-windows', type=int)
    parser.add_argument('--output-root', type=Path)
    parser.add_argument('--skip-static-analysis', action='store_true')
    args = parser.parse_args()
    if args.max_windows is not None and args.max_windows < 1:
        parser.error('--max-windows must be positive')
    report = run_experiment(args.provider, args.seed, args.batch_size,
                            output_root=args.output_root, max_windows=args.max_windows,
                            static_analysis=not args.skip_static_analysis)
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    print(report['artifact_directory'])


if __name__ == '__main__':
    main()
