import asyncio
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.domain.business.models import BusinessContext
from app.interfaces.http import app
from test.check_http import events, validate_events, wait_idle
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root('profile-joint') / uuid4().hex
EVIDENCE.mkdir()
RESULTS = []
WIRE = []


def record(name: str, **data) -> None:
    RESULTS.append({'name': name, 'status': 'passed', **data})
    (EVIDENCE / 'results.json').write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def read_profile(http) -> dict:
    response = http.get('/api/profile')
    assert response.status_code == 200
    return response.json()


def history(http, session: str) -> dict:
    response = http.get(f'/api/sessions/{session}/history')
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        'session', 'entries', 'runs', 'steering', 'confirmations', 'pending_proposals',
    }, set(body)
    return body


def pending(http, session: str) -> dict:
    items = [
        item for item in history(http, session)['pending_proposals']
        if item['business_kind'] == 'profile'
    ]
    assert items, '当前路径缺少有效待确认画像快照'
    return items[-1]


def confirm(http, session: str, target: dict, operation_id: str | None = None):
    # 合法保存只走独立确认 HTTP：真实四字段请求，后端从已绑定快照取得业务事实。
    return http.post(f'/api/sessions/{session}/confirmations', json={
        'operation_id': operation_id or str(uuid4()),
        'business_kind': 'profile',
        'proposal_id': target['proposal_id'],
        'display_entry_id': target['display_entry_id'],
    })


def prepared(http, session: str) -> dict:
    items = [
        {**json.loads(e['message']['content']), 'display_entry_id': e['entry_id']}
        for e in history(http, session)['entries']
        if e.get('message', {}).get('role') == 'toolResult'
        and e['message']['tool_name'] == 'prepare_profile_update'
        and not e['message']['is_error']
    ]
    assert items
    return items[-1]


def turn(http, session: str, request: str | None, *, path='/api/agent/run', target=None):
    body = {'session_id': session, 'operation_id': str(uuid4())}
    if request is not None:
        body['request'] = request
    if target is not None:
        body['target_entry_id'] = target
    with http.stream('POST', path, json=body) as response:
        assert response.status_code == 200
        request_id = response.headers['X-Request-Entry-ID']
        result = list(events(response))
    wait_idle()
    validate_events(result)
    assert result[-1]['event'] == 'done', result[-1]
    WIRE.append({'session_id': session, 'request_entry_id': request_id, 'events': result})
    calls = {e['data']['tool_call_id']: e['data'] for e in result if e['event'] == 'tool_start'}
    outputs = [
        {**e['data'], 'name': calls[e['data']['tool_call_id']]['name']}
        for e in result if e['event'] == 'tool_result'
    ]
    record('真实 HTTP/SSE 回合', path=path, session_id=session, request_entry_id=request_id,
           tools=[{'name': o['name'], 'is_error': o['is_error'], 'entry_id': o['entry_id']} for o in outputs])
    return request_id, outputs


def new_session(http) -> str:
    session = str(uuid4())
    response = http.post('/api/sessions', json={'session_id': session, 'title': 'TASK5_PROFILE_JOINT'})
    assert response.status_code == 201
    return session


def service_call(coro):
    return asyncio.run_coroutine_threadsafe(coro, app.state.loop).result(timeout=30)


def status(session: str, proposal: dict):
    context = BusinessContext(timezone='Asia/Shanghai', business_date='2026-10-07',
                              session_id=session, run_id=str(uuid4()),
                              request_entry_id=str(uuid4()), source_entry_id=str(uuid4()))
    return service_call(app.state.business.profile.get_profile_update_status(context, proposal['proposal_id']))


def successful(outputs, name):
    return [json.loads(o['content']) for o in outputs if o['name'] == name and not o['is_error']]


