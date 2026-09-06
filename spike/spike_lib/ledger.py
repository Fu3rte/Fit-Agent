# Fit-Agent PydanticAI spike：spike 专用持久账本（PLAN 已拍决策 A，2026-09-06）。
# 要求：请求前落盘预留；重启保留未结算金额；账本损坏或并发启动即拒绝；支持安全分批验收。
# 实现：PersistentFeeGuard 子类化 FeeGuard（核心记账逻辑不改，只加落盘时机与加载校验）。
# 锁：fcntl.flock 独占（POSIX 专用；spike 运行环境为 Linux）——第二进程启动即拒绝，
# 进程崩溃自动释放锁，但账本文件保留未结算预留（保守，不自动清零）。
# 落盘：临时文件 + fsync + os.replace 原子替换；崩溃残留的 .tmp 在加载时忽略。

from __future__ import annotations

import fcntl
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from spike_lib.fee_guard import (
    HARD_CAP_USD,
    MAX_TOKENS_LIMIT,
    CallRecord,
    FeeGuard,
    FeeModel,
    Reservation,
    StopSpike,
)

LEDGER_VERSION = 1
_KNOWN_MODELS = {m.value for m in FeeModel}


class LedgerCorrupt(RuntimeError):
    """账本文件损坏或非法：拒绝启动，绝不覆盖。"""


class LedgerLocked(RuntimeError):
    """账本已被其他进程占用：拒绝并发启动。"""


class LedgerWriteError(RuntimeError):
    """账本落盘失败（写入/替换环节）。"""


