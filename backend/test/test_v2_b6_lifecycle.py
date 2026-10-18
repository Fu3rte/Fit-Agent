import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app import model_config
from app.agent import cache_observation, telemetry
from app.ai.messages import SystemMessage
from app.application.session import service as session_service
from app.domain.session.models import SendCommand, SendRequest
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces import http as http_interface
from app.interfaces.http_api.routes import agent as agent_routes
from test import run_v2_eval as runner
from test.check_save_confirmation_benchmark import (
    GROUP_PROTOCOL,
    SuiteRoot,
    run_benchmark,
)
from test.p0_business_cases import _build
from test.regression_support import Server, client, run_tool
from test.v2_attempts import claim_slot, create_attempt, read_record, write_record
from test.v2_budget import DEFAULT_LIMITS, BudgetExhausted, BudgetLedger, StopTriggered
from test.v2_business_cases import _as_p0_case
from test.v2_eval_support import (
    BACKEND,
    _call,
    build_manifest,
    environment_preflight,
    evaluation_roots,
    experiment_env,
    freeze_model_config,
    load_cases,
    protected_baseline,
    run_case_round,
    sha256_file,
)
from test.v2_test_paths import OUTPUT_ROOT

ROOT = OUTPUT_ROOT
CASES = BACKEND / 'test/v2_cases.json'


@pytest.fixture
def tmp_path():
    directory = ROOT / 'pytest-r2/b6' / uuid4().hex[:12] / 'case'
    directory.mkdir(parents=True)
    original = model_config.DATA_ROOT
    try:
        yield directory
    finally:
        model_config.DATA_ROOT = original


@pytest.fixture
def configured(tmp_path):
    original = model_config.DATA_ROOT
    try:
        identity = freeze_model_config(BACKEND / 'data', tmp_path / 'config-source')
        yield identity
    finally:
        model_config.DATA_ROOT = original


def local_experiment(tmp_path):
    runner.load_backend_env()
    root = tmp_path / 'parent'
    root.mkdir()
    original = model_config.DATA_ROOT
    try:
        identity = freeze_model_config(BACKEND / 'data', root)
    finally:
        model_config.DATA_ROOT = original
    manifest = build_manifest(BACKEND.parent, root.name, CASES, identity, '2026-01-01', 1)
    manifest.update(slot_plan=runner.slot_plan(load_cases(CASES)), planned_slots=150,
                    execution_identity=runner.current_identity(CASES, identity),
                    budget_ledger_path=str(tmp_path / 'budget-ledger.json'),
                    protected_baseline_before=protected_baseline(BACKEND.parent, snapshot_root=tmp_path / 'snapshots'))
    write_record(root / 'manifest.json', manifest)
    write_record(manifest['budget_ledger_path'], {'limits': DEFAULT_LIMITS, 'models': {},
                                                'experiments': {}, 'completion_seq': 0})
    return root, manifest


def recovery_args(root):
    return runner.parse_args(['--experiment-root', str(root.parent), '--experiment-id', root.name,
                              '--benchmarks-only', '--recover-benchmark', '--benchmark-topic',
                              'tool_parallel', '--reason', '隔离本地工具恢复，无供应商请求'])


def child_environment():
    environment = dict(os.environ, FIT_AGENT_B6_TEST_INSTANCE=uuid4().hex)
    environment.pop('FIT_AGENT_B6_TEST_HOOK', None)
    environment.pop('FIT_AGENT_B6_TEST_ROOT', None)
    environment['PYTHONPATH'] = os.pathsep.join((str(BACKEND / 'test'), str(BACKEND),
                                                 environment.get('PYTHONPATH', '')))
    environment['FIT_AGENT_B6_TEST_OUTPUT_ROOT'] = str(ROOT)
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        environment.pop(key, None)
    return environment


