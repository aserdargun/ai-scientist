"""Independent review candidates; source bytes are only executed inside Docker."""
from __future__ import annotations
from dataclasses import dataclass
import ast
import hashlib
import json
from pathlib import Path

BASE = '''import numpy as np
import pandas as pd
from harness.contracts import AlarmPolicy

class Candidate:
    def fit(self, train, ctx):
        self.center = train.median()
        self.scale = (train - self.center).abs().median().replace(0.0, 1.0)
        self.calls = 0
        {fit_extra}

    def score(self, data):
        {score_body}

    def alarm_policy(self, train_scores):
        return AlarmPolicy(threshold=float(np.quantile(train_scores, 0.99)),
                           release=float(np.quantile(train_scores, 0.95)), dwell=2)

def build_candidate():
    return Candidate()
{extra}
'''
CAUSAL = 'z = ((data - self.center) / self.scale).abs().mean(axis=1)\n        return z.rolling(6, min_periods=1).mean().to_numpy(dtype=float)'

@dataclass(frozen=True)
class ReviewCase:
    name: str
    source: bytes
    expected_rejection: tuple[str,...]
    note: str
    nrm_only: bool = False


def source(*, fit_extra='pass', score_body=CAUSAL, extra=''):
    return BASE.format(fit_extra=fit_extra,score_body=score_body,extra=extra).encode()


def cases() -> tuple[ReviewCase,...]:
    return (
        ReviewCase('causal_clean',source(),(), 'Clean train-derived robust normalization and trailing rolling mean.'),
        ReviewCase('causal_small_scale',source(score_body=CAUSAL.replace('return z.', 'return 1e-12 * z.')),(),
                   'Nonconstant finite causal scores at small scale; a fixed variance cutoff must not silently define constant.'),
        ReviewCase('stateful_score_fresh_restore',source(score_body='self.calls += 1\n        '+CAUSAL.replace('return z.', 'return self.calls + z.')),(),
                   'Within-instance mutation is isolated by restoring the same frozen fit artifact for each score call.'),
        ReviewCase('seeded_random_fit',source(fit_extra='self.weight = float(np.random.default_rng(ctx.seed).uniform(0.5, 1.5))',
                   score_body=CAUSAL.replace('return z.', 'return self.weight * z.')),(),
                   'Repeated full fitting with identical ctx.seed must reproduce the score vector.'),
        ReviewCase('unseeded_random_fit',source(fit_extra='self.weight = float(np.random.default_rng().uniform(0.5, 1.5))',
                   score_body=CAUSAL.replace('return z.', 'return self.weight * z.')),('determinism',),
                   'Randomness occurs only in fit; repeating score on one artifact would incorrectly pass.'),
        ReviewCase('unseeded_random_score',source(score_body=CAUSAL.replace('return z.', 'return float(np.random.default_rng().uniform(0.5, 1.5)) * z.')),('determinism','causality'),
                   'Score randomness must reject; whichever comparison is evaluated first may discover it.'),
        ReviewCase('eval_statistics',source(score_body='center = data.mean()\n        scale = data.std().replace(0.0, 1.0)\n        return ((data - center) / scale).abs().mean(axis=1).to_numpy(dtype=float)'),('causality',),
                   'Evaluation-global mean/std changes earlier scores after a future perturbation.'),
        ReviewCase('eval_statistics_small_scale',source(score_body='center = data.mean()\n        scale = data.std().replace(0.0, 1.0)\n        return 1e-12 * ((data - center) / scale).abs().mean(axis=1).to_numpy(dtype=float)'),('causality',),
                   'Scaling the same leaky detector must not bypass a relative causality comparison.'),
        ReviewCase('centered_rolling',source(score_body=CAUSAL.replace('rolling(6, min_periods=1)', 'rolling(9, min_periods=1, center=True)')),('causality',),
                   'Centered rolling uses future rows even though normalization comes only from train.'),
        ReviewCase('constant_score',source(score_body='return np.ones(len(data), dtype=float)'),('degenerate',),
                   'Constant finite vectors must be rejected independently of candidate claims.'),
        ReviewCase('nan_score',source(score_body='result = np.arange(len(data), dtype=float)\n        result[-1] = np.nan\n        return result'),('degenerate','interface'),
                   'Nonfinite output must be rejected before any independent metric commit.'),
        ReviewCase('infinite_score',source(score_body='result = np.arange(len(data), dtype=float)\n        result[-1] = np.inf\n        return result'),('degenerate','interface'),
                   'Infinity must not turn into a valid JSON score or KEEP candidate.'),
        ReviewCase('wrong_length',source(score_body='return np.arange(max(0, len(data)-1), dtype=float)'),('degenerate','interface'),
                   'One score per row is mandatory; missing output rows cannot be padded.'),
        ReviewCase('time_ramp',source(score_body='return np.arange(len(data), dtype=float)'),('position_bias',),
                   'Perfect monotone timing score must be rejected on a suite with eligible NRM tasks.', True),
        ReviewCase('hardcoded_task_identity',source(extra='TASK_MEMORY = "private-review-task-id"'),('hardcoding',),
                   'Literal task identity is forbidden even when the constant is currently unused.'),
        ReviewCase('hardcoded_eval_instant',source(extra='TIME_MEMORY = "2026-09-24T13:15:00+03:00"'),('hardcoding',),
                   '13:15 +03:00 is inside the trusted eval interval [10:00Z,11:00Z).'),
        ReviewCase('hardcoded_64_floats',source(extra='MEMORIZED = ['+', '.join(str(float(i)) for i in range(64))+']'),('hardcoding',),
                   'Explicit 64-element numeric literal array exceeds the >=50 static threshold.'),
        ReviewCase('hardcoded_64_negative_floats',source(extra='MEMORIZED = ['+', '.join(str(-float(i)) for i in range(64))+']'),('hardcoding',),
                   'Negative numeric literals are unary AST nodes but still form a forbidden literal list.'),
        ReviewCase('slow_fit',source(fit_extra='import time\n        time.sleep(5)'),('timeout',),
                   'Use a one-second fit budget; the evaluation deadline must encompass all phase/guard work.'),
    )


def manifest():
    records=[]
    for case in cases():
        ast.parse(case.source,filename=case.name+'.py')
        records.append({'name':case.name,'source_sha256':hashlib.sha256(case.source).hexdigest(),
                        'bytes':len(case.source),'expected_rejection':list(case.expected_rejection),
                        'note':case.note,'nrm_only':case.nrm_only})
    return {'schema':'guard-review-candidate-catalog.v1',
            'scope':'Review inputs only; AST syntax parsing is not a guard/sandbox execution result.',
            'entrypoint':'build_candidate() -> ADPipeline',
            'private_identifiers':['private-review-task-id'],
            'eval_interval_utc':['2026-09-24T10:00:00Z','2026-09-24T11:00:00Z'],
            'cases':records,'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

if __name__=='__main__':
    data=manifest()
    Path(__file__).with_name('guard-review-candidate-catalog.json').write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps({'syntax_checked_cases':len(data['cases']),'executed_candidates':0}))
