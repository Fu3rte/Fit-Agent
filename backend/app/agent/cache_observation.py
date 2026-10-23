import argparse
import json
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from time import time_ns
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from app.agent.telemetry import TraceSpan
from app.ai.messages import Model, Usage
from app.ai.types import ModelSpec

# 调用用途：普通调用、历史摘要、任务前缀摘要。比较链按会话与用途隔离。
NORMAL_CALL = "normal"
HISTORY_SUMMARY_CALL = "history_summary"
TURN_PREFIX_SUMMARY_CALL = "turn_prefix_summary"
NORMAL_PURPOSE = "normal"
SUMMARY_PURPOSE = "summary"

# 汇总分类：首次调用、普通追加、摘要调用、重置后的调用。
FIRST_CALL = "first"
APPEND_CALL = "append"
SUMMARY_CALL = "summary"
RESET_CALL = "reset"

# 已知重置事件：这些原因之后的调用不再复用此前前缀。
RESET_EVENTS = frozenset(
    {"compaction", "model_switch", "edit", "branch", "overflow_recovery"}
)

# 真实调用结局到模型 Span 终结状态与 stop_reason 的映射：stop_reason 仅取五种合法值。
_CACHE_STATUS_TO_SPAN: dict[str, tuple[str, str | None]] = {
    "stop": ("completed", "stop"),
    "length": ("completed", "length"),
    "toolUse": ("completed", "toolUse"),
    "error": ("failed", "error"),
    "aborted": ("cancelled", "aborted"),
    "failed": ("failed", None),
}


def observation_directory() -> Path:
    # 诊断产物写入已配置 gitignore 的本地目录。
    return Path(__file__).resolve().parents[2] / "data" / "cache-observation"


def sanitize_endpoint(url: str) -> str:
    # 记录 endpoint 前丢弃 userinfo、query 与 fragment，避免地址携带凭据或敏感参数。
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def extract_projection(request: dict, api: str) -> tuple[str, str, list[str]]:
    # 基于适配器实际发送的最终投影计算指纹：协议消息逐条哈希，system 与工具声明单独哈希。
    # timestamp 不进入协议投影，因此仅时间变化保持指纹稳定；数组与内容块顺序保持不变。
    messages = list(request.get("messages") or [])
    if api == "anthropic-messages":
        system = request.get("system")
    else:
        system = None
        if messages and messages[0].get("role") == "system":
            system = messages.pop(0)
    tools = request.get("tools")
    system_hash = _digest(system) if system is not None else ""
    tools_hash = _digest(tools) if tools else ""
    return system_hash, tools_hash, [_digest(message) for message in messages]


def first_difference(previous: Sequence[str], current: Sequence[str]) -> int | None:
    # 返回首个差异位置：前缀保持返回空，出现截断返回当前长度。
    for index, (left, right) in enumerate(zip(previous, current)):
        if left != right:
            return index
    if len(current) < len(previous):
        return len(current)
    return None


def _purpose(call_kind: str) -> str:
    return NORMAL_PURPOSE if call_kind == NORMAL_CALL else SUMMARY_PURPOSE


def _chain_key(session_id: str, purpose: str) -> str:
    return f"{session_id}|{purpose}"


class ModelCallObservation(Model):
    # 一次真实模型调用的观测记录：只持久化最小必要的指纹、位置、身份与用量。
    observation_id: str
    recorded_at: int
    api: str
    provider: str
    model: str
    endpoint: str
    session_id: str
    run_id: str
    message_id: str | None
    compaction_id: str | None
    call_kind: str
    call_index: int
    category: str
    system_hash: str
    tools_hash: str
    message_fingerprints: list[str]
    first_difference_index: int | None
    known_events: list[str]
    status: str
    input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    failure: dict | None = None


