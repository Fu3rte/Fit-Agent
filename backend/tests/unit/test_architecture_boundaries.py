# 分层边界的 AST 硬校验：domain 只引用 domain，application 不依赖传输层与组合根，application 不得导入
# Provider SDK，infrastructure 不依赖传输层，且全树不再引用已删除的旧顶层包。
# 依据：架构决议 §1（架构边界与 Agent 分层）、§5.4（新增依赖方向约束）与 §8（分层边界硬校验）。

import ast
from pathlib import Path
from typing import get_type_hints

from app.application.agent.contracts import PlanLlmNodeDeps
from app.application.agent.plan_nodes import EvaluatorAgentNode, PlannerAgentNode

BACKEND_DIR = Path(__file__).resolve().parents[2]
APP_DIR = BACKEND_DIR / "app"

#: 已删除的旧顶层包：任何 app 模块再引用它们都是迁移残留。
LEGACY_ROOTS = ("api", "domain", "graph", "harness", "storage", "provider_settings")

#: LLM Node 依赖里一律不得出现的类型名：Repository、事务对象、业务写 Service 与事实装配器。
FORBIDDEN_LLM_DEPENDENCY_TYPES: tuple[str, ...] = (
    "PlansService",
    "MemoryAssembler",
    "MemoryContext",
    "Database",
    "Connection",
    "Transaction",
)

#: LLM Node 依赖里一律不得出现的模块前缀：数据库基础设施（Repository 实现）与组合根。
FORBIDDEN_LLM_DEPENDENCY_MODULES: tuple[str, ...] = (
    "app.infrastructure",
    "app.bootstrap",
)


def _type_names(dataclass_type: type) -> set[str]:
    """一个 dataclass 的字段注解的完全限定名（未注解名的可调用类型回退到 ``str``）。"""
    return {
        f"{getattr(hint, '__module__', '')}.{getattr(hint, '__name__', hint)}"
        for hint in get_type_hints(dataclass_type).values()
    }


def _forbidden_dependency_hits(type_names: set[str]) -> list[str]:
    return sorted(
        name
        for name in type_names
        if name.rpartition(".")[2] in FORBIDDEN_LLM_DEPENDENCY_TYPES
        or name.startswith(FORBIDDEN_LLM_DEPENDENCY_MODULES)
    )


def _module_files(*parts: str) -> tuple[Path, ...]:
    return tuple(sorted(APP_DIR.joinpath(*parts).rglob("*.py")))


def _imported_modules(path: Path) -> set[str]:
    """文件内全部 ``import``／``from ... import`` 的绝对模块名（相对导入按文件所在包还原，不做别名解析）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ("app",) + path.relative_to(APP_DIR).parts[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module is not None:
                    names.add(node.module)
                continue
            prefix = package[: len(package) - node.level + 1]
            target = node.module or ""
            names.add(".".join((*prefix, target)) if target else ".".join(prefix))
    return names


def _violations(
    files: tuple[Path, ...], forbidden_roots: tuple[str, ...]
) -> list[str]:
    found: list[str] = []
    for path in files:
        for module in sorted(_imported_modules(path)):
            for root in forbidden_roots:
                if module == root or module.startswith(f"{root}."):
                    found.append(f"{path.relative_to(BACKEND_DIR)} → {module}")
    return found


def test_domain_depends_on_nothing_above_it() -> None:
    """domain 只允许引用 domain：无 infrastructure／application／api／组合根，也不出现 aiosqlite。"""
    found = _violations(
        _module_files("domain"),
        ("app.infrastructure", "app.application", "app.api", "app.bootstrap", "aiosqlite"),
    )
    assert found == []


def test_application_does_not_depend_on_transport_or_bootstrap() -> None:
    """application 只用 application／domain／端口与 infrastructure 持久化实现：无 api、组合根与 aiosqlite。"""
    found = _violations(
        _module_files("application"),
        ("app.api", "app.bootstrap", "aiosqlite"),
    )
    assert found == []


def test_infrastructure_does_not_depend_on_the_transport_layer() -> None:
    """infrastructure 只实现端口与领域约束：不得引用 api 或 application 服务层（装配由组合根负责）。"""
    found = _violations(
        _module_files("infrastructure"),
        ("app.api", "app.application.services"),
    )
    assert found == []


def test_api_does_not_depend_on_database_infrastructure() -> None:
    """传输层通过 Application Service 与 Port 使用数据库能力，不导入数据库基础设施。"""
    assert _violations(_module_files("api"), ("app.infrastructure.database",)) == []


def test_application_does_not_import_provider_sdks() -> None:
    """Provider SDK 只在 infrastructure／llm：application 不得直接导入 openai／anthropic。"""
    found = _violations(_module_files("application"), ("openai", "anthropic"))
    assert found == []


def test_no_module_imports_the_removed_legacy_layers() -> None:
    """全树零旧路径：迁移完成后不再存在 api／domain／graph／harness／storage／provider_settings 顶层包。"""
    found = _violations(_module_files(), LEGACY_ROOTS)
    assert found == []
    for root in LEGACY_ROOTS:
        assert not (BACKEND_DIR / root).exists(), f"旧层残留：{root}"


def test_planner_and_evaluator_nodes_have_no_write_dependency() -> None:
    """两个 LLM Node 只拿 ``PlanLlmNodeDeps``（模型 ＋ 只读事实边界）：不含 Repository、数据库／事务对象、
    业务写 Service 与 ``MemoryAssembler``。"""
    for node in (PlannerAgentNode, EvaluatorAgentNode):
        assert get_type_hints(node.__init__)["deps"] is PlanLlmNodeDeps

    assert _forbidden_dependency_hits(_type_names(PlanLlmNodeDeps)) == []


def test_memory_assembler_is_no_longer_a_plan_fact_source() -> None:
    """事实装配器已删除：Planner／Evaluator 的事实只经各自的 ToolNode 返回。"""
    assert not (APP_DIR / "application" / "agent" / "memory.py").exists()

