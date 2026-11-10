"""Run the real agent.py CLI with the fake model and a throwaway ledger.

    python tests/run_cli.py <ledger.db> -- <agent.py arguments>

Used by test_crash.py, which kills this process at awkward moments. FAST_LEDGER=1 removes
ledger.post's 2 s delay (the file itself is untouched; only its module is patched here).
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

from fakes import FakeModel  # noqa: E402

import agent  # noqa: E402
import ledger  # noqa: E402

ledger.DB_PATH = Path(sys.argv[1])
if os.environ.get("FAST_LEDGER"):
    ledger.time = SimpleNamespace(sleep=lambda s: None)
model = FakeModel("fake")
agent.PROVIDERS = {"claude": lambda: model, "openai": lambda: model}
sys.exit(agent.main(sys.argv[3:]))