class CallObservationHandle:
    # 单次真实调用的观测句柄：observe 记录最终投影，finish/fail 记录可核实的结局与用量。
    def __init__(
        self,
        owner: "CacheObserver",
        *,
        call_kind: str,
        purpose: str,
        message_id: str | None,
        compaction_id: str | None,
        call_index: int,
        events: list[str],
        span: TraceSpan | None = None,
    ) -> None:
        self._owner = owner
        self._call_kind = call_kind
        self._purpose = purpose
        self._message_id = message_id
        self._compaction_id = compaction_id
        self._call_index = call_index
        self._events = events
        self._span = span
        self._record: ModelCallObservation | None = None
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    def record_first_text(self) -> None:
        # 首个非空正文增量：复用同一模型 Span 的首次写入语义。
        if self._span is not None:
            self._span.record_first_text()

    def observe(self, request: dict, model: ModelSpec) -> None:
        if self._record is not None:
            raise ValueError("同一次真实调用不能重复观测请求")
        owner = self._owner
        system_hash, tools_hash, fingerprints = extract_projection(request, model.api)
        previous = owner.chains.get(_chain_key(owner.session_id, self._purpose))
        events = list(self._events)
        difference: int | None = None
        if self._call_kind != NORMAL_CALL:
            category = SUMMARY_CALL
        elif previous is None:
            category = FIRST_CALL
        else:
            difference = first_difference(previous["message_fingerprints"], fingerprints)
            if previous["system_hash"] != system_hash:
                events.append("system_change")
            if previous["tools_hash"] != tools_hash:
                events.append("tools_change")
            if (previous["provider"], previous["model"]) != (model.provider, model.id):
                events.append("model_switch")
            category = (
                RESET_CALL
                if difference == 0 or (RESET_EVENTS & set(events))
                else APPEND_CALL
            )
        observation_id = uuid4().hex
        self._record = ModelCallObservation(
            observation_id=observation_id,
            recorded_at=time_ns() // 1_000_000,
            api=model.api,
            provider=model.provider,
            model=model.id,
            endpoint=sanitize_endpoint(model.base_url),
            session_id=owner.session_id,
            run_id=owner.run_id,
            message_id=self._message_id,
            compaction_id=self._compaction_id,
            call_kind=self._call_kind,
            call_index=self._call_index,
            category=category,
            system_hash=system_hash,
            tools_hash=tools_hash,
            message_fingerprints=fingerprints,
            first_difference_index=difference,
            known_events=list(dict.fromkeys(events)),
            status="pending",
        )
        owner.record_chain(
            _chain_key(owner.session_id, self._purpose),
            model,
            system_hash,
            tools_hash,
            fingerprints,
            observation_id,
        )

    def finish(self, usage: Usage | None, status: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._write(usage, status)

    def fail(self, aborted: bool, error_info: dict | None = None) -> None:
        # 已发送但未取得完整结局：保留可核实状态，缺失用量保持缺失。
        if self._finished:
            return
        self._finished = True
        self._write(None, "aborted" if aborted else "failed", error_info)

    def _write(self, usage: Usage | None, status: str,
               failure: dict | None = None) -> None:
        record = self._record
        if record is None:
            # 未观测到最终投影（发送前取消或请求转换失败）：不产生记录。
            return
        if usage is not None:
            record = record.model_copy(update={
                "input_tokens": int(usage.input),
                "cache_read_tokens": int(usage.cache_read),
                "cache_write_tokens": int(usage.cache_write),
                "output_tokens": int(usage.output),
            })
        update: dict = {"status": status}
        if failure is not None:
            update["failure"] = failure
        self._record = record.model_copy(update=update)
        try:
            self._owner.persist(self._record)
        finally:
            # 持久化属纯观测操作：失败后仍向模型 Span 补充真实状态与用量。
            self._report_span(usage, status)

    def _report_span(self, usage: Usage | None, status: str) -> None:
        # 向对应模型 Span 补充真实结局与用量：只推送属性白名单允许的字段，未知值保持省略。
        span = self._span
        if span is None:
            return
        span_status, stop_reason = _CACHE_STATUS_TO_SPAN.get(status, ("failed", None))
        attributes: dict[str, object] = {}
        if stop_reason is not None:
            attributes["stop_reason"] = stop_reason
        if usage is not None:
            attributes["input_tokens"] = int(usage.input)
            attributes["output_tokens"] = int(usage.output)
            attributes["cache_read_tokens"] = int(usage.cache_read)
            attributes["cache_write_tokens"] = int(usage.cache_write)
        span.set_attributes(attributes)
        span.set_status(span_status)


class CacheObserver:
    # 运行级观测：为每次真实模型调用生成记录，按会话与用途维护比较链，重启后依据已存记录衔接。
    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        directory: Path | str | None = None,
        contains: Callable[[object], bool] | None = None,
    ) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self._directory = (
            Path(directory) if directory is not None else observation_directory()
        )
        self._contains = contains
        self._call_index = 0
        self._pending: list[str] = []
        self.chains: dict[str, dict] = self._load_chains()

    def _path(self) -> Path:
        return self._directory / f"{self.session_id}.jsonl"

    def _load_chains(self) -> dict[str, dict]:
        # 读取与解析属纯观测操作：文件缺失、读取或解析故障时按无历史比较链继续。
        chains: dict[str, dict] = {}
        with suppress(Exception):
            path = self._path()
            if not path.exists():
                return chains
            for record in _read_records(path):
                if record.session_id != self.session_id:
                    continue
                self._call_index = max(self._call_index, record.call_index)
                chains[_chain_key(record.session_id, _purpose(record.call_kind))] = {
                    "system_hash": record.system_hash,
                    "tools_hash": record.tools_hash,
                    "provider": record.provider,
                    "model": record.model,
                    "message_fingerprints": record.message_fingerprints,
                    "observation_id": record.observation_id,
                }
        return chains

    def record_chain(
        self,
        key: str,
        model: ModelSpec,
        system_hash: str,
        tools_hash: str,
        fingerprints: list[str],
        observation_id: str,
    ) -> None:
        self.chains[key] = {
            "system_hash": system_hash,
            "tools_hash": tools_hash,
            "provider": model.provider,
            "model": model.id,
            "message_fingerprints": fingerprints,
            "observation_id": observation_id,
        }

    def note_event(self, tag: str) -> None:
        # 缓冲实际发生的上下文事件，交予下一次真实调用记录。
        self._pending.append(tag)

    def start_call(
        self,
        call_kind: str,
        *,
        message_id: str | None = None,
        compaction_id: str | None = None,
        span: TraceSpan | None = None,
    ) -> CallObservationHandle:
        if call_kind not in (NORMAL_CALL, HISTORY_SUMMARY_CALL, TURN_PREFIX_SUMMARY_CALL):
            raise ValueError(f"未知调用用途: {call_kind}")
        self._call_index += 1
        events = self._pending
        self._pending = []
        return CallObservationHandle(
            self,
            call_kind=call_kind,
            purpose=_purpose(call_kind),
            message_id=message_id,
            compaction_id=compaction_id,
            call_index=self._call_index,
            events=events,
            span=span,
        )

    def persist(self, observation: ModelCallObservation) -> None:
        # 写入属纯观测操作：目录创建、序列化与写入的普通故障被隔离。
        # 命中凭据或敏感信息时阻止落盘；检查本身失败同样按拒绝处理，保持白名单边界。
        with suppress(Exception):
            payload = observation.model_dump()
            if self._contains is not None and self._contains(payload):
                return
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n"
                )


