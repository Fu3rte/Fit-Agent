import json
import os
import sys
from pathlib import Path


def install():
    if os.environ.get('FIT_AGENT_B6_TEST_HOOK') != '1':
        return
    instance = os.environ.get('FIT_AGENT_B6_TEST_INSTANCE', '')
    if len(instance) != 32 or any(char not in '0123456789abcdef' for char in instance):
        return
    command = sys.orig_argv
    if '-m' not in command or command[command.index('-m') + 1] != 'test.run_v2_eval':
        return
    required = ('--experiment-root', '--experiment-id', '--benchmark-topic')
    if any(command.count(flag) != 1 or command.index(flag) + 1 >= len(command) for flag in required):
        return
    if command[command.index('--benchmark-topic') + 1] != 'tool_parallel':
        return
    root = Path(os.environ['FIT_AGENT_B6_TEST_ROOT']).resolve()
    output_root = Path(os.environ.get('FIT_AGENT_B6_TEST_OUTPUT_ROOT', '')).resolve()
    if not output_root or not root.is_relative_to(output_root / 'pytest-r2'):
        return
    actual = (Path(command[command.index('--experiment-root') + 1])
              / command[command.index('--experiment-id') + 1]).resolve()
    if actual != root or not (root / 'manifest.json').is_file():
        return
    from lifecycle_driver_r2 import install_local_hook

    install_local_hook(root, 'benchmark', 'FileExistsError', instance)
    (root / 'hook-activation.json').write_text(json.dumps({
        'instance': instance, 'pid': os.getpid(), 'root': str(root), 'topic': 'tool_parallel',
    }, indent=2), encoding='utf-8')


install()
