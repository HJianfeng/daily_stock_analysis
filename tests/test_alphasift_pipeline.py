import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('pipeline_selector', Path(__file__).resolve().parents[1] / 'scripts/select_alphasift_candidates.py')
selector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selector)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.track = Path(self.temp.name)
        (self.track / 'data/logs').mkdir(parents=True)
        (self.track / 'data/runs').mkdir()
        self.date = '2026-11-27'
        self.report = dict(trade_date=self.date, pack_trade_date=self.date,
                           expected_trade_date=self.date, pack_is_stale=False,
                           ai_brief={'mode': 'llm'})
        self.log = {'effective_dates': {'cn': self.date}, 'entries': [], 'ai_entries': []}
        self.add('one', [self.pick('600600'), self.pick('000100', 99, ['rsi_overbought']), self.pick('600941', 90)])
        self.add('two', [self.pick('600600', 70), self.pick('603345', 85)])
        # An empty rule strategy does not need an AI result.
        self.log['entries'].append({'market': 'cn', 'strategy': 'empty', 'status': 'ok', 'picks': 0})

    def pick(self, code, score=80, flags=None):
        return {'code': code, 'name': code, 'final_score': score,
                'llm_score': score, 'risk_flags': flags or []}

    def add(self, strategy, picks):
        self.log['entries'].append({'market': 'cn', 'strategy': strategy, 'status': 'ok', 'picks': len(picks)})
        self.log['ai_entries'].append({'market': 'cn', 'strategy': strategy, 'status': 'ok', 'run_id': strategy})
        self.write_run(strategy, picks)

    def write_run(self, strategy, picks, **extra):
        (self.track / f'data/runs/{strategy}.json').write_text(json.dumps({
            'market': 'cn', 'track': 'ai', 'strategy': strategy,
            'llm_ranked': True, 'picks': picks, **extra,
        }))

    def select(self):
        (self.track / f'data/logs/{self.date}-asia.json').write_text(json.dumps(self.log))
        return selector.select_candidates(self.track, self.date, self.report)

    def test_consensus_and_risk_exclusion(self):
        self.assertEqual([p['code'] for p in self.select()], ['600600', '600941', '603345'])

    def test_missing_ai_stops_pipeline(self):
        self.log['ai_entries'].pop()
        with self.assertRaises(ValueError):
            self.select()

    def test_degraded_ai_stops_pipeline(self):
        self.log['ai_entries'][0]['status'] = 'skipped_llm_degraded'
        with self.assertRaises(ValueError):
            self.select()

    def test_rule_failure_stops_pipeline(self):
        self.log['entries'][0]['status'] = 'failed'
        with self.assertRaises(ValueError):
            self.select()

    def test_unranked_run_stops_pipeline(self):
        self.write_run('one', [], llm_ranked=False)
        with self.assertRaises(ValueError):
            self.select()

    def test_unreviewed_tail_is_excluded(self):
        pick = self.pick('600001', 100)
        pick['llm_score'] = None
        self.write_run('one', [pick])
        self.assertNotIn('600001', [p['code'] for p in self.select()])

    def test_announcement_risk_is_excluded(self):
        self.report['announcement_risks'] = {'by_code': {'600600': {'negative_event_flags': ['risk']}}}
        self.assertNotIn('600600', [p['code'] for p in self.select()])

    def test_stale_pack_stops_pipeline(self):
        self.report['pack_trade_date'] = '2026-11-26'
        with self.assertRaises(ValueError):
            self.select()

    def test_stale_snapshot_stops_pipeline(self):
        self.log['effective_dates']['cn'] = '2026-11-26'
        with self.assertRaises(ValueError):
            self.select()

    def test_rule_only_brief_stops_pipeline(self):
        self.report['ai_brief']['mode'] = 'rules'
        with self.assertRaises(ValueError):
            self.select()

    def test_limit_and_determinism(self):
        self.write_run('one', [self.pick(f'60000{i}') for i in range(8)])
        self.write_run('two', [])
        self.assertEqual([p['code'] for p in self.select()], [f'60000{i}' for i in range(5)])

    def test_push_result_contract(self):
        self.assertTrue(selector.push_succeeded({'status': 'ok', 'sent': ['feishu'], 'errors': []}))
        self.assertTrue(selector.push_succeeded({'status': 'skipped_duplicate', 'skipped': [{'channel': 'feishu', 'reason': 'duplicate_same_trade_date'}]}))
        for result in [dict(status='ok', sent=[]), dict(status='skipped_no_channels'),
                       dict(status='error'), dict(status='partial', errors=['error']),
                       dict(status='skipped_duplicate', skipped=[{'channel': 'feishu', 'reason': 'in_progress_concurrent'}])]:
            with self.subTest(result=result):
                self.assertFalse(selector.push_succeeded(result))


if __name__ == '__main__':
    unittest.main()
