#!/usr/bin/env python3
"""Validate fixtures and score externally produced predictions. Never runs the app."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests/golden/v1'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def validate(root=GOLDEN):
    manifest = read(root / 'manifest.json')
    actual = {str(p.relative_to(root)) for p in root.rglob('*.json') if p.name != 'manifest.json'}
    if actual != set(manifest['files']):
        raise ValueError('manifest file list differs from fixture files')
    for name, digest in manifest['files'].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'checksum mismatch: {name}')
    counts = {'records': 0, 'pairs': 0, 'lifecycle': 0}
    case_ids = set()
    for domain in manifest['domains']:
        records = read(root / domain / 'records.json')
        refs = {r['record_ref'] for r in records}
        keys = {(r['source_system'], r['dataset'], r['source_record_id']) for r in records}
        if len(refs) != len(records) or len(keys) != len(records):
            raise ValueError(f'duplicate source identity: {domain}')
        scheme = read(root / domain / 'scheme.json')
        if scheme['entity_type'] != domain or not scheme['nulls_never_equal']:
            raise ValueError(f'invalid scheme: {domain}')
        pairs = set()
        for case in read(root / domain / 'expected.json'):
            key = frozenset((case['left'], case['right']))
            if len(key) != 2 or not key <= refs or key in pairs or case['case_id'] in case_ids:
                raise ValueError(f'duplicate or invalid case: {case}')
            pairs.add(key)
            case_ids.add(case['case_id'])
            if case['truth'] not in {'SAME', 'DIFFERENT', 'UNRESOLVED'}:
                raise ValueError('invalid truth')
            if case['expected_action'] not in {'AUTO_MATCH', 'DISTINCT', 'REVIEW'}:
                raise ValueError('invalid action')
            if case['expected_action'] == 'AUTO_MATCH' and (case['truth'] != 'SAME' or not case['required_candidate']):
                raise ValueError('unsafe automatic match oracle')
        counts['records'] += len(records)
        counts['pairs'] += len(pairs)
    lifecycle = read(root / 'lifecycle.json')
    if len({c['case_id'] for c in lifecycle}) != len(lifecycle):
        raise ValueError('duplicate lifecycle case')
    baseline_text = (ROOT / 'docs/reference/acceptance-baseline.md').read_text()
    for case in lifecycle:
        if case['scenario'] not in manifest['domains'] or not case['assertions']:
            raise ValueError('invalid lifecycle case')
        for gate in case['baseline_ids']:
            if f'| {gate} |' not in baseline_text:
                raise ValueError(f'unknown acceptance gate: {gate}')
    counts['lifecycle'] = len(lifecycle)
    for key, manifest_key in [('records', 'record_count'), ('pairs', 'pair_case_count'), ('lifecycle', 'lifecycle_case_count')]:
        if counts[key] != manifest[manifest_key]:
            raise ValueError(f'incorrect count: {key}')
    return counts


def evaluate(predictions, root=GOLDEN):
    validate(root)
    manifest = read(root / 'manifest.json')
    if predictions.get('golden_version') != manifest['version']:
        raise ValueError('prediction golden_version mismatch')
    if not predictions.get('run_id') or not predictions.get('scheme_revision'):
        raise ValueError('run_id and scheme_revision required')
    if set(predictions['domains']) != set(manifest['domains']):
        raise ValueError('all and only golden domains required')
    reports = {}
    for domain in manifest['domains']:
        cases = read(root / domain / 'expected.json')
        records = read(root / domain / 'records.json')
        refs = {r['record_ref'] for r in records}
        output = predictions['domains'][domain]
        assignments = output['assignments']
        if set(assignments) != refs or any(not isinstance(v, str) or not v.strip() for v in assignments.values()):
            raise ValueError(f'{domain}: complete nonempty entity assignments required')
        rows = output['pairs']
        by_id = {p['case_id']: p for p in rows}
        if len(by_id) != len(rows) or set(by_id) != {c['case_id'] for c in cases}:
            raise ValueError(f'{domain}: missing, duplicate or extra pair predictions')
        for p in rows:
            if type(p['candidate']) is not bool or p['action'] not in {'AUTO_MATCH', 'DISTINCT', 'REVIEW'}:
                raise ValueError('invalid prediction action or candidate flag')
        correct = required = recalled = false_merges = known_same = missed_same = reviews = 0
        allowed_links = set()
        for c in cases:
            p = by_id[c['case_id']]
            merged = assignments[c['left']] == assignments[c['right']]
            correct += p['action'] == c['expected_action']
            reviews += p['action'] == 'REVIEW'
            required += c['required_candidate']
            recalled += c['required_candidate'] and p['candidate']
            false_merges += c['truth'] == 'DIFFERENT' and merged
            known_same += c['truth'] == 'SAME'
            missed_same += c['truth'] == 'SAME' and not merged
            if c['expected_action'] == 'AUTO_MATCH':
                allowed_links.add(frozenset((c['left'], c['right'])))
        unauthorized = 0
        ordered = sorted(refs)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                if assignments[left] == assignments[right] and frozenset((left, right)) not in allowed_links:
                    unauthorized += 1
        required_merges = sum(assignments[c['left']] == assignments[c['right']] for c in cases if c['expected_action'] == 'AUTO_MATCH')
        expected_merges = len(allowed_links)
        passed = (correct == len(cases) and recalled == required and false_merges == 0 and unauthorized == 0 and required_merges == expected_merges)
        reports[domain] = dict(passed=passed,policy_correct=correct,pair_count=len(cases),required_candidates=required,recalled_candidates=recalled,false_merges=false_merges,unauthorized_cluster_links=unauthorized,required_merges=expected_merges,completed_required_merges=required_merges,review_count=reviews,known_same_count=known_same,unmerged_known_same_count=missed_same)
    return dict(golden_version=manifest['version'],passed=all(x['passed'] for x in reports.values()),domains=reports,lifecycle_status='NOT_EVALUATED',product_release_status='NOT_EVALUATED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'evaluate'])
    parser.add_argument('predictions', nargs='?', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'validate':
            report = {'fixture_integrity': 'PASS', **validate(), 'product_status': 'NOT_EVALUATED'}
        else:
            if args.predictions is None:
                parser.error('evaluate requires a predictions JSON file')
            report = evaluate(read(args.predictions))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get('passed', True) else 1
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
