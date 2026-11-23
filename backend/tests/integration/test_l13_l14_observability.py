"""Opt-in acceptance against isolated real Compose services and Phoenix."""
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(os.getenv("RUN_L13_L14_INTEGRATION") != "true", reason="requires Docker Compose and Phoenix client")
def test_l13_l14_real_worker_traces_and_audit_survive_collector_loss(tmp_path):
    from phoenix.client import Client

    def port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    env_file = tmp_path / '.env'
    values = {'JWT_SECRET_KEY': secrets.token_hex(32), 'APP_ENCRYPTION_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), 'INIT_ADMIN_EMAIL': 'l13@example.invalid', 'INIT_ADMIN_PASSWORD': secrets.token_hex(24), 'POSTGRES_PASSWORD': secrets.token_hex(24), 'MINIO_SECRET_KEY': secrets.token_hex(24), 'APP_ENV_FILE': str(env_file), 'API_PORT': str(port()), 'MINIO_PORT': str(port()), 'MINIO_CONSOLE_PORT': str(port()), 'PHOENIX_ENABLED': 'true', 'OTEL_SERVICE_NAME': 'l13-l14-test'}
    env_file.write_text('\n'.join(f'{k}={v}' for k, v in values.items()) + '\n')
    phoenix_port = port()
    override = tmp_path / 'override.yml'
    model_script = """from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
 def do_POST(self):
  payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if payload['model'] == 'controlled-error':
   body = b'{\"choices\":[{\"message\":{\"content\":\"invalid json\"}}]}'
   self.send_response(200)
   self.send_header('Content-Type', 'application/json')
   self.send_header('Content-Length', str(len(body)))
   self.end_headers()
   self.wfile.write(body)
   return
  content = json.dumps({'is_same_entity': True, 'confidence': 0.9, 'reason': 'controlled response'})
  body = json.dumps({'choices': [{'message': {'content': content}}]}).encode()
  self.send_response(200)
  self.send_header('Content-Type', 'application/json')
  self.send_header('Content-Length', str(len(body)))
  self.end_headers()
  self.wfile.write(body)
HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""
    override.write_text('services:\n  phoenix:\n    ports: !override\n      - "127.0.0.1:' + str(phoenix_port) + ':6006"\n  model:\n    image: python:3.12-slim\n    command: ' + json.dumps(['python', '-c', model_script]) + '\n')
    command = ['docker', 'compose', '--project-name', f'osu-l13-{secrets.token_hex(5)}', '--env-file', str(env_file), '-f', str(ROOT / 'docker-compose.yml'), '-f', str(override), '--profile', 'observability']
    def compose(*args):
        result = subprocess.run(command + list(args), cwd=ROOT, capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, (result.stdout + result.stderr)[-6000:]
        return result.stdout
    api = f"http://127.0.0.1:{values['API_PORT']}"
    def request(path, token=None, payload=None, method=None, key=None):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        if payload is not None: headers['Content-Type'] = 'application/json'
        if key: headers['Idempotency-Key'] = key
        req = Request(api + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
        with urlopen(req, timeout=10) as response: return json.load(response)
    def await_run(base, token, key):
        run_id = request(base + '/runs', token, {'scheme_key': 'customer', 'input_version': 'observability-v1'}, 'POST', key)['run_id']
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            run = request(base + '/runs/' + run_id, token)
            if run['status'] in {'SUCCEEDED', 'FAILED'}: break
            time.sleep(.5)
        assert run['status'] == 'SUCCEEDED', run
        return run
    try:
        compose('up', '-d', '--build', 'postgres', 'redis', 'minio', 'phoenix', 'model', 'backend', 'worker')
        deadline = time.monotonic() + 180
        while True:
            try:
                request('/health')
                with urlopen(f'http://127.0.0.1:{phoenix_port}/', timeout=3): pass
                break
            except (OSError, URLError):
                assert time.monotonic() < deadline, 'service startup timed out'
                time.sleep(1)
        token = request('/api/v1/auth/login', payload={'email': values['INIT_ADMIN_EMAIL'], 'password': values['INIT_ADMIN_PASSWORD']}, method='POST')['access_token']
        project = request('/api/v1/projects', token)[0]['id']
        base = f'/api/v1/projects/{project}/unification'
        provider = request('/api/v1/providers', token, {'provider_key': 'controlled', 'name': 'controlled', 'base_url': 'http://model:8080/v1', 'default_model': 'controlled-model', 'api_key': 'test-only'}, 'POST')
        fixture = ROOT / 'tests/golden/v1/customer'
        scheme = json.loads((fixture / 'scheme.json').read_text())
        scheme.update(model_judgment_enabled=True, model_provider_id=provider['id'])
        request(base + '/schemes/customer', token, scheme, 'PUT')
        records = [{k: row[k] for k in ('source_system', 'dataset', 'source_record_id', 'attributes')} for row in json.loads((fixture / 'records.json').read_text()) if row['record_ref'] in {'customer:erp:04', 'customer:secondary:04'}]
        request(base + '/batches', token, {'input_version': 'observability-v1', 'records': records}, 'POST')
        run = await_run(base, token, 'trace-first')
        assert run['trace_id'], run
        recommendation = run['result']['pairs'][0]['recommendation']
        assert recommendation['usage'] is None and recommendation['cost_usd'] is None
        client = Client(base_url=f'http://127.0.0.1:{phoenix_port}')
        deadline = time.monotonic() + 30
        trace = None
        while time.monotonic() < deadline:
            traces = client.traces.get_traces(project_identifier='l13-l14-test', include_spans=True, limit=100)
            trace = next((x for x in traces if x['trace_id'] == run['trace_id']), None)
            if trace and any(s['name'] == 'unification.model_judge' for s in trace['spans']): break
            time.sleep(.5)
        assert trace is not None
        spans = {s['name']: s for s in trace['spans']}
        assert {'unification.api_submit', 'unification.run', 'unification.read_input', 'unification.standardize_and_match', 'unification.candidate_generation', 'unification.pair_rules', 'unification.write_result', 'unification.model_judge'} <= spans.keys(), spans.keys()
        assert spans['unification.run']['parent_id'] == spans['unification.api_submit']['span_id']
        model = next(s for s in client.spans.get_spans(project_identifier='l13-l14-test', trace_ids=[run['trace_id']]) if s['name'] == 'unification.model_judge')
        attrs = model['attributes']
        for key, value in {'project_id': project, 'run_id': run['run_id'], 'input_version': 'observability-v1', 'scheme_revision': run['scheme_revision'], 'prompt_version': scheme.get('prompt_version', 'entity-pair-v1')}.items():
            assert attrs[key] == value
        assert 'input.value' in attrs and 'output.value' in attrs and 'llm.request.duration_ms' in attrs
        reviews = request(base + '/reviews?run_id=' + run['run_id'], token)['items']
        pair = reviews[0]
        decision = request(base + '/reviews', token, {'run_id': run['run_id'], 'left_source_revision_id': pair['left_source_revision_id'], 'right_source_revision_id': pair['right_source_revision_id'], 'action': 'KEEP_DISTINCT', 'reason': 'L14 durable audit', 'expected_revision': 1}, 'POST')
        audit = request(base + '/identity-changes', token)['items']
        assert any(x['change_id'] == decision['change_id'] for x in audit)
        decision_trace = next(x['trace_id'] for x in audit if x['change_id'] == decision['change_id'])
        assert decision_trace
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            decision_spans = client.spans.get_spans(project_identifier='l13-l14-test', trace_ids=[decision_trace])
            if any(x['name'] == 'unification.api_review_decision' for x in decision_spans):
                break
            time.sleep(.5)
        assert any(x['name'] == 'unification.api_review_decision' for x in decision_spans)
        error_provider = request('/api/v1/providers', token, {'provider_key': 'controlled', 'name': 'controlled-error', 'base_url': 'http://model:8080/v1', 'default_model': 'controlled-error', 'api_key': 'test-only'}, 'POST')
        error_scheme = dict(scheme, model_provider_id=error_provider['id'], model_failure_policy='REVIEW')
        request(base + '/schemes/customer', token, error_scheme, 'PUT')
        error_run = await_run(base, token, 'model-error')
        assert 'invalid_json' in error_run['result']['pairs'][0]['model_error']
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            errors = client.spans.get_spans(project_identifier='l13-l14-test', trace_ids=[error_run['trace_id']])
            model_errors = [x for x in errors if x['name'] == 'unification.model_judge']
            if model_errors:
                break
            time.sleep(.5)
        assert model_errors and model_errors[0]['status_code'] == 'ERROR'
        assert model_errors[0]['attributes']['error.code'] == 'invalid_json'
        # Delete the Phoenix project and query its newly-created replacement.
        client.projects.delete(project_name='l13-l14-test')
        client.projects.create(name='l13-l14-test')
        assert not client.spans.get_spans(project_identifier='l13-l14-test', trace_ids=[decision_trace])
        assert request(base + '/identity-changes', token)['items'] == audit
        compose('stop', 'phoenix')
        baseline_logs = compose('logs', 'worker', 'backend')
        baseline_failures = baseline_logs.count('trace export failed') + baseline_logs.count('trace exporter reported failure')
        await_run(base, token, 'collector-down')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            logs = compose('logs', 'worker', 'backend')
            if logs.count('trace export failed') + logs.count('trace exporter reported failure') > baseline_failures: break
            time.sleep(.5)
        assert logs.count('trace export failed') + logs.count('trace exporter reported failure') > baseline_failures
        assert request(base + '/identity-changes', token)['items'] == audit
        values['PHOENIX_ENABLED'] = 'false'
        env_file.write_text('\n'.join(f'{k}={v}' for k, v in values.items()) + '\n')
        compose('up', '-d', '--no-deps', 'backend', 'worker')
        deadline = time.monotonic() + 60
        while True:
            try:
                request('/health')
                break
            except (OSError, URLError):
                assert time.monotonic() < deadline, 'API restart timed out'
                time.sleep(1)
        off_run = await_run(base, token, 'tracing-off')
        assert off_run['trace_id'] is None
        assert request(base + '/identity-changes', token)['items'] == audit
    finally:
        compose('down', '--volumes', '--remove-orphans')
