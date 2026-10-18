import asyncio
import ipaddress
import json
import os
import shlex
import signal
import sys
from pathlib import Path


def install_local_hook(root, mode, kind, instance):
    root = Path(root).resolve()
    directory = root / 'benchmarks/tool-parallel'
    event_record = {}
    active = True

    def hook(event, args):
        nonlocal active
        if event in {'socket.connect', 'socket.getaddrinfo'}:
            address = args[1][0] if event == 'socket.connect' else args[0]
            address = os.fsdecode(address)
            if address != 'localhost' and not ipaddress.ip_address(address).is_loopback:
                raise PermissionError('本地回归禁止外部网络连接')
        if not active:
            return
        target = None
        phase = None
        if mode in {'slot', 'additional'} and event == 'open' and isinstance(args[0], (str, bytes)):
            candidate = Path(os.fsdecode(args[0])).resolve()
            expected = root / 'cases/P01/round-0/precondition.json'
            additional = (candidate.is_relative_to(root / 'reproductions')
                          and candidate.parts[-4:] == ('cases', 'P01', 'round-0', 'precondition.json'))
            if candidate == expected or (mode == 'additional' and additional):
                target, phase = candidate, 'precondition-evidence-before-business'
        elif mode == 'benchmark' and event == 'os.mkdir' and kind == 'FileExistsError':
            candidate = Path(os.fsdecode(args[0])).resolve()
            if (candidate.is_relative_to(directory / 'tmp') and candidate.name == 'workspace'
                    and (directory / 'started.json').is_file()):
                target, phase = candidate, 'benchmark-body-workspace'
        elif mode == 'benchmark' and event == 'tempfile.mkdtemp' and kind != 'FileExistsError':
            candidate = Path(os.fsdecode(args[0])).resolve()
            if candidate.parent == directory / 'tmp' and (directory / 'started.json').is_file():
                target, phase = candidate, 'benchmark-body-before-temporary-directory'
        elif mode == 'orchestrator' and event == 'subprocess.Popen' and kind == 'KeyboardInterrupt':
            # Windows 的审计事件把完整命令行作为字符串给出；统一成参数序列后精确匹配。
            command = args[1]
            if isinstance(command, str):
                command = shlex.split(command, posix=False)
            if (isinstance(command, (list, tuple)) and '--benchmark-topic' in command
                    and command[command.index('--benchmark-topic') + 1] == 'tool_parallel'
                    and command[command.index('--experiment-id') + 1] == root.name):
                target, phase = directory, 'before-worker-popen'
        if target is None:
            return
        active = False
        event_record.update(instance=instance, pid=os.getpid(), event=event,
                            target=str(target), phase=phase, kind=kind)
        (root / 'audit-event.json').write_text(json.dumps(event_record, ensure_ascii=False, indent=2), encoding='utf-8')
        if kind == 'FileExistsError':
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('本地文件系统冲突', encoding='utf-8')
        elif kind == 'KeyboardInterrupt':
            if mode == 'orchestrator':
                # 派发前取消：直接中断 Popen 构造，确保子进程不会被创建。
                raise KeyboardInterrupt
            signal.raise_signal(signal.SIGINT)
        elif kind == 'SystemExit':
            sys.exit(23)
        elif kind == 'CancelledError':
            loop = asyncio.new_event_loop()
            try:
                future = loop.create_future()
                future.cancel()
                loop.run_until_complete(future)
            finally:
                loop.close()
        else:
            raise ValueError(kind)

    sys.addaudithook(hook)
    return event_record


def main():
    from app import model_config
    from app.agent import cache_observation, telemetry
    from app.application.session import service as session_service
    from app.infrastructure.persistence.sqlite import database
    from app.interfaces import http
    from app.interfaces.http_api.routes import agent as agent_routes
    from test import p0_business_cases
    from test import run_v2_eval as runner
    from test.v2_eval_support import exception_type, load_cases

    runner.load_backend_env()
    mode, root, kind = sys.argv[1:]
    root = Path(root).resolve()
    # 输出根由调用方显式绑定到本轮工件根；历史工件目录不再作为可执行代码来源。
    output_root = Path(os.environ['FIT_AGENT_B6_TEST_OUTPUT_ROOT']).resolve()
    if not root.is_relative_to(output_root / 'pytest-r2'):
        raise ValueError('测试根必须位于本轮独立pytest命名空间')
    manifest = runner.read_record(root / 'manifest.json')
    instance = os.environ['FIT_AGENT_B6_TEST_INSTANCE']
    event = install_local_hook(root, mode, kind, instance)
    originals = (session_service.PROJECT_ROOT, agent_routes.create_file_tools,
                 database.default_database_path, telemetry.trace_database_path,
                 cache_observation.observation_directory, model_config.DATA_ROOT, p0_business_cases.TMP_ROOT)
    args = runner.parse_args(['--experiment-root', str(root.parent), '--experiment-id', root.name,
                              '--cases', manifest['cases_path']])
    result = {'instance': instance, 'root': str(root), 'mode': mode, 'kind': kind, 'pid': os.getpid()}
    try:
        if mode == 'benchmark':
            args.benchmark_topic = 'tool_parallel'
            result['return_code'] = runner.run_benchmark_worker(args, args.benchmark_topic)
        elif mode == 'orchestrator':
            args.benchmarks_only = True
            result['return_code'] = runner.run_existing_benchmarks(args)
        elif mode == 'slot':
            args.process_index = 0
            args.processes = 1
            result['return_code'] = runner.run_worker(args, load_cases(args.cases)[:1])
        elif mode == 'additional':
            args.case_id, args.round_index = 'P01', 0
            args.purpose, args.reason = 'extra', '本地控制流传播'
            result['return_code'] = runner.run_additional(args)
        else:
            raise ValueError(mode)
    except BaseException as error:
        result.update(error_type=exception_type(error), error=str(error))
        raise
    finally:
        current = (session_service.PROJECT_ROOT, agent_routes.create_file_tools,
                   database.default_database_path, telemetry.trace_database_path,
                   cache_observation.observation_directory, model_config.DATA_ROOT, p0_business_cases.TMP_ROOT)
        result.update(event=event, restored=[left == right for left, right in zip(originals, current)])
        runner.write_record(root / 'driver-result.json', result)


if __name__ == '__main__':
    main()
