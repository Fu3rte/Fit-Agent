import json
import os
import sys
from pathlib import Path

import pytest

from app.application.business.retrieval_embedding import EmbeddingClient

ATTRS = ("_v2_recorder_owner", "_v2_original_embed", "_v2_recorder_installed")


def snapshot():
    state = {}
    for name in ATTRS:
        if hasattr(EmbeddingClient, name):
            state[name] = [True, id(getattr(EmbeddingClient, name))]
        else:
            state[name] = [False, None]
    return state


def main():
    output = Path(os.environ["EMBEDDING_CLASS_STATE_RESULT"])
    root = Path(os.environ["FIT_AGENT_B6_TEST_OUTPUT_ROOT"])
    before = snapshot()
    code = pytest.main([
        "-q",
        "test/test_v2_metrics.py::test_embedding_recorder_settles_real_usage_and_is_idempotent",
        "test/test_v2_metrics.py::test_embedding_recorder_class_state_teardown[absent]",
        "test/test_v2_metrics.py::test_embedding_recorder_class_state_teardown[present]",
        f"--basetemp={root / 'pt-embedding-check'}",
        "-o", f"cache_dir={root / 'cache-embedding-check'}",
    ])
    after = snapshot()
    result = {"exit_code": int(code), "before": before, "after": after, "equal": before == after}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if code == 0 and before == after else 1


if __name__ == "__main__":
    sys.exit(main())
