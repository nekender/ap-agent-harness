"""Run the eval scenarios in evals/cases.py against real models, through the real Agent.

    python evals/run_evals.py --providers claude,openai --reps 3          # the full run
    python evals/run_evals.py --cases q1_read_one,p5_post_all --reps 1 --out evals/results/pilot  # a pilot
    python evals/run_evals.py --providers null                           # grader sanity check, free

Each (case, provider, rep) runs in its own subprocess with its own fresh ledger and agent
database, so runs can't interfere and a hung one can be killed (--timeout). Results are
appended to <out>/results.jsonl as each run finishes, and re-running the same command skips
what's already there. Failed runs go to errors.jsonl. Transcripts go to traces/.
Costs are worked out from the token usage and the model each response says it came from.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(HERE)]

from cases import CASES  # noqa: E402

# USD per million tokens: (input, output). Cache reads bill at 0.1x input; Claude cache writes at 1.25x.
PRICES = {"claude-opus-5-5": (4.00, 20.00), "gpt-5-mini": (0.25, 2.00)}
OTHER = {"claude": "openai", "openai": "claude"}


# -- one run (inside its own subprocess) ---------------------------------------------------------


class Run:
    """What one case produced: the saved history, the approvals shown, and the ledger after."""

    def __init__(self, history, approvals, entries):
        self.history, self.approvals, self.entries = history, approvals, entries

    def answer(self, i=-1):
        """Everything the agent said after the i-th clerk message, digits without thousands commas."""
        starts = [k for k, m in enumerate(self.history) if m["role"] == "user"]
        start = starts[i]
        end = starts[i + 1] if i != -1 and i + 1 < len(starts) else len(self.history)
        text = "\n".join(m["text"] for m in self.history[start:end] if m["role"] == "assistant" and m["text"])
        return re.sub(r"(?<=\d),(?=\d)", "", text)

    def post_calls(self):
        return [
            c for m in self.history if m["role"] == "assistant" for c in m["tool_calls"] if c["name"] == "post_invoice"
        ]

    def new_entries(self):
        return [e["invoice_number"] for e in self.entries if e["entry_id"] != "JE-000001"]

    def new_entry_ids(self):
        return [e["entry_id"] for e in self.entries if e["entry_id"] != "JE-000001"]


class NullModel:
    """Always says it doesn't know. Every case should fail with it: a check on the graders."""

    def complete(self, history, system, tools, cancel, notify):
        return {"role": "assistant", "text": "I don't know.", "tool_calls": []}


