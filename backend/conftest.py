# 让 backend/ 成为 pytest 的 rootdir 锚点，使 app 包与 tests 包在测试中可导入。

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """协程测试的后端固定为 asyncio；anyio 自动模式负责收集标记。"""
    return "asyncio"
