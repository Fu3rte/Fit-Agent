import os
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = Path(
    os.environ.get("FIT_AGENT_B6_TEST_OUTPUT_ROOT", BACKEND / "temp" / "v2-test-output")
).resolve()