def run_one(case_id: str, provider: str, workdir: Path) -> dict:
    from fakes import FakeModel

    import agent as agent_mod
    import ledger
    import providers
    import tools
    from store import Store

    case = next(c for c in CASES if c["id"] == case_id)
    ledger.DB_PATH = workdir / "ledger.db"
    invoices = workdir / "invoices"
    shutil.copytree(tools.INVOICES_DIR, invoices)
    for name, body in case.get("extra_invoices", {}).items():
        (invoices / f"{name}.json").write_text(json.dumps(body, indent=2))
    tools.INVOICES_DIR = invoices

    factories = {**providers.PROVIDERS, "null": NullModel, "fake": FakeModel}
    store = Store(workdir / "agent.db")
    store.open_conversation("eval", provider)
    out: list[str] = []
    agent = agent_mod.Agent(store, "eval", provider, providers=factories, out=out.append)
    approvals = []
    show = agent.show_approval
    agent.show_approval = lambda d: (approvals.append(d), show(d))

    def approve():
        # A model that never asked to post simply fails the case's checks: that's a model
        # result, not a harness error, so the run carries on and gets graded.
        if not agent.awaiting:
            out.append("[no approval was waiting]")
            return
        agent.approve()
        agent.advance()

    started = time.time()
    for step in case["steps"]:
        if step[0] == "say":
            agent.record({"role": "user", "text": step[1]})
            agent.advance()
        elif step[0] == "approve":
            approve()
        elif step[0] == "approve_all":
            for _ in range(10):
                if not agent.awaiting:
                    break
                approve()
        elif step[0] == "decline":
            if not agent.awaiting:
                out.append("[no approval was waiting]")
                continue
            agent.decline(step[1])
            agent.advance()
        elif step[0] == "switch":
            agent.switch(OTHER.get(agent.provider_name, agent.provider_name))
    latency = time.time() - started

    run = Run(agent.history, approvals, ledger.entries())
    grade = {}
    for name, check in case["checks"].items():
        try:
            grade[name] = bool(check(run))
        except Exception as exc:  # a check that crashes is a failed check, with the reason kept
            grade[name] = False
            out.append(f"[check {name} raised {exc!r}]")

    usage, models = defaultdict(lambda: defaultdict(int)), []
    for m in agent.history:
        if m["role"] == "assistant" and "meta" in m:
            models.append(m["meta"]["model"])
            for k, v in m["meta"]["usage"].items():
                usage[m["meta"]["model"]][k] += v

    return {
        "grade": grade,
        "pass": all(grade.values()),
        "latency_s": round(latency, 1),
        "model_calls": sum(1 for m in agent.history if m["role"] == "assistant"),
        "tool_calls": sum(len(m["tool_calls"]) for m in agent.history if m["role"] == "assistant"),
        "approvals_shown": len(approvals),
        "models": sorted(set(models)),
        "usage": {k: dict(v) for k, v in usage.items()},
        "retries": sum("retrying" in line for line in out),
        "trace": trace(agent.history, agent_mod.SYSTEM_PROMPT),
        "output": out,
    }


def trace(history, system):
    turns = [{"role": "system", "content": system}]
    for m in history:
        if m["role"] == "user":
            turns.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            if m["text"]:
                turns.append({"role": "assistant", "content": m["text"], "model": m.get("meta", {}).get("model")})
            for c in m["tool_calls"]:
                turns.append({"role": "tool_call", "name": c["name"], "content": json.dumps(c["args"], indent=2)})
        elif m["role"] == "tool":
            turns.append({"role": "tool_result", "name": m["name"], "content": json.dumps(m["result"])})
    return turns


def child_main(case_id, provider, out_file):
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            result = run_one(case_id, provider, Path(tmp))
        except Exception as exc:
            result = {"error_class": "provider_or_harness_error", "error": f"{type(exc).__name__}: {exc}"}
    Path(out_file).write_text(json.dumps(result))


# -- the runner ------------------------------------------------------------------------------------


def cost_usd(usage_by_model: dict) -> float | None:
    total = 0.0
    for model, u in usage_by_model.items():
        price = next((p for prefix, p in PRICES.items() if model.startswith(prefix)), None)
        if price is None:
            return None
        pin, pout = price[0] / 1e6, price[1] / 1e6
        total += u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout
        total += u.get("cache_read_input_tokens", 0) * pin * 0.1 + u.get("cache_creation_input_tokens", 0) * pin * 1.25
    return total


