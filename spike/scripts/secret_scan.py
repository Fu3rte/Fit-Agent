# Fit-Agent PydanticAI spike：秘密扫描（不打印任何匹配值）。
# 扫描范围：spike 下源码/证据/脚本（排除 .venv、__pycache__、.pytest_cache）。
# 规则：凭据形态字符串（sk- 前缀长 token）、Key 字面量赋值、日志/打印中可能的 Key。
# 唯一白名单：合成占位符常量（明确标注非凭据）。发现即退出码 1，只输出文件与行号。

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache"}
WHITELIST = {"dummy-not-a-credential"}

PATTERNS = {
    "sk_token": re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    "api_key_literal": re.compile(r"api_key\s*=\s*[\"']([^\"']{16,})[\"']"),
    "bearer_header": re.compile(r"Bearer\s+[A-Za-z0-9_-]{20,}"),
}


def scan() -> int:
    findings: list[tuple[Path, int, str]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if EXCLUDE_DIRS & set(path.parts):
            continue
        if path.suffix not in {".py", ".json", ".md", ".toml", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                value = match.group(0)
                if any(w in value for w in WHITELIST):
                    continue
                line_no = text.count("\n", 0, match.start()) + 1
                findings.append((path, line_no, name))
    if findings:
        print(f"SECRET SCAN FAIL: {len(findings)} finding(s) (values suppressed)")
        for path, line_no, name in findings:
            print(f"  {path.relative_to(ROOT)}:{line_no}: {name}")
        return 1
    print("SECRET SCAN OK: no credential-shaped findings (values suppressed)")
    return 0


if __name__ == "__main__":
    sys.exit(scan())
