"""Blinded trace serialization and measured, abstention-aware experiment metrics."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from statistics import mean

THRESHOLD = 0.5  # Fixed before inference; never tuned on this demonstration corpus.
TRACE_FIELDS = (
    'cycle', 'state', 'pwm', 'start_switch', 'stop_switch', 'emergency_stop',
    'motor_current', 'motor_speed', 'encoder_position', 'limit_switch',
    'temperature', 'communication_alive', 'reset_fault', 'fault_reset',
    'sensor_valid', 'current_valid', 'speed_valid', 'temperature_valid',
    'isr_estop', 'isr_event', 'watchdog_due', 'watchdog_age', 'tick_ms',
    'elapsed_ms', 'state_ticks', 'timer', 'run_counter', 'fault_latched',
    'estimated_speed', 'measured_speed', 'speed_estimate', 'start_count',
    'comm_age', 'comm_ticks', 'startup_ticks', 'stop_ticks', 'fault_code',
    'target_pwm', 'isr_window', 'brake', 'start_elapsed', 'stop_elapsed', 'stall_count',
)


def make_windows(suite: dict) -> list[dict]:
    """IDs and ground truth stay outside the exact string sent to the provider."""
    requirements = suite['requirements']
    if isinstance(requirements, dict) and 'text' in requirements:
        requirements = requirements['text'] + '\nUnits: ' + json.dumps(requirements.get('units', {}), ensure_ascii=False)
    elif not isinstance(requirements, str):
        requirements = json.dumps(requirements, ensure_ascii=False, separators=(',', ':'))
    windows = []
    for index, case in enumerate(suite['cases']):
        trace = case['trace']
        if not trace or len(trace) > 100:
            raise ValueError('Expected a preplanned 1–100-cycle window, without event selection.')
        fields = [field for field in TRACE_FIELDS if any(field in row for row in trace)]
        stream = io.StringIO(newline='')
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(fields)
        for row in trace:
            writer.writerow([int(row[f]) if isinstance(row.get(f), bool) else
                             round(row[f], 3) if isinstance(row.get(f), float) else
                             row.get(f, '') for f in fields])
        context = case.get('public_context', '')
        text = (
            'Evaluate this software-in-the-loop motor controller observation. '
            'Rows are consecutive post-step observations; input columns are sampled before '
            'that step. Booleans are 0/1. Physical speed may coast after PWM becomes zero. '
            'Use only observable evidence; do not infer a race or missing volatile solely '
            'from output symptoms. Questions about continuing apply only to simulation.\n'
            f'Requirements:\n{requirements}\nInitial conditions / units:\n{context}\n'
            f'Trace (CSV, {len(trace)} cycles):\n{stream.getvalue()}'
        )
        if len(text) > 32000:
            raise ValueError(f'Window {index} is {len(text)} characters; refuses silent truncation.')
        identifier = hashlib.sha256(f'{suite.get("seed", 0)}:{index}'.encode()).hexdigest()[:16]
        windows.append({'window_id': identifier, 'input': text})
    return windows


def wilson(successes: int, count: int) -> list[float] | None:
    if not count:
        return None
    z = 1.959963984540054
    p = successes / count
    denom = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denom
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * count)) / count) / denom
    return [max(0, center - radius), min(1, center + radius)]


def _probability(question: dict) -> float | None:
    scores = question.get('scores')
    if question.get('abstained') or not isinstance(scores, dict):
        return None
    keys = ('NORMAL', 'SUSPICIOUS', 'FAULT')
    if any(not isinstance(scores.get(k), (int, float)) or isinstance(scores[k], bool)
           or not math.isfinite(scores[k]) or not 0 <= scores[k] <= 1 for k in keys):
        return None
    if abs(sum(scores[k] for k in keys) - 1) > 0.02:
        return None
    mass = scores['SUSPICIOUS'] + scores['FAULT']
    return mass if mass <= 1 else None


def assemble_report(suite: dict, windows: list[dict], oracle: dict, *,
                    provider: str, run_id: str, created_at: str) -> dict:
    predictions = {p['window_id']: p for p in oracle.get('predictions', [])}
    cases, confidences, briers, models = [], [], [], set()
    confusion = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0, 'abstained': 0}
    for source, window in zip(suite['cases'], windows, strict=True):
        prediction = predictions.get(window['window_id'], {})
        questions = prediction.get('questions', {})
        anomaly = questions.get('anomaly', {})
        probability = _probability(anomaly)
        detected = None if probability is None else probability >= THRESHOLD
        truth = bool(source['truth'])
        if detected is None:
            confusion['abstained'] += 1
        else:
            confusion[('t' if detected == truth else 'f') + ('p' if detected else 'n')] += 1
            briers.append((probability - int(truth)) ** 2)
        confidence = anomaly.get('confidence')
        if isinstance(confidence, (float, int)) and not isinstance(confidence, bool) and 0 <= confidence <= 1:
            confidences.append(confidence)
        else:
            confidence = None
        for question in questions.values():
            if isinstance(question, dict) and question.get('model'):
                models.add(str(question['model']))
        model = prediction.get('model')
        if model:
            if isinstance(model, list):
                models.update(map(str, model))
            else:
                models.add(str(model))
        conventional = source['conventional']
        cases.append({
            **source, 'window_id': window['window_id'],
            'conventional_detected': bool(conventional['detected']),
            'violations': conventional.get('violations', []),
            'ai_detected': detected, 'ai_status': anomaly.get('label'),
            'confidence': confidence, 'p_anomaly': probability, 'questions': questions,
            'model': model, 'input_sha256': hashlib.sha256(window['input'].encode()).hexdigest(),
        })
    defects = [c for c in cases if c['truth']]
    healthy = [c for c in cases if not c['truth']]
    ai_cases = [c for c in cases if c['ai_detected'] is not None]
    healthy_scored = [c for c in healthy if c['ai_detected'] is not None]
    active = {c['bug_id'] for c in defects if c['activation_verified']}
    baseline_ids = {c['bug_id'] for c in defects if c['conventional_detected']}
    ai_ids = {c['bug_id'] for c in defects if c['ai_detected'] is True}
    requests = oracle.get('requests', [])
    latency = [r['latency_ms'] for r in requests if isinstance(r.get('latency_ms'), (int, float))]
    costs = []
    for request in requests:
        usage = request.get('usage') or {}
        value = usage.get('cost', usage.get('cost_usd')) if isinstance(usage, dict) else None
        if isinstance(value, (int, float)) and value >= 0:
            costs.append(value)
    # No extrapolation from partially reported usage or from provider's internal cost.
    reported_cost = (sum(costs) if provider == 'typesafe' and requests
                     and len(costs) == len(requests) else None)
    million_cost = (reported_cost / len(ai_cases) * 1_000_000
                    if provider == 'typesafe' and ai_cases and reported_cost is not None
                    and not oracle.get('errors') and len(ai_cases) == len(cases) else None)
    summary = {
        'bug_types_total': len({c['bug_id'] for c in cases if c['bug_id']}),
        'activated_bug_types': len(active), 'conventional_detected': len(baseline_ids),
        'ai_detected': len(ai_ids) if ai_cases else None,
        'combined_detected': len(baseline_ids | ai_ids) if ai_cases else None,
        'healthy_windows': len(healthy), 'ai_healthy_evaluated': len(healthy_scored),
        'ai_evaluated': len(ai_cases), 'ai_total': len(cases),
        'ai_bug_types_evaluated': len({c['bug_id'] for c in defects if c['ai_detected'] is not None}),
        'ai_false_positive_rate': confusion['fp'] / len(healthy_scored) if healthy_scored else None,
        'ai_false_positive_wilson95': wilson(confusion['fp'], len(healthy_scored)),
        'ai_precision': confusion['tp'] / (confusion['tp'] + confusion['fp']) if confusion['tp'] + confusion['fp'] else None,
        'ai_recall_evaluated': confusion['tp'] / (confusion['tp'] + confusion['fn']) if confusion['tp'] + confusion['fn'] else None,
        'conventional_false_positives': sum(c['conventional_detected'] for c in healthy),
        'mean_confidence': mean(confidences) if confidences else None,
        'brier_score': mean(briers) if briers else None, 'threshold': THRESHOLD,
        'mean_request_latency_ms': mean(latency) if latency else None,
        'cost_usd_reported': reported_cost, 'estimated_million_cost_usd': million_cost,
        'confusion': confusion, 'http_requests': len(requests),
    }
    return {
        'run_id': run_id, 'created_at': created_at, 'seed': suite.get('seed'),
        'provider': provider, 'models': sorted(models), 'summary': summary,
        'cases': cases, 'build': suite.get('build'), 'requirements': suite['requirements'],
        'static_analysis': {'status': 'not_run', 'detected': None},
        'requests': requests, 'errors': oracle.get('errors', []),
        'notes': [
            '30種類の既知変異と対応する正常版の探索用コーパス。未知バグへの汎化性能ではありません。',
            '要求仕様・初期条件・連続トレースのみ送信。正解、変異名、従来検査の結果、Cソースは送信しません。',
            'Q1のSUSPICIOUS+FAULTの確率質量が0.5以上で検出。閾値は実行前固定。未採点・通信失敗は判定保留です。',
            'confidenceはモデルが返す確信度であり、実測正解率ではありません。Q1〜Q5の確率を独立と仮定して乗算しません。',
            '検出数はバグ種類単位、誤検出率は採点済み正常ウィンドウ単位。保留数と母数を併記します。',
            'ISR競合・volatile欠落は決定的な介入モデル。実機の競合やコンパイラ最適化を再現・証明したものではありません。',
            '静的解析の警告数をバグ検出数に換算しません。安全性の証明や実機への接続は行いません。',
            'APIの実際のmodelを保存します。classifier.devの代替モデルによる応答はJev固有の成績と区別が必要です。',
            '費用未報告は不明。classifier.devの内部コストを利用者の料金として扱いません。100万件は未実行です。',
        ],
    }
