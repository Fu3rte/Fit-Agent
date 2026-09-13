"""pytest 装配：异步测试统一走 anyio 内置插件。

anyio 是已拍技术栈 fastapi 的既有传递依赖（插件已随包注册为 pytest11 入口点），
不新增依赖；在此为所有 async 测试自动打 ``anyio`` 标记，后端固定 asyncio。
"""

import inspect

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        if isinstance(item, pytest.Function) and inspect.iscoroutinefunction(
            item.function
        ):
            item.add_marker(pytest.mark.anyio)
