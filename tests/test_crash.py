"""Kill the real program at every awkward moment, restart it, and check what happened.

Each test runs agent.py in a subprocess (fake model, throwaway databases). AGENT_CRASH_AT
makes it call os._exit at a named point, which skips all cleanup like kill -9 would. One
test uses a real SIGKILL in the middle of ledger.post. After every crash the invoice is
posted at most once, and the conversation carries on.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "run_cli.py"


class Program:
    def __init__(self, tmp_path):
        self.ledger_db, self.agent_db = tmp_path / "ledger.db", tmp_path / "agent.db"

    def args(self):
        return [sys.executable, str(RUNNER), str(self.ledger_db), "--", "-c", "crash", "--db", str(self.agent_db)]

    def run(self, *lines, crash_at=None, fast=True):
        """Start the program, type the lines, and wait for it to exit (or die)."""
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        env.pop("AGENT_CRASH_AT", None)
        if crash_at:
            env["AGENT_CRASH_AT"] = crash_at
        if fast:
            env["FAST_LEDGER"] = "1"
        done = subprocess.run(
            self.args(), input="\n".join([*lines, "/quit"]) + "\n", env=env, capture_output=True, text=True, timeout=60
        )
        return done.returncode, done.stdout

    def posted(self, invoice_number="SGP/26-27/0412"):
        if not self.ledger_db.exists():
            return []
        with sqlite3.connect(self.ledger_db) as db:
            return db.execute("SELECT entry_id FROM entries WHERE invoice_number = ?", (invoice_number,)).fetchall()


@pytest.fixture
def prog(tmp_path):
    return Program(tmp_path)


def test_killed_before_the_model_answers(prog):
    code, _ = prog.run("post invoice-01", crash_at="before_model_call")
    assert code == 137 and prog.posted() == []
    code, out = prog.run()
    assert "The last turn didn't finish" in out and "Approval needed" not in out
    prog.run("post invoice-01", "/approve")
    assert len(prog.posted()) == 1


def test_killed_after_the_model_asked_to_post(prog):
    code, _ = prog.run("post invoice-01", crash_at="after_reply_saved")
    assert code == 137 and prog.posted() == []
    code, out = prog.run("/approve")  # the approval is shown again and can be approved
    assert "Approval needed" in out and "SGP/26-27/0412" in out
    assert len(prog.posted()) == 1


def test_killed_with_read_calls_open(prog):
    code, _ = prog.run("read invoice-06", crash_at="after_reply_saved")
    assert code == 137
    code, out = prog.run()  # reads have no side effects, so they simply run on restart
    assert "· read_invoice(invoice-06)" in out


def test_killed_after_approval_before_the_ledger(prog):
    code, _ = prog.run("post invoice-01", "/approve", crash_at="before_ledger_post")
    assert code == 137 and prog.posted() == []
    code, out = prog.run()
    assert "never reached the ledger" in out
    prog.run("post invoice-01", "/approve")  # asking again works, and needs approval again
    assert len(prog.posted()) == 1


def test_killed_after_the_ledger_returned_before_saving(prog):
    code, _ = prog.run("post invoice-01", "/approve", crash_at="after_ledger_post")
    assert code == 137 and len(prog.posted()) == 1  # posted, but we never saved the id
    code, out = prog.run("post invoice-01")
    (entry_id,) = prog.posted()[0]
    assert f"Recovered an interrupted post: SGP/26-27/0412 is in the ledger as {entry_id}" in out
    assert "already in the ledger" in out and "Approval needed" not in out
    assert len(prog.posted()) == 1


def test_killed_after_the_post_was_saved(prog):
    code, _ = prog.run("post invoice-01", "/approve", crash_at="after_post_saved")
    assert code == 137 and len(prog.posted()) == 1
    code, out = prog.run("post invoice-01")
    assert "Recovered" not in out and "already in the ledger" in out
    assert len(prog.posted()) == 1


def test_sigkill_in_the_middle_of_ledger_post(prog):
    """A real kill -9 while ledger.post is running, after it wrote the entry but before it returned."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    env.pop("AGENT_CRASH_AT", None)
    p = subprocess.Popen(prog.args(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env=env, text=True)
    p.stdin.write("post invoice-01\n/approve\n")
    p.stdin.flush()
    deadline = time.time() + 30
    while not prog.posted() and time.time() < deadline:  # ledger.post sleeps 1 s after writing
        time.sleep(0.02)
    os.kill(p.pid, signal.SIGKILL)
    p.wait()
    assert p.returncode == -signal.SIGKILL and len(prog.posted()) == 1

    code, out = prog.run("post invoice-01")
    assert "Recovered an interrupted post" in out and "already in the ledger" in out
    assert len(prog.posted()) == 1


def test_second_window_on_the_same_conversation_is_refused(prog):
    env = {**os.environ, "FAST_LEDGER": "1"}
    first = subprocess.Popen(prog.args(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True)
    first.stdout.readline()  # it has started and holds the lock
    code, out = prog.run("post invoice-01")
    assert code == 1 and "already open in another window" in out
    first.communicate("/quit\n", timeout=30)
    code, _ = prog.run()  # free again once the first one exits
    assert code == 0
