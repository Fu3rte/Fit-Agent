from dataclasses import dataclass

#: 阶段 1 定界默认值：首批工具都是按查询参数界定的只读短输出。
#: Pi DEFAULT_BOUNDS 为 64 KiB / 200 行，本阶段按只读事实输出取 32 KiB / 400 行。
DEFAULT_MAX_BYTES = 32768
DEFAULT_MAX_LINES = 400

#: 截断提示由 Harness 统一添加，最多一次；其字节与换行占用计入同一组上限。
TRUNCATION_NOTICE = "\n[输出已截断]"
_TRUNCATION_NOTICE_BYTES = len(TRUNCATION_NOTICE.encode("utf-8"))
_TRUNCATION_NOTICE_NEWLINES = TRUNCATION_NOTICE.count("\n")


@dataclass(frozen=True, slots=True)
class BoundedText:
    text: str
    truncated: bool


def bound_text(
    text: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_lines: int = DEFAULT_MAX_LINES,
) -> BoundedText:
    """保留 head：同时满足 UTF-8 字节上限与换行数上限，截断提示最多添加一次。"""
    if max_bytes < _TRUNCATION_NOTICE_BYTES:
        raise ValueError(
            f"max_bytes={max_bytes} 无法容纳完整截断提示，至少需要 {_TRUNCATION_NOTICE_BYTES} 字节"
        )
    if max_lines < _TRUNCATION_NOTICE_NEWLINES:
        raise ValueError(
            f"max_lines={max_lines} 无法容纳完整截断提示，至少需要 {_TRUNCATION_NOTICE_NEWLINES} 个换行"
        )
    if len(text.encode("utf-8")) <= max_bytes and text.count("\n") <= max_lines:
        return BoundedText(text=text, truncated=False)
    head = _retain_head(
        text,
        max_bytes - _TRUNCATION_NOTICE_BYTES,
        max_lines - _TRUNCATION_NOTICE_NEWLINES,
    )
    return BoundedText(text=head + TRUNCATION_NOTICE, truncated=True)


def _retain_head(text: str, max_bytes: int, max_lines: int) -> str:
    """取头部：字节上限按 UTF-8 边界截断，换行上限保留至第 max_lines 个换行。"""
    head = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    if max_lines <= 0:
        return head.partition("\n")[0]
    newlines = 0
    for index, char in enumerate(head):
        if char != "\n":
            continue
        newlines += 1
        if newlines == max_lines:
            return head[: index + 1]
    return head