def run_driver(root, mode, kind, *, child_fault=False):
    environment = child_environment()
    if child_fault:
        environment.update(FIT_AGENT_B6_TEST_HOOK='1', FIT_AGENT_B6_TEST_ROOT=str(root))
    completed = subprocess.run([sys.executable, str(BACKEND / 'test/lifecycle_driver_r2.py'), mode, str(root), kind],
                               cwd=BACKEND, env=environment, capture_output=True, timeout=180)
    (root / 'subprocess.log').write_bytes(completed.stdout + completed.stderr)
    write_record(root / 'subprocess-receipt.json', {'exit_code': completed.returncode,
                                                  'instance': environment['FIT_AGENT_B6_TEST_INSTANCE']})
    return completed, read_record(root / 'driver-result.json')


def assert_no_provider_usage(manifest):
    assert read_record(manifest['budget_ledger_path'])['models'] == {}


@pytest.mark.parametrize('exit_kind', ['normal', 'error', 'cancel'])
def test_nested_roots_real_attachment_tool_observation_restoration(tmp_path, configured, exit_kind):
    from app.ai.model_capabilities import resolve_model_spec

    before = (session_service.PROJECT_ROOT, agent_routes.create_file_tools,
              database_module.default_database_path, telemetry.trace_database_path,
              cache_observation.observation_directory)
    root = SuiteRoot(tmp_path, 'protocol')
    try:
        with root.bind(), Server(http_interface.app) as server, client(server.base_url) as http:
            service = http_interface.app.state.session_service
            session = str(uuid4())
            _call(service.create_session(session, '本地附件协议'))
            attachment = str(uuid4())
            text = '真实协议附件\n'
            outcome = _call(service.accept_send(SendCommand(
                session_id=session, operation_id=str(uuid4()),
                request=SendRequest(text='读取附件', attachments=[{
                    'kind': 'upload', 'attachment_id': attachment, 'file_name': 'plan.md',
                    'data_base64': base64.b64encode(text.encode()).decode(),
                }]),
            ), system_message=SystemMessage(role='system', content='本地协议', tools_added=[], timestamp=100)))
            metadata, content = _call(service.attachments.get_attachment_content(session, attachment))
            assert content == text
            assert http.get(f'/api/sessions/{session}/attachments/{attachment}').json()['text'] == text
            tools = agent_routes.create_file_tools(session)
            relative = metadata.storage_ref.removeprefix('tmp/')
            assert text.strip() in run_tool(tools['read'], {'path': relative}).content[0].text
            assert run_tool(tools['read'], {'path': f'sessions/{uuid4()}/attachments/x.md'}).is_error
            spec = resolve_model_spec(model_config.load_model_config())
            projection = {'messages': [{'role': 'user', 'content': '本地观测API回归'}]}
            observer = cache_observation.CacheObserver(session_id=session, run_id=outcome.run.id)
            handle = observer.start_call(cache_observation.NORMAL_CALL)
            handle.observe(projection, spec)
            handle.fail(aborted=True)
            observations = cache_observation.read_observations(root.base / 'observations')
            assert len(observations) == 1
            assert observations[0].session_id == session and observations[0].status == 'aborted'
            assert observations[0].input_tokens is None
            assert (root.base / 'observations' / f'{session}.jsonl').is_file()
            _call(service.runs.finish_run(session, outcome.run.id, 'completed'))
            nested = SuiteRoot(tmp_path, 'att')
            with nested.bind():
                assert session_service.PROJECT_ROOT == nested.attachment_root
                assert cache_observation.observation_directory() == nested.base / 'observations'
                nested_observer = cache_observation.CacheObserver(session_id=session, run_id=outcome.run.id)
                nested_call = nested_observer.start_call(cache_observation.NORMAL_CALL)
                nested_call.observe(projection, spec)
                nested_call.fail(aborted=True)
                assert len(cache_observation.read_observations(nested.base / 'observations')) == 1
            assert len(cache_observation.read_observations(root.base / 'observations')) == 1
            assert session_service.PROJECT_ROOT == root.attachment_root
            if exit_kind == 'error':
                raise RuntimeError('协议退出')
            if exit_kind == 'cancel':
                raise asyncio.CancelledError('协议取消')
    except (RuntimeError, asyncio.CancelledError) as error:
        assert str(error) in ('协议退出', '协议取消')
    after = (session_service.PROJECT_ROOT, agent_routes.create_file_tools,
             database_module.default_database_path, telemetry.trace_database_path,
             cache_observation.observation_directory)
    assert after == before