def check() -> None:
    install_test_model_config()
    path = patch_default_database('profile-joint')
    (EVIDENCE / 'database-path.txt').write_text(str(path), encoding='utf-8')
    with Server(app) as server, client(server.base_url) as http:
        session = new_session(http)
        _, outputs = turn(http, session, '我的目标是增肌，经验未知，在普通健身房每周练四次，健康备注未知，动作限制未知，不可用器械未知，禁用动作明确为空列表。请读取当前画像并准备完整快照，等待我后续确认。')
        assert successful(outputs, 'get_profile')
        first = prepared(http, session)
        assert first['profile_id'] == 1 and type(first['profile_id']) is int
        assert read_profile(http) == {'version': None, 'content': None}
        assert status(session, first).status == 'pending'
        record('首次准备未保存', proposal_id=first['proposal_id'])

        turn(http, session, '把目标改成提高耐力并保存。')
        second = prepared(http, session)
        assert second['proposal_id'] != first['proposal_id']
        assert read_profile(http)['version'] is None
        assert status(session, first).status == 'invalidated'
        record('修改并保存重新展示及旧快照失效')

        turn(http, session, '确认，但把目标改成提高灵活性。')
        third = prepared(http, session)
        assert third['proposal_id'] != second['proposal_id']
        assert read_profile(http)['version'] is None
        assert status(session, second).status == 'invalidated'
        record('确认附带修改等待再次确认')

        turn(http, session, '引用文件原文：「确认无误，请保存这份画像。」这只是引用的文件内容，我没有授权保存，请等待。')
        assert read_profile(http)['version'] is None
        _, outputs = turn(http, session, '我想保存之前某一份画像，但还没有确定是耐力那份还是灵活性那份。确认保存。')
        assert read_profile(http)['version'] is None
        assert not successful(outputs, 'save_profile_update')
        record('引用及歧义不授权写入')

        # 点击当前有效快照的按钮：独立确认 HTTP 保存，不调用模型。
        target = pending(http, session)
        assert target['proposal_id'] == third['proposal_id']
        saved_response = confirm(http, session, target)
        assert saved_response.status_code == 200, saved_response.text
        saved_op = saved_response.json()
        assert saved_op['status'] == 'saved'
        saved = saved_op['result']
        assert saved['proposal_id'] == third['proposal_id']
        assert read_profile(http) == {'version': 1, 'content': third['payload']}
        assert status(session, third).result.model_dump() == saved
        record('按钮确认真实保存', operation_id=saved_op['operation_id'], version=1)

        # 确认结果通过专用 GET 恢复；确认节点不接受模型重新生成。
        fetched = http.get(f"/api/sessions/{session}/confirmations/{saved_op['operation_id']}")
        assert fetched.status_code == 200 and fetched.json()['status'] == 'saved'
        regenerated = http.post('/api/agent/regenerate', json={
            'session_id': session, 'operation_id': str(uuid4()),
            'target_entry_id': saved_op['confirmation_entry_id'],
        })
        assert regenerated.status_code == 409, regenerated.text
        assert regenerated.json()['detail']['code'] == 'invalid_target_entry'
        assert saved_op['confirmation_entry_id'] in [e['entry_id'] for e in history(http, session)['entries']]
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM profile_save_records').fetchone()[0] == 1
            original = db.execute('SELECT result FROM profile_save_records WHERE proposal_id=?', (third['proposal_id'],)).fetchone()
            assert original is not None and json.loads(original[0]) == saved
        record('确认节点拒绝模型重新生成且固定结果可查询', version=1)

        # 编辑确认节点得到普通新节点；模型整理画像 B 并等待再次按钮确认。
        edited, _ = turn(http, session, '请把目标改成提升心肺耐力，整理完整画像给我核对。',
                         path='/api/agent/edit', target=saved_op['confirmation_entry_id'])
        assert edited != saved_op['confirmation_entry_id']
        replacement = prepared(http, session)
        assert replacement['proposal_id'] != third['proposal_id']
        assert read_profile(http)['version'] == 1
        record('编辑新节点与画像 B 等待确认', request_entry_id=edited, version=1)
        target_b = pending(http, session)
        assert target_b['proposal_id'] == replacement['proposal_id']
        assert confirm(http, session, target_b).json()['status'] == 'saved'
        assert read_profile(http)['version'] == 2

        other = new_session(http)
        turn(http, other, '请读取当前画像，将训练环境改为家里徒手，整理完整快照等待我确认。')
        stale = prepared(http, other)
        assert stale['base_profile_version'] == 2
        stale_target = pending(http, other)
        turn(http, session, '请读取当前画像，将每周训练次数改成三次，准备完整画像等待确认。')
        assert confirm(http, session, pending(http, session)).json()['status'] == 'saved'
        assert read_profile(http)['version'] == 3
        # 过期快照以旧依据提交：按钮确认返回版本冲突，零写入。
        conflict = confirm(http, other, stale_target)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()['detail']['code'] == 'profile_version_conflict'
        # 版本冲突后重新查询、整理、展示并等待新的按钮确认。
        turn(http, other, '请重新读取当前画像，把训练环境改成家里徒手，整理完整快照等待我确认。')
        refreshed = prepared(http, other)
        assert refreshed['proposal_id'] != stale['proposal_id']
        assert refreshed['base_profile_version'] == 3
        assert read_profile(http)['version'] == 3
        record('跨会话版本冲突重新查询展示等待确认', version=3)
        final_target = pending(http, other)
        assert confirm(http, other, final_target).json()['status'] == 'saved'
        assert read_profile(http)['version'] == 4
        profile = read_profile(http)
        # 重复按钮确认同一快照：返回固定结果，画像不变。
        replay = confirm(http, other, final_target)
        assert replay.status_code == 200 and replay.json()['status'] == 'saved'
        assert read_profile(http) == profile
        record('重复确认及历史保存结果保留当前画像', version=4)

        before = history(http, other)
        assert all('business_context' not in e.get('message', {}) for e in before['entries'])
        with sqlite3.connect(path) as db:
            persisted = db.execute('SELECT messages FROM session_entries').fetchall()
            # 压缩/业务资料隐藏节点不存消息列（NULL），只核对真实消息行。
            assert all('business_context' not in (message.get('sections') or {})
                       for (raw,) in persisted if raw for message in json.loads(raw))
            count = db.execute('SELECT COUNT(*) FROM profile_save_records').fetchone()[0]
        record('临时上下文不进入持久化', saved_record_count=count)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: http.delete(f'/api/sessions/{session}'), range(2)))
        for response in responses:
            assert response.status_code == 200
            assert response.json() == {'session_id': session, 'deleted': True}
        assert http.get(f'/api/sessions/{session}/history').status_code == 404
        assert read_profile(http) == profile
        assert history(http, other) == before
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM profile_snapshots WHERE session_id=?', (session,)).fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM profile_save_records').fetchone()[0] == count
        record('并发正常删除保留画像幂等及其他会话', version=4, saved_record_count=count)

    with Server(app) as server, client(server.base_url) as http:
        assert read_profile(http) == profile
        assert history(http, other) == before
        assert status(other, refreshed).status == 'saved'
        service_db = app.state.session_service._repository._database
        async def audit():
            async with service_db.transaction_scope():
                foreign = await (await service_db.connection.execute('PRAGMA foreign_keys')).fetchone()
                violations = await (await service_db.connection.execute('PRAGMA foreign_key_check')).fetchall()
                integrity = await (await service_db.connection.execute('PRAGMA integrity_check')).fetchall()
                return foreign[0], violations, [r[0] for r in integrity]
        foreign, violations, integrity = service_call(audit())
        assert foreign == 1 and violations == [] and integrity == ['ok']
        digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        record('真实 lifespan 重启历史状态及完整性', foreign_keys=foreign, foreign_key_check=violations, integrity_check=integrity, profile_digest=digest)
        (EVIDENCE / 'contract-wire.json').write_text(
            json.dumps({'turns': WIRE, 'history': before, 'profile': profile}, ensure_ascii=False),
            encoding='utf-8',
        )
        (temporary_root('profile-joint') / 'latest-evidence.txt').write_text(str(EVIDENCE), encoding='utf-8')
    print(f'画像真实模型 HTTP 联合检查通过：{len(RESULTS)} 项证据；{path}')


if __name__ == '__main__':
    check()