class PersistentFeeGuard(FeeGuard):
    """带持久化的 FeeGuard。落盘时机：
    - reserve：super().reserve() 完成内存变更后、返回前落盘（wire 请求在 reserve 返回后才发出，
      因此预留必然先于任何真实请求落盘）。
    - settle/cancel/stop：super() 完成全部内存变更（含异常路径上的记录与停止位）后落盘；
      finally 保证阈值停止等异常路径同样落盘。"""

    def __init__(self, path: str | os.PathLike) -> None:
        super().__init__()
        self._path = Path(path)
        self._lock_fd = os.open(str(self._path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._lock_fd)
            raise LedgerLocked(f"账本已被其他进程占用，拒绝并发启动：{self._path}") from exc
        try:
            if self._path.exists():
                self._load()
            else:
                self._save()  # 首次建立账本文件（持锁后即刻落盘，确立归属）
        except BaseException:
            os.close(self._lock_fd)
            raise

    # ---- 落盘时机包装 ----
    def reserve(
        self,
        model: str,
        *,
        input_tokens_reserve: int,
        max_output: int = MAX_TOKENS_LIMIT,
    ) -> Reservation:
        res = super().reserve(model, input_tokens_reserve=input_tokens_reserve, max_output=max_output)
        self._save_guarded()
        return res

    def settle(self, res: Reservation, usage_raw: dict | None, *, now_utc: Any, model: str) -> Decimal:
        try:
            return super().settle(res, usage_raw, now_utc=now_utc, model=model)
        finally:
            self._save_guarded()  # 异常路径（校验失败/超预留/自动停）同样落盘

    def cancel(self, res: Reservation, reason: str) -> None:
        super().cancel(res, reason)
        self._save_guarded()

    def stop(self, reason: str) -> None:
        super().stop(reason)
        self._save_guarded()

    def _save_guarded(self) -> None:
        try:
            self._save()
        except (OSError, LedgerWriteError) as exc:
            # 落盘失败 = 无法保证请求前预留已持久化：立即停止，拒绝后续一切调用（fail-closed）。
            if self.stopped is None:
                self.stopped = f"账本落盘失败，停止：{exc}"
            raise StopSpike(f"账本落盘失败：{exc}") from exc

    def close(self) -> None:
        """释放锁（进程退出也会自动释放；主要供测试内显式重启语义使用）。"""
        os.close(self._lock_fd)

    # ---- 序列化 / 校验 ----
    def _save(self) -> None:
        state = {
            "version": LEDGER_VERSION,
            "settled_usd": str(self.settled_usd),
            "reserved_usd": str(self.reserved_usd),
            "stopped": self.stopped,
            "calls": [
                {
                    "model": c.model,
                    "reserved_usd": str(c.reserved_usd),
                    "settled_usd": None if c.settled_usd is None else str(c.settled_usd),
                    "usage_raw": c.usage_raw,
                    "expected_min_usd": None if c.expected_min_usd is None else str(c.expected_min_usd),
                    "note": c.note,
                }
                for c in self.calls
            ],
        }
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            raise LedgerWriteError(f"账本写入失败：{self._path} ({exc})") from exc

    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
            state = json.loads(text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerCorrupt(f"账本无法读取/解析，拒绝启动且不覆盖：{self._path} ({exc})") from exc
        if not isinstance(state, dict):
            raise LedgerCorrupt("账本顶层须为对象")
        if state.get("version") != LEDGER_VERSION or isinstance(state.get("version"), bool):
            raise LedgerCorrupt(f"账本版本非法：{state.get('version')!r}")
        if set(state) != {"version", "settled_usd", "reserved_usd", "stopped", "calls"}:
            raise LedgerCorrupt(f"账本字段集非法：{sorted(state)}")
        settled = _req_dec(state["settled_usd"], "settled_usd")
        reserved = _req_dec(state["reserved_usd"], "reserved_usd")
        if settled + reserved > HARD_CAP_USD:
            raise LedgerCorrupt(f"账本总占用 ${settled + reserved} 超过硬顶 ${HARD_CAP_USD}，拒绝启动")
        stopped = state["stopped"]
        if stopped is not None and not isinstance(stopped, str):
            raise LedgerCorrupt(f"账本 stopped 字段非法：{stopped!r}")
        calls: list[CallRecord] = []
        if not isinstance(state["calls"], list):
            raise LedgerCorrupt("账本 calls 须为数组")
        for i, entry in enumerate(state["calls"], 1):
            if not isinstance(entry, dict) or set(entry) != {
                "model",
                "reserved_usd",
                "settled_usd",
                "usage_raw",
                "expected_min_usd",
                "note",
            }:
                raise LedgerCorrupt(f"第 {i} 条账目字段集非法")
            if entry["model"] not in _KNOWN_MODELS:
                raise LedgerCorrupt(f"第 {i} 条账目模型未知：{entry['model']!r}")
            if entry["usage_raw"] is not None and not isinstance(entry["usage_raw"], dict):
                raise LedgerCorrupt(f"第 {i} 条账目 usage_raw 非法")
            if entry["note"] is not None and not isinstance(entry["note"], str):
                raise LedgerCorrupt(f"第 {i} 条账目 note 非法")
            calls.append(
                CallRecord(
                    model=entry["model"],
                    reserved_usd=_req_dec(entry["reserved_usd"], f"calls[{i}].reserved_usd"),
                    settled_usd=_opt_dec(entry["settled_usd"], f"calls[{i}].settled_usd"),
                    usage_raw=entry["usage_raw"],
                    expected_min_usd=_opt_dec(entry["expected_min_usd"], f"calls[{i}].expected_min_usd"),
                    note=entry["note"],
                )
            )
        self.settled_usd = settled
        self.reserved_usd = reserved
        self.stopped = stopped
        self.calls = calls


def _req_dec(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise LedgerCorrupt(f"账本字段 {name} 非法（须为十进制字符串）：{value!r}")
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise LedgerCorrupt(f"账本字段 {name} 非法（无法解析为 Decimal）：{value!r}") from exc
    if not d.is_finite() or d < 0:
        raise LedgerCorrupt(f"账本字段 {name} 非法（须为非负有限数）：{value!r}")
    return d


def _opt_dec(value: object, name: str) -> Decimal | None:
    return None if value is None else _req_dec(value, name)