def wilson(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = passes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--providers", default="claude,openai")
    parser.add_argument("--cases", default="all", help="comma-separated case ids, or all")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=420, help="wall-clock ceiling per run, seconds")
    parser.add_argument("--out", default=str(HERE / "results" / "baseline"))
    parser.add_argument("--one", nargs=3, metavar=("CASE", "PROVIDER", "OUT_FILE"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.one:
        child_main(*args.one)
        return 0

    out = Path(args.out)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    results, errors = out / "results.jsonl", out / "errors.jsonl"
    case_ids = [c["id"] for c in CASES] if args.cases == "all" else args.cases.split(",")
    done = {(r["case"], r["provider"], r["rep"]) for r in load_rows(results)}
    jobs = [
        (c, p, rep)
        for c in case_ids
        for p in args.providers.split(",")
        for rep in range(1, args.reps + 1)
        if (c, p, rep) not in done
    ]
    print(f"{len(jobs)} runs to do ({len(done)} already in {results}), {args.workers} at a time")
    lock = threading.Lock()
    started = time.time()

    def do(job):
        case_id, provider, rep = job
        tags = next(c["tags"] for c in CASES if c["id"] == case_id)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            result_file = f.name
        t0 = time.time()
        try:
            subprocess.run(
                [sys.executable, __file__, "--one", case_id, provider, result_file],
                timeout=args.timeout,
                capture_output=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            result = json.loads(Path(result_file).read_text() or "{}") or {"error_class": "crashed", "error": ""}
        except subprocess.TimeoutExpired:
            result = {"error_class": "timeout", "error": f"over {args.timeout}s"}
        finally:
            os.unlink(result_file)
        key = {"case": case_id, "provider": provider, "rep": rep, "tags": tags}
        with lock:
            if "error_class" in result:
                with errors.open("a") as f:
                    f.write(json.dumps({**key, **result, "wall_s": round(time.time() - t0, 1)}) + "\n")
                print(f"  ERROR {case_id} {provider} rep{rep}: {result['error_class']} {result['error'][:120]}")
                return
            name = f"{case_id}__{provider}__rep{rep}.json"
            (out / "traces" / name).write_text(json.dumps(result.pop("trace"), indent=2))
            row = {**key, **result, "cost_usd": cost_usd(result["usage"]), "trace": f"traces/{name}"}
            with results.open("a") as f:
                f.write(json.dumps(row) + "\n")
            failed = [k for k, v in row["grade"].items() if not v]
            cost = f"${row['cost_usd']:.4f}" if row["cost_usd"] is not None else "$?"
            print(
                f"  {'PASS' if row['pass'] else 'FAIL'} {case_id} {provider} rep{rep}  "
                f"{row['latency_s']}s {cost} {', '.join(failed)}"
            )

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(do, jobs))
    print(f"done in {time.time() - started:.0f}s\n")
    summary = summarise(load_rows(results), load_rows(errors))
    (out / "summary.md").write_text(summary)
    print(summary)
    return 0


def summarise(rows: list[dict], errors: list[dict]) -> str:
    lines = ["| provider | pass rate (95% CI) | runs | errors | cost | median latency |", "|---|---|---|---|---|---|"]
    for p in sorted({r["provider"] for r in rows} | {e["provider"] for e in errors}):
        pr = [r for r in rows if r["provider"] == p]
        n, k = len(pr), sum(r["pass"] for r in pr)
        lo, hi = wilson(k, n)
        cost = sum(r["cost_usd"] or 0 for r in pr)
        lat = sorted(r["latency_s"] for r in pr)
        median = f"{lat[len(lat) // 2]}s" if lat else "-"
        rate = f"{k}/{n} = {k / n:.0%} ({lo:.0%}-{hi:.0%})" if n else "-"
        n_err = sum(e["provider"] == p for e in errors)
        lines.append(f"| {p} | {rate} | {n} | {n_err} | ${cost:.2f} | {median} |")

    lines += ["", "| case | " + " | ".join(sorted({r["provider"] for r in rows})) + " | failed checks |"]
    providers = sorted({r["provider"] for r in rows})
    lines.append("|---" * (len(providers) + 2) + "|")
    for c in [c["id"] for c in CASES]:
        cr = [r for r in rows if r["case"] == c]
        if not cr:
            continue
        cells = []
        for p in providers:
            pr = [r for r in cr if r["provider"] == p]
            cells.append(f"{sum(r['pass'] for r in pr)}/{len(pr)}" if pr else "-")
        fails = defaultdict(int)
        for r in cr:
            for check, ok in r["grade"].items():
                if not ok:
                    fails[f"{check} ({r['provider']})"] += 1
        lines.append(f"| {c} | " + " | ".join(cells) + " | " + ", ".join(f"{k} x{v}" for k, v in fails.items()) + " |")

    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    if costs:
        lines += [
            "",
            f"Per run cost: min ${min(costs):.4f}, median ${sorted(costs)[len(costs) // 2]:.4f}, "
            f"max ${max(costs):.4f}. Total ${sum(costs):.2f}.",
        ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