def _read_records(path: Path) -> list[ModelCallObservation]:
    records: list[ModelCallObservation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(ModelCallObservation.model_validate(json.loads(line)))
    return records


def read_observations(directory: Path | str | None = None) -> list[ModelCallObservation]:
    root = Path(directory) if directory is not None else observation_directory()
    if not root.exists():
        return []
    records: list[ModelCallObservation] = []
    for path in sorted(root.glob("*.jsonl")):
        records.extend(_read_records(path))
    return records


def _usage_totals(records: Sequence[ModelCallObservation]) -> dict:
    # 一组真实调用的用量与命中率：先求用量总和再计算比例，保留有效用量数与缺失用量数。
    available = [record for record in records if record.input_tokens is not None]
    input_total = sum(record.input_tokens or 0 for record in available)
    read_total = sum(record.cache_read_tokens or 0 for record in available)
    write_total = sum(record.cache_write_tokens or 0 for record in available)
    output_total = sum(record.output_tokens or 0 for record in available)
    # 命中率统一口径：cache_read / (input + cache_read + cache_write)。
    # cache_write_1h 已包含在 cache_write 内，分母只计入一次。
    denominator = input_total + read_total + write_total
    return {
        "calls": len(records),
        "usage_available_calls": len(available),
        "usage_missing_calls": len(records) - len(available),
        "input_tokens": input_total,
        "cache_read_tokens": read_total,
        "cache_write_tokens": write_total,
        "output_tokens": output_total,
        # 零分母表示指标不可计算。
        "hit_rate": (read_total / denominator) if denominator > 0 else None,
    }


def _bucket(records: Sequence[ModelCallObservation]) -> dict:
    # 整体汇总，并按分类独立汇总真实用量与命中率。
    result = _usage_totals(records)
    result["categories"] = {
        name: _usage_totals([record for record in records if record.category == name])
        for name in (FIRST_CALL, APPEND_CALL, SUMMARY_CALL, RESET_CALL)
    }
    return result


def summarize_observations(records: Iterable[ModelCallObservation]) -> dict:
    # 汇总基于原始真实调用记录：按 observation_id 去重，先求用量总和再计算命中率。
    unique: dict[str, ModelCallObservation] = {}
    for record in records:
        unique[record.observation_id] = record
    ordered = sorted(
        unique.values(), key=lambda item: (item.session_id, item.call_index)
    )
    groups: dict[tuple[str, str], list[ModelCallObservation]] = {}
    for record in ordered:
        groups.setdefault((record.model, record.endpoint), []).append(record)
    return {
        "calls": len(ordered),
        "totals": _bucket(ordered),
        "groups": [
            {"model": key[0], "endpoint": key[1], **_bucket(items)}
            for key, items in sorted(groups.items())
        ],
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="读取并汇总本地模型调用缓存观测记录。"
    )
    parser.add_argument("--directory", default=None, help="观测目录，默认使用配置目录。")
    parser.add_argument("--session", default=None, help="只汇总指定会话。")
    args = parser.parse_args(argv)
    records = read_observations(args.directory)
    if args.session:
        records = [record for record in records if record.session_id == args.session]
    print(json.dumps(summarize_observations(records), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
