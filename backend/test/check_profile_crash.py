import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from uuid import uuid4

from test.check_profile_confirmation import Fixture, payload, prepare_arguments, propose
from test.regression_support import temporary_root

ROOT = temporary_root('profile-crash') / uuid4().hex


async def worker(root: Path, phase: str) -> None:
    fixture = await Fixture(root / 'crash.db').seeded()
    session = await fixture.session('TASK5_CRASH')
    ids = await propose(fixture, session, prepare_arguments(None, payload()))
    identity = {key: ids[key] for key in ('session', 'request', 'source', 'display', 'confirmation', 'proposal')}
    (root / 'identity.json').write_text(json.dumps(identity), encoding='utf-8')
    blocked = Event()
    commits = 0

    def trace(sql: str) -> None:
        nonlocal commits
        if sql == 'COMMIT':
            commits += 1
            if phase == 'before_commit' and commits == 2:
                (root / 'ready.json').write_text(json.dumps({'phase': phase, 'pid': os.getpid()}), encoding='utf-8')
                blocked.wait(60)

    await fixture.database.connection.set_trace_callback(trace)
    result = await fixture.service_save(identity)
    assert phase == 'after_commit'
    (root / 'saved.json').write_text(json.dumps({'proposal_id': result.proposal_id, 'version': result.version, 'saved_at': result.saved_at}), encoding='utf-8')
    (root / 'ready.json').write_text(json.dumps({'phase': phase, 'pid': os.getpid()}), encoding='utf-8')
    await asyncio.to_thread(blocked.wait, 60)


async def recover(root: Path, phase: str) -> dict:
    ids = json.loads((root / 'identity.json').read_text(encoding='utf-8'))
    fixture = await Fixture(root / 'crash.db').open(recovered=True)
    try:
        snapshot = await fixture.snapshot(ids['proposal'])
        before = await fixture.profile_state()
        expected = 'pending' if phase == 'before_commit' else 'saved'
        assert snapshot.status == expected
        assert snapshot.confirmation_entry_id == ids['confirmation']
        assert snapshot.display_entry_id == ids['display']
        assert before['version'] == (None if phase == 'before_commit' else 1)
        saved = await fixture.service_save(ids)
        assert saved.proposal_id == ids['proposal'] and saved.version == 1
        assert await fixture.profile_state() == {'version': 1, 'profile_rows': 1, 'record_rows': 1}
        status = await fixture.service_status(ids)
        assert status.status == 'saved' and status.result == saved
        if phase == 'after_commit':
            original = json.loads((root / 'saved.json').read_text(encoding='utf-8'))
            assert original == {'proposal_id': saved.proposal_id, 'version': saved.version, 'saved_at': saved.saved_at}
        async with fixture.database.transaction_scope():
            foreign = await (await fixture.database.connection.execute('PRAGMA foreign_keys')).fetchone()
            violations = await (await fixture.database.connection.execute('PRAGMA foreign_key_check')).fetchall()
            integrity = await (await fixture.database.connection.execute('PRAGMA integrity_check')).fetchall()
        assert foreign[0] == 1 and violations == [] and [row[0] for row in integrity] == ['ok']
        return {'phase': phase, 'recovered': expected, 'proposal_id': saved.proposal_id,
                'version': 1, 'saved_at': saved.saved_at, 'foreign_keys': 1,
                'foreign_key_check': [], 'integrity_check': 'ok'}
    finally:
        await fixture.close()


def terminate_owned(process) -> None:
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], check=True, capture_output=True)
    else:
        process.kill()
    process.wait(timeout=15)


def check() -> None:
    ROOT.mkdir()
    results = []
    for phase in ('before_commit', 'after_commit'):
        directory = ROOT / phase
        directory.mkdir()
        with (directory / 'worker.log').open('w', encoding='utf-8') as output:
            process = subprocess.Popen([sys.executable, '-m', 'test.check_profile_crash', str(directory), phase], stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while not (directory / 'ready.json').exists():
                    assert process.poll() is None, directory / 'worker.log'
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                marker = json.loads((directory / 'ready.json').read_text(encoding='utf-8'))
                assert marker['phase'] == phase and marker['pid'] > 0
                terminate_owned(process)
            finally:
                if process.poll() is None:
                    terminate_owned(process)
        results.append(asyncio.run(recover(directory, phase)))
        (ROOT / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(f'真实保存进程强制中断与恢复通过：{ROOT}')


if __name__ == '__main__':
    if len(sys.argv) == 3:
        asyncio.run(worker(Path(sys.argv[1]), sys.argv[2]))
    else:
        check()
