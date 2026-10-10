import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from app.agent.tools.business import bind_business_tools
from app.domain.business.models import BusinessContext
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app
from test.check_business_profile import _StatementSync
from test.check_http import events, wait_idle
from test.check_profile_confirmation import Fixture, payload, prepare_arguments, propose
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    run_tool,
    temporary_root,
)

EVIDENCE = temporary_root('profile-delete-interleaving') / uuid4().hex
EVIDENCE.mkdir()


def call(coro):
    return asyncio.run_coroutine_threadsafe(coro, app.state.loop).result(timeout=60)


async def seed(path):
    fixture = await Fixture(path).seeded()
    try:
        session = await fixture.session('TASK5_SAVE_DELETE')
        initial = await propose(fixture, session, prepare_arguments(None, payload()))
        saved = await fixture.service_save(initial)
        pending = await propose(fixture, session, prepare_arguments(1, payload(goal='提高耐力')))
        return pending, saved.model_dump()
    finally:
        await fixture.close()


def save(ids):
    context = BusinessContext(timezone='Asia/Shanghai', business_date='2026-10-07',
                              session_id=ids['session'], run_id=ids['run'],
                              request_entry_id=ids['request'], source_entry_id=ids['source'])
    tool = bind_business_tools(app.state.business, context, call, {}, {})['save_profile_update']
    return run_tool(tool, {'proposal_id': ids['proposal'], 'display_entry_id': ids['display'],
                           'confirmation_entry_id': ids['confirmation']})


def fingerprint(path):
    with sqlite3.connect(path) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() for table in tables}


def check() -> None:
    install_test_model_config()
    results = []
    for phase in ('before_commit', 'after_commit'):
        path = EVIDENCE / (phase + '.db')
        ids, initial = asyncio.run(seed(path))
        database_module.default_database_path = lambda: path
        with Server(app) as server, client(server.base_url) as http:
            connection = app.state.session_service._repository._database.connection
            if phase == 'before_commit':
                sync = _StatementSync('COMMIT', nth=2)
                call(connection.set_trace_callback(sync.trace))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    saving = pool.submit(save, ids)
                    try:
                        assert sync.entered.wait(30)
                        deleting = pool.submit(http.delete, f"/api/sessions/{ids['session']}")
                        deadline = time.monotonic() + 20
                        while app.state.replacements.pending_count(ids['session']) == 0:
                            assert time.monotonic() < deadline
                            time.sleep(0.01)
                    finally:
                        sync.release.set()
                    result = deleting.result(timeout=45)
                    message = saving.result(timeout=45)
                call(connection.set_trace_callback(None))
                assert result.status_code == 200
                assert message.is_error
                detail = json.loads(''.join(block.text for block in message.content))
                assert detail['code'] == 'session_not_found'
                expected_version, expected_records = 1, 1
            else:
                message = save(ids)
                assert not message.is_error
                saved = json.loads(''.join(block.text for block in message.content))
                assert saved['version'] == 2
                result = http.delete(f"/api/sessions/{ids['session']}")
                assert result.status_code == 200
                expected_version, expected_records = 2, 2
            assert http.get('/api/profile').json()['version'] == expected_version
            assert http.get(f"/api/sessions/{ids['session']}/history").status_code == 404
            with sqlite3.connect(path) as db:
                assert db.execute('SELECT COUNT(*) FROM profile_snapshots').fetchone()[0] == 0
                assert db.execute('SELECT COUNT(*) FROM profile_save_records').fetchone()[0] == expected_records
                preserved = json.loads(db.execute('SELECT result FROM profile_save_records WHERE proposal_id=?', (initial['proposal_id'],)).fetchone()[0])
                assert preserved == initial
                assert db.execute('PRAGMA foreign_key_check').fetchall() == []
                assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            results.append({'phase': phase, 'version': expected_version, 'saved_records': expected_records, 'integrity': 'ok'})

    path = EVIDENCE / 'delete-rollback.db'
    ids, initial = asyncio.run(seed(path))
    database_module.default_database_path = lambda: path
    with Server(app) as server, client(server.base_url) as http:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER task5_refuse_delete BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT, 'TASK5_DELETE_ABORT'); END")
        before = fingerprint(path)
        response = http.delete(f"/api/sessions/{ids['session']}")
        assert response.status_code == 500 and response.json()['detail']['code'] == 'internal_error'
        assert fingerprint(path) == before
        assert http.get('/api/profile').json()['version'] == 1
        assert http.get(f"/api/sessions/{ids['session']}/history").status_code == 200
        with sqlite3.connect(path) as db:
            db.execute('DROP TRIGGER task5_refuse_delete')
        response = http.delete(f"/api/sessions/{ids['session']}")
        assert response.status_code == 200
        results.append({'phase': 'delete_transaction_abort', 'rollback': 'complete', 'retry': 'deleted'})

        session = str(uuid4())
        assert http.post('/api/sessions', json={'session_id': session, 'title': 'TASK5_RUNNING_DELETE'}).status_code == 201
        streamed = Event()

        async def running():
            with client(server.base_url) as streaming:
                with streaming.stream('POST', '/api/agent/run', json={'session_id': session, 'operation_id': str(uuid4()), 'request': '逐行输出从 1 到 100000 的整数，不要省略。无需使用工具。'}) as stream:
                    assert stream.status_code == 200
                    collected = []
                    for event in events(stream):
                        collected.append(event)
                        if event['event'] == 'message_update':
                            streamed.set()
                    return collected
        with ThreadPoolExecutor(max_workers=3) as pool:
            executing = pool.submit(lambda: asyncio.run(running()))
            deadline = time.monotonic() + 60
            # 真实模型流已开始增量输出，运行记录仍在 running，此时并发删除会话。
            while not streamed.is_set():
                assert time.monotonic() < deadline
                time.sleep(0.05)
            body = http.get(f'/api/sessions/{session}/history').json()
            assert any(run['status'] == 'running' for run in body['runs']), body['runs']
            deletes = [pool.submit(http.delete, f'/api/sessions/{session}') for _ in range(2)]
            responses = [future.result(timeout=60) for future in deletes]
            terminal = executing.result(timeout=60)
        assert all(r.status_code == 200 and r.json() == {'session_id': session, 'deleted': True} for r in responses)
        wait_idle()
        assert http.get(f'/api/sessions/{session}/history').status_code == 404
        assert http.get('/api/profile').json()['version'] == 1
        assert terminal[-1]['event'] in {'done', 'error'}
        results.append({'phase': 'real_model_running_parallel_delete', 'deleted': True, 'profile_version': 1, 'terminal': terminal[-1]['event']})
    (EVIDENCE / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(f'真实保存与 HTTP 删除交错、事务回滚、运行中并发删除通过：{EVIDENCE}')


if __name__ == '__main__':
    check()