def test_save_confirmation_protocol_groups_real_http(tmp_path, configured):
    result = run_benchmark(str(tmp_path / 'suite'), groups=(GROUP_PROTOCOL,))
    assert result.topic == 'save_confirmation'
    assert len(result.samples) == 15
    assert all(sample['status'] == 'pass' for sample in result.samples)
    regenerate = next(sample for sample in result.samples if sample['name'] == 'regenerate_rejects_confirmation')
    assert regenerate['detail']['status_code'] == 409
    assert regenerate['response']['detail']['code'] == 'invalid_target_entry'


def test_precheck_two_real_processes_same_directory(tmp_path):
    commands = [sys.executable, str(Path(__file__).resolve()), '--probe-child', str(tmp_path)]
    processes = [subprocess.Popen(commands, cwd=BACKEND, env=dict(os.environ),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for process in processes:
        assert process.stdout.readline().strip() == 'ready'
    for process in processes:
        process.stdin.write('go\n')
        process.stdin.flush()
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stdout + stderr
    assert not list(tmp_path.glob('.write-probe*'))


def test_s06_referenced_session_constructed_in_real_sqlite(tmp_path):
    case = next(case for case in load_cases(CASES) if case.case_id == 'S06')
    declaration = _as_p0_case(case).precondition
    assert declaration['sessions'] == ['另一会话']
    with evaluation_roots(tmp_path / 'attachments', tmp_path / 'app.db', tmp_path / 'trace.db',
                          tmp_path / 'observations'):
        detail = asyncio.run(_build(declaration, tmp_path / 'app.db', '2026-01-01'))
    assert detail['sessions']['另一会话']
    assert case.precondition.get('sessions') is None


def test_precondition_failure_has_round_claim_and_original_error(tmp_path, configured):
    case = load_cases(CASES)[0].model_copy(update={'precondition': {
        'profile_versions': [{'goal': '增肌', 'experience': '新手', 'environment': '居家',
                              'availability': '每周三次', 'health_notes': None,
                              'movement_restrictions': None, 'unavailable_equipment': [],
                              'forbidden_exercise_ids': ['missing-real-catalog-id']}]}})
    root = tmp_path / 'experiment'
    root.mkdir()
    env = experiment_env('local-precondition', case, 0, 0, 0, root, configured, '2026-01-01')
    with pytest.raises(ValueError, match='动作 ID 不在动作目录'):
        run_case_round(case, 0, 0, env, configured)
    error = read_record(Path(env.evidence_dir) / 'error.json')
    result = read_record(Path(env.evidence_dir) / 'round.json')
    claim = read_record(root / 'claims' / f'{case.case_id}-r0.json')
    assert result['status'] == claim['status'] == 'error'
    assert result['error_type'] == error['error_type']
    assert error['completed_steps'] == 0


def test_finalize_without_summary_preserves_source_and_missing_terminal(tmp_path):
    root, manifest = local_experiment(tmp_path)
    (root / 'logs').mkdir()
    slot = manifest['slot_plan'][-1]
    schedule = root / 'logs/schedule-p0.jsonl'
    schedule.write_text(json.dumps({**slot, 'status': 'run_error', 'error': '隔离前置失败回执'}) + '\n', encoding='utf-8')
    originals = {path: path.read_bytes() for path in (root / 'manifest.json', Path(manifest['budget_ledger_path']), schedule)}
    args = runner.parse_args(['--experiment-root', str(tmp_path), '--experiment-id', root.name, '--finalize-only'])
    assert runner.run_finalize(args) == 0
    assert not (root / 'summary.json').exists()
    assert {path: path.read_bytes() for path in originals} == originals
    recovery = next((root / 'recovery').iterdir())
    summary = read_record(recovery / 'summary.json')
    execution = read_record(recovery / 'execution-ledger.json')
    assert summary['planned_slots'] == summary['aggregate']['executions'] == 150
    assert summary['released'] is False
    assert execution['slots'][-1]['status'] == 'run_error'
    assert execution['slots'][-1]['missing_round_evidence'] is True
    assert not list(root.glob('cases/**/round.json'))
    assert read_record(recovery / 'provenance.json')['source_identity'] == manifest['execution_identity']


def test_recovery_requires_explicit_intent_and_preserves_stop(tmp_path):
    args = runner.parse_args(['--recover-benchmark'])
    with pytest.raises(ValueError, match='专项恢复需要'):
        runner.run_benchmark_recovery(args)
    root, manifest = local_experiment(tmp_path)
    ledger = BudgetLedger(manifest['budget_ledger_path'], experiment_id=root.name)
    ledger.request_stop('production_drift', owner=root.name, trigger='local-regression')
    before = Path(manifest['budget_ledger_path']).read_bytes()
    with pytest.raises(StopTriggered):
        runner.run_benchmark_recovery(recovery_args(root))
    assert Path(manifest['budget_ledger_path']).read_bytes() == before
    assert not (root / 'benchmark-attempts').exists()


def test_release_blocks_historical_source_without_resigning(tmp_path):
    root, manifest = local_experiment(tmp_path)
    manifest['execution_identity'] = {'source_sha256': 'historical-source'}
    write_record(root / 'manifest.json', manifest)
    before = (root / 'manifest.json').read_bytes()
    result = runner.release_check(root)
    assert result['released'] is False
    assert any('冻结冒烟身份不匹配' in item for item in result['blockers'])
    assert (root / 'manifest.json').read_bytes() == before


def test_read_only_environment_precheck_records_failure(tmp_path):
    with pytest.raises(RuntimeError, match='只读环境预检失败'):
        environment_preflight(tmp_path, expected={'milvus': {}, 'volumes': {}})
    evidence = read_record(next(tmp_path.glob('environment-*.json')))
    assert evidence['blockers']
    assert set(evidence['observed']) == {'milvus', 'volumes'}


@pytest.mark.parametrize('state', ['unknown', 'sent', 'reserved'])
def test_zero_token_unsettled_attempt_blocks_recovery_and_current_release(tmp_path, state):
    root, manifest = local_experiment(tmp_path)
    ledger = BudgetLedger(manifest['budget_ledger_path'], experiment_id=root.name)
    write_record(root / 'budget-summary.json', ledger.snapshot())
    case = load_cases(CASES)[0]
    claim_slot(experiment_env(root.name, case, 0, 0, 0, root, manifest['model_identity'], '2026-01-01'))
    if state == 'reserved':
        ledger.reserve('local-state', 'zero-tokens', 0, '本地账本状态转换')
    else:
        ledger.record_attempt('local-state', 'zero-tokens', inputs=1, basis='本地账本状态转换，无网络')
        if state == 'unknown':
            ledger.settle('local-state', 'zero-tokens', None, reason='本地用量未知反例')
    snapshot = ledger.snapshot()
    bucket = snapshot['models']['local-state']
    assert bucket['unknown'] == bucket['reserved'] == 0
    assert bucket['unsettled_attempts'][0]['state'] == state
    before = Path(manifest['budget_ledger_path']).read_bytes()
    with pytest.raises(ValueError, match='在途或 unknown 用量未结清'):
        runner.run_benchmark_recovery(recovery_args(root))
    with pytest.raises(ValueError, match='在途或 unknown 用量未结清'):
        ledger.claim_guarded(lambda: (root / 'forbidden-claim').mkdir(), require_settled=True)
    with pytest.raises(ValueError, match='在途或 unknown 用量未结清'):
        create_attempt(root, case.case_id, 0, 'extra', '未结清门禁回归', manifest['execution_identity'])
    with pytest.raises(ValueError, match='在途或 unknown 用量未结清'):
        runner.run_benchmarks(root)
    assert not (root / 'forbidden-claim').exists()
    assert not (root / 'reproductions').exists()
    assert not (root / 'benchmark-attempts').exists()
    assert not (root / 'preflight').exists()
    result = runner.release_check(root)
    assert result['released'] is False
    assert any('当前共享账本存在在途或 unknown attempt' in item and state in item for item in result['blockers'])
    assert Path(manifest['budget_ledger_path']).read_bytes() == before


def test_zero_token_unknown_rejected_before_original_success_topic(tmp_path):
    root, manifest = local_experiment(tmp_path)
    ledger = BudgetLedger(manifest['budget_ledger_path'], experiment_id=root.name)
    ledger.record_outcome('success', execution_unit='topic:tool_parallel', owner=root.name, trigger='本地终态fixture')
    ledger.record_attempt('local-state', 'missing-usage', inputs=1, basis='本地状态测试，无网络')
    ledger.settle('local-state', 'missing-usage', None, reason='未知用量')
    assert ledger.snapshot()['models']['local-state']['unknown_attempts']
    with pytest.raises(ValueError, match='在途或 unknown 用量未结清'):
        runner.run_benchmark_recovery(recovery_args(root))
    assert not (root / 'benchmark-attempts').exists()


def test_recovery_budget_limit_rejects_before_claim(tmp_path):
    root, manifest = local_experiment(tmp_path)
    ledger = BudgetLedger(manifest['budget_ledger_path'], {'local-state': 1}, experiment_id=root.name)
    ledger.record_attempt('local-state', 'budget-threshold', inputs=1, basis='本地账本状态转换，无网络')
    ledger.settle('local-state', 'budget-threshold', 1)
    with pytest.raises(BudgetExhausted):
        runner.run_benchmark_recovery(recovery_args(root))
    assert not (root / 'benchmark-attempts').exists()


def test_known_zero_settled_attempt_keeps_recovery_claim_allowed(tmp_path):
    root, manifest = local_experiment(tmp_path)
    ledger = BudgetLedger(manifest['budget_ledger_path'], experiment_id=root.name)
    ledger.record_attempt('local-state', 'known-zero', inputs=1, basis='本地账本状态转换，无网络')
    ledger.settle('local-state', 'known-zero', 0)
    write_record(root / 'budget-summary.json', ledger.snapshot())
    bucket = ledger.snapshot()['models']['local-state']
    assert (bucket['actual'], bucket['unknown'], bucket['reserved'], bucket['unsettled_attempts']) == (0, 0, 0, [])
    before = Path(manifest['budget_ledger_path']).read_bytes()
    ledger.check_settled()
    marker = root / 'known-zero-claim'
    ledger.claim_guarded(lambda: marker.mkdir(parents=True, exist_ok=False), require_settled=True)
    assert marker.is_dir()
    result = runner.release_check(root)
    assert not any('local-state' in item for item in result['blockers'])
    assert Path(manifest['budget_ledger_path']).read_bytes() == before


def test_tool_parallel_recovery_real_zero_provider_attempt_and_reentry(tmp_path):
    root, manifest = local_experiment(tmp_path)
    before = (root / 'manifest.json').read_bytes()
    original_config = model_config.DATA_ROOT
    try:
        assert runner.run_benchmark_recovery(recovery_args(root)) == 0
        attempt = next(root.glob('benchmark-attempts/*/attempt.json'))
        evidence_before = {path: path.read_bytes() for path in attempt.parent.rglob('*.json')}
        with pytest.raises(ValueError, match='已成功或终态未确认'):
            runner.run_benchmark_recovery(recovery_args(root))
        args = runner.parse_args(['--experiment-root', str(root.parent), '--experiment-id', root.name,
                                  '--benchmark-topic', 'tool_parallel', '--benchmark-attempt', read_record(attempt)['attempt_id']])
        with pytest.raises(FileExistsError):
            runner.run_benchmark_worker(args, 'tool_parallel')
    finally:
        model_config.DATA_ROOT = original_config
    assert {path: path.read_bytes() for path in evidence_before} == evidence_before
    assert len(list(root.glob('benchmark-attempts/*/attempt.json'))) == 1
    assert read_record(attempt.parent / 'benchmark.json')['topic'] == 'tool_parallel'
    assert read_record(attempt.parent / 'outcome.json')['status'] == 'pass'
    assert_no_provider_usage(manifest)
    outcomes = read_record(manifest['budget_ledger_path'])['experiments'][root.name]['outcomes']
    assert set(outcomes) == {f"attempt:{read_record(attempt)['attempt_id']}"}
    assert not (root / 'cases').exists() and not (root / 'claims').exists()
    assert (root / 'manifest.json').read_bytes() == before


@pytest.mark.parametrize('kind', ['FileExistsError', 'KeyboardInterrupt', 'SystemExit', 'CancelledError'])
def test_real_benchmark_worker_failure_cancellation_and_unique_terminal(tmp_path, kind):
    root, manifest = local_experiment(tmp_path)
    completed, result = run_driver(root, 'benchmark', kind)
    assert completed.returncode != 0
    assert result['error_type'].endswith('.' + kind)
    if kind == 'SystemExit':
        assert completed.returncode == 23 and result['error'] == '23'
    assert result['event']['phase'].startswith('benchmark-body')
    assert all(result['restored'])
    directory = root / 'benchmarks/tool-parallel'
    assert (directory / 'started.json').exists()
    outcome = read_record(directory / 'outcome.json')
    expected = 'run_error' if kind == 'FileExistsError' else 'cancelled'
    assert outcome['status'] == expected
    assert_no_provider_usage(manifest)
    budget = read_record(manifest['budget_ledger_path'])
    outcomes = budget['experiments'][root.name]['outcomes']
    assert len(outcomes) == budget['completion_seq'] == 1
    assert outcomes['topic:tool_parallel']['terminal']['outcome'] == expected
    if kind == 'FileExistsError':
        assert outcome['failure_type'] == 'builtins.FileExistsError'
        assert b'FileExistsError' in completed.stderr


@pytest.mark.parametrize('kind', ['FileExistsError', 'KeyboardInterrupt'])
def test_existing_orchestrator_failure_or_predispatch_cancel_finalizes(tmp_path, kind):
    root, manifest = local_experiment(tmp_path)
    before = (root / 'manifest.json').read_bytes()
    completed, result = run_driver(root, 'orchestrator', kind, child_fault=kind == 'FileExistsError')
    assert completed.returncode != 0
    output = next((root / 'recovery').iterdir())
    for name in ('summary.json', 'execution-ledger.json', 'protection.json', 'budget-summary.json', 'provenance.json'):
        assert (output / name).is_file()
    assert read_record(output / 'summary.json')['released'] is False
    assert read_record(output / 'summary.json')['planned_slots'] == 150
    assert (root / 'manifest.json').read_bytes() == before
    assert_no_provider_usage(manifest)
    assert not (root / 'cases').exists() and not (root / 'claims').exists()
    if kind == 'FileExistsError':
        assert result['error_type'] == 'builtins.RuntimeError'
        activation = read_record(root / 'hook-activation.json')
        event = read_record(root / 'audit-event.json')
        assert activation['instance'] == event['instance'] == result['instance']
        assert activation['pid'] == event['pid'] != result['pid']
        directory = root / 'benchmarks/tool-parallel'
        assert (directory / 'started.json').is_file()
        assert read_record(directory / 'outcome.json')['failure_type'] == 'builtins.FileExistsError'
        assert read_record(directory / 'process.json')['exit_code'] != 0
        outcomes = read_record(manifest['budget_ledger_path'])['experiments'][root.name]['outcomes']
        assert set(outcomes) == {'topic:tool_parallel'}
        assert b'FileExistsError' in completed.stderr
    else:
        assert result['error_type'] == 'builtins.KeyboardInterrupt'
        assert result['event']['phase'] == 'before-worker-popen'
        assert not (root / 'hook-activation.json').exists()
        assert not list(root.glob('benchmarks/**/started.json'))


def test_unstarted_recovery_attempt_remains_blocked_without_receipt(tmp_path):
    root, manifest = local_experiment(tmp_path)
    environment_preflight(tmp_path / 'health-prerequisite', manifest['protected_baseline_before'])
    manifest['protected_baseline_before']['milvus']['collection'] += '-local-mismatch'
    write_record(root / 'manifest.json', manifest)
    with pytest.raises(ValueError, match='未解决生产保护差异'):
        runner.run_benchmark_recovery(recovery_args(root))
    attempt = next(root.glob('benchmark-attempts/*/attempt.json'))
    assert not (attempt.parent / 'started.json').exists()
    assert not (attempt.parent / 'outcome.json').exists()
    assert not (attempt.parent / 'receipt.json').exists()
    before = attempt.read_bytes()
    with pytest.raises(ValueError, match='已成功或终态未确认'):
        runner.run_benchmark_recovery(recovery_args(root))
    assert attempt.read_bytes() == before
    assert len(list(root.glob('benchmark-attempts/*/attempt.json'))) == 1
    assert_no_provider_usage(manifest)


def test_successful_original_topic_recovery_refused_without_reexecution(tmp_path):
    root, manifest = local_experiment(tmp_path)
    args = runner.parse_args(['--experiment-root', str(root.parent), '--experiment-id', root.name,
                              '--benchmark-topic', 'tool_parallel'])
    original = model_config.DATA_ROOT
    try:
        assert runner.run_benchmark_worker(args, 'tool_parallel') == 0
        before = Path(manifest['budget_ledger_path']).read_bytes()
        with pytest.raises(ValueError, match='成功专项禁止重发'):
            runner.run_benchmark_recovery(recovery_args(root))
        assert Path(manifest['budget_ledger_path']).read_bytes() == before
    finally:
        model_config.DATA_ROOT = original
    assert not (root / 'benchmark-attempts').exists()
    assert_no_provider_usage(manifest)


def test_recovery_attempt_keeps_parent_and_tool_identities_separate(tmp_path, configured):
    from app.application.business import retrieval_frozen

    root = tmp_path / 'parent'
    attempt_id = uuid4().hex
    directory = root / 'benchmark-attempts' / attempt_id
    directory.mkdir(parents=True)
    source = root / 'manifest.json'
    manifest = {'model_identity': configured, 'cases_path': str(CASES),
                'cases_sha256': sha256_file(CASES),
                'catalog_sha256': sha256_file(runner.DEFAULT_ACTIONS_PATH),
                'retrieval_frozen': retrieval_frozen.frozen_values(),
                'execution_identity': {'source_sha256': 'parent-historical'},
                'budget_ledger_path': str(tmp_path / 'budget-ledger.json')}
    write_record(source, manifest)
    args = runner.parse_args(['--experiment-root', str(tmp_path), '--experiment-id', 'parent',
                              '--benchmark-topic', 'tool_parallel', '--benchmark-attempt', attempt_id])
    record = {'attempt_id': attempt_id, 'parent_experiment_id': 'parent',
              'topic': 'tool_parallel', 'model_identity': configured,
              'tool_identity': runner.current_identity(CASES, configured),
              'source_identity': manifest['execution_identity'],
              'budget_ledger_path': manifest['budget_ledger_path'], 'stop_scope': 'parent',
              'source_evidence': [{'path': str(source), 'sha256': sha256_file(source)}]}
    write_record(directory / 'attempt.json', record)
    assert runner.validate_benchmark_attempt(args, manifest, configured)['source_identity'] == manifest['execution_identity']
    assert read_record(source) == manifest
    record['tool_identity'] = {'source_sha256': 'changed-tool'}
    write_record(directory / 'attempt.json', record)
    with pytest.raises(ValueError, match='身份不匹配'):
        runner.validate_benchmark_attempt(args, manifest, configured)
    args.benchmark_attempt = '../escape'
    with pytest.raises(ValueError, match='身份无效'):
        runner.benchmark_directory(args, 'tool_parallel')


if __name__ == '__main__' and sys.argv[1:2] == ['--probe-child']:
    print('ready', flush=True)
    input()
    for _ in range(20):
        runner.precheck(CASES, Path(sys.argv[2]))
