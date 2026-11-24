"""Test oracle integrity and scorer rejection, not application correctness."""
import copy
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('golden', Path(__file__).resolve().parents[1] / 'scripts/golden.py')
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)


def oracle_predictions():
    result = {'golden_version': '1.0.0', 'run_id': 'scorer-self-test', 'scheme_revision': '1', 'domains': {}}
    for domain in golden.read(golden.GOLDEN / 'manifest.json')['domains']:
        cases = golden.read(golden.GOLDEN / domain / 'expected.json')
        assignments = {r['record_ref']: r['record_ref'] for r in golden.read(golden.GOLDEN / domain / 'records.json')}
        for c in cases:
            if c['expected_action'] == 'AUTO_MATCH':
                assignments[c['right']] = assignments[c['left']]
        result['domains'][domain] = {'assignments': assignments, 'pairs': [dict(case_id=c['case_id'], candidate=c['required_candidate'], action=c['expected_action']) for c in cases]}
    return result


class GoldenContractTest(unittest.TestCase):
    def test_fixture_integrity(self):
        self.assertEqual(golden.validate(), {'records': 60, 'pairs': 30, 'lifecycle': 22})

    def test_oracle_output_only_proves_scorer_contract(self):
        self.assertTrue(golden.evaluate(oracle_predictions())['passed'])
        self.assertEqual(golden.evaluate(oracle_predictions())['product_release_status'], 'NOT_EVALUATED')

    def test_all_review_and_all_singletons_cannot_pass(self):
        predictions = oracle_predictions()
        for output in predictions['domains'].values():
            output['assignments'] = {ref: ref for ref in output['assignments']}
            for pair in output['pairs']:
                pair['action'] = 'REVIEW'
        self.assertFalse(golden.evaluate(predictions)['passed'])

    def test_cluster_transitivity_cannot_hide_unsafe_merge(self):
        predictions = oracle_predictions()
        predictions['domains']['product']['assignments'] = dict.fromkeys(predictions['domains']['product']['assignments'], 'one-entity')
        report = golden.evaluate(predictions)
        self.assertFalse(report['passed'])
        self.assertGreater(report['domains']['product']['false_merges'], 0)
        self.assertGreater(report['domains']['product']['unauthorized_cluster_links'], 0)

    def test_missing_candidates_cannot_pass(self):
        predictions = oracle_predictions()
        for output in predictions['domains'].values():
            for pair in output['pairs']:
                pair['candidate'] = False
        self.assertFalse(golden.evaluate(predictions)['passed'])

    def test_incomplete_or_duplicate_predictions_are_errors(self):
        for change in ('missing_pair', 'duplicate_pair', 'missing_assignment', 'invalid_type'):
            predictions = copy.deepcopy(oracle_predictions())
            output = predictions['domains']['supplier']
            if change == 'missing_pair':
                output['pairs'].pop()
            elif change == 'duplicate_pair':
                output['pairs'].append(output['pairs'][0])
            elif change == 'missing_assignment':
                output['assignments'].pop(next(iter(output['assignments'])))
            else:
                output['pairs'][0]['candidate'] = 'false'
            with self.assertRaises(ValueError):
                golden.evaluate(predictions)
