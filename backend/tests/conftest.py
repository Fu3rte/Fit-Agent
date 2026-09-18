"""pytest 装配：异步测试统一走 anyio 内置插件。

anyio 是已拍技术栈 fastapi 的既有传递依赖（插件已随包注册为 pytest11 入口点），
不新增依赖；在此为所有 async 测试自动打 ``anyio`` 标记，后端固定 asyncio。
"""

import inspect
import re
from pathlib import Path

import pytest

#: Stage 5 契约正本：Stage 5 的四个测试文件都从这一份文件读 §3 原文做「代码前断言」
#: （``refactor-log/stage5.md`` §6 Subtask 01；历史 ``stage1.md``–``stage4.md`` 不回改）。
STAGE5_PLAN_PATH = Path(__file__).resolve().parents[2] / "refactor-log" / "stage5.md"

#: 表格单元格里的转义竖线（``\|``）不是列分隔符。
_CELL_SEPARATOR = re.compile(r"(?<!\\)\|")


class Stage5Plan:
    """``refactor-log/stage5.md`` 的只读切片（小节原文与表格数据行）：只作冻结断言，不解释语义。"""

    def __init__(self, text: str):
        self._text = text

    def section(self, heading: str) -> str:
        """``### <heading>`` 小节的原文（不含标题行，到下一个 ``###``／``##`` 标题前）。"""
        marker = f"### {heading}"
        rest = self._text[self._text.index(marker) + len(marker) :]
        ends = [end for boundary in ("\n### ", "\n## ") if (end := rest.find(boundary)) >= 0]
        return rest[: min(ends)] if ends else rest

    def table(self, heading: str, header: str) -> tuple[tuple[str, ...], ...]:
        """小节内「第二列表头为 ``header``」的表格数据行；表头与 ``| --- |`` 分隔行不计入。"""
        rows: list[tuple[str, ...]] = []
        heading_row = True  # 每张表的第一行是表头
        collecting = False
        for line in self.section(heading).splitlines():
            if not line.startswith("|"):
                heading_row, collecting = True, False
                continue
            if set(line) <= set("|- "):  # 分隔行
                continue
            cells = tuple(cell.strip() for cell in _CELL_SEPARATOR.split(line.strip("|")))
            if heading_row:
                heading_row = False
                collecting = len(cells) > 1 and cells[1] == header
                continue
            if collecting:
                rows.append(cells)
        return tuple(rows)


@pytest.fixture(scope="session")
def stage5_plan() -> Stage5Plan:
    """Stage 5 正本 §3 的切片入口：四个 Stage 5 测试文件共用，避免各自复制一份 Markdown 解析。"""
    return Stage5Plan(STAGE5_PLAN_PATH.read_text(encoding="utf-8"))


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
