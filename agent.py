"""Accounts payable agent: a command-line chat that posts vendor invoices to the ledger.

    python agent.py --conversation demo [--model claude|openai]

The loop (Agent.advance): answer any tool calls still open in the last model reply, then
ask the model again, until it replies with plain text. A post_invoice call pauses the
loop until the clerk types /approve or /decline. Every message is saved as soon as it
exists, and "what's still open" is worked out from the saved history, so a restarted
program carries on exactly where the last one stopped, approval included.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import textwrap
import threading

import ledger
import store as store_mod
import tools
from providers import API_KEYS, PROVIDERS, Cancelled, ProviderError
from store import Store

MAX_STEPS = 15  # model calls per user message, so a confused model can't loop forever

SYSTEM_PROMPT = """You are an accounts payable assistant for Acme Foods. You help a clerk check \
vendor invoices against the vendor list and post them to the ledger.

- Use the tools to look things up; never guess invoice or vendor details.
- Call post_invoice only when the clerk asks you to post or pay specific invoices. A question \
such as "can we pay X?" or "is X ready?" is not a request to post: answer it, and offer to post.
- When the clerk does ask, call post_invoice with the invoice id (e.g. invoice-01) straight away. \
The program checks the vendor and the ledger and asks the clerk to approve, so don't ask first.
- If a post is refused or declined, nothing was posted: say so plainly and give the reason.
- If a tool result says "cancelled", the clerk stopped you; don't retry it unless asked.
- Answer briefly. Give money as currency and amount, e.g. INR 16862.20."""


class Stopped(Exception):
    """The clerk pressed Ctrl-C while the agent was working."""


class Agent:
    def __init__(self, store: Store, conversation: str, provider: str, providers=None, out=print):
        self.store = store
        self.conversation = conversation
        self.provider_name = provider
        self.factories = providers or PROVIDERS
        self.instances: dict = {}
        self.out = out
        self.history = store.messages(conversation)
        self.system, self.tool_defs = store.pinned_setup(conversation, SYSTEM_PROMPT, tools.TOOLS)
        self.awaiting: tuple[dict, dict] | None = None  # (tool call, what would be posted)
        self.stop = threading.Event()

    # -- persistence ------------------------------------------------------------------

    def record(self, message: dict) -> None:
        self.store.append(self.conversation, message)
        self.history.append(message)

    def open_calls(self) -> list[dict]:
        """Tool calls in the latest model reply that have no result yet."""
        for i in range(len(self.history) - 1, -1, -1):
            msg = self.history[i]
            if msg["role"] == "assistant":
                answered = {m["tool_call_id"] for m in self.history[i + 1 :] if m["role"] == "tool"}
                return [c for c in msg.get("tool_calls", []) if c["id"] not in answered]
            if msg["role"] == "user":
                return []
        return []

    def tool_result(self, call: dict, result: dict) -> dict:
        return {"role": "tool", "tool_call_id": call["id"], "name": call["name"], "result": result}

    def refusal(self, call: dict, reason: str) -> dict:
        return self.tool_result(call, {"status": "refused", "posted": False, "reason": reason})

    def refuse(self, call: dict, reason: str) -> None:
        self.out(f"  · not posted: {reason}")
        self.record(self.refusal(call, reason))

    # -- providers ----------------------------------------------------------------------

    def provider(self):
        if self.provider_name not in self.instances:
            self.instances[self.provider_name] = self.factories[self.provider_name]()
        return self.instances[self.provider_name]

    def switch(self, name: str) -> None:
        self.provider_name = name
        self.store.set_provider(self.conversation, name)

    def call_model(self) -> dict:
        """Ask the model in a background thread so Ctrl-C can abandon the call at once.

        An abandoned call keeps running in its daemon thread until the HTTP request
        returns; its reply is simply dropped and never saved.
        """
        provider, history, stop = self.provider(), list(self.history), self.stop
        box: dict = {}
        done = threading.Event()

        def run():
            try:
                box["reply"] = provider.complete(
                    history, self.system, self.tool_defs, stop, lambda note: self.out(f"  ({note})")
                )
            except BaseException as exc:  # handed to the main thread below
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        while not done.wait(0.1):
            if stop.is_set():
                raise Stopped()
        if stop.is_set() or isinstance(box.get("error"), Cancelled):
            raise Stopped()
        if "error" in box:
            raise box["error"]
        return box["reply"]

    # -- the loop -------------------------------------------------------------------------

    def advance(self) -> None:
        """Run until the model answers in text, a post needs approval, or the clerk stops it."""
        for _ in range(MAX_STEPS):
            if self.run_open_calls():
                return  # waiting for /approve or /decline
            last = self.history[-1] if self.history else None
            if last and last["role"] == "assistant" and not last.get("tool_calls"):
                return  # the model has answered
            if self.stop.is_set():
                raise Stopped()
            crash_point("before_model_call")
            reply = self.call_model()
            self.record(reply)
            crash_point("after_reply_saved")
            if reply["text"]:
                self.say(reply["text"])
            elif not reply["tool_calls"]:
                self.out("agent> (no answer)")
        self.out(f"agent> (stopped after {MAX_STEPS} model calls in one turn)")

    def run_open_calls(self) -> bool:
        """Answer open tool calls in order. Returns True if paused on an approval."""
        for call in self.open_calls():
            if self.stop.is_set():
                raise Stopped()
            if call["name"] != tools.POST_TOOL:
                self.out(f"  · {call['name']}({_fmt_args(call['args'])})")
                self.record(self.tool_result(call, tools.run_read_tool(call["name"], call["args"])))
                continue
            try:
                details = tools.prepare_post(call["args"].get("invoice_id", ""), self.store)
            except tools.ToolError as exc:
                self.out(f"  · post_invoice refused: {exc}")
                self.record(self.refusal(call, str(exc)))
                continue
            self.awaiting = (call, details)
            self.show_approval(details)
            return True
        return False

    # -- approval -----------------------------------------------------------------------

    def show_approval(self, d: dict) -> None:
        self.out(
            "\n  Approval needed: post to the ledger\n"
            f"    vendor    {d['vendor_id']}  {d['vendor_name']}\n"
            f"    invoice   {d['invoice_number']}  dated {d['invoice_date']}  ({d['invoice_id']})\n"
            f"    amount    {d['currency']} {d['amount']}\n"
            + (f"  ! Warning: {d['warning']}\n" if d.get("warning") else "")
            + "  Type /approve or /decline <reason>\n"
        )

    def approve(self) -> None:
        """Post exactly what the clerk saw, or nothing.

        Everything is checked again first: the ledger or the invoice file may have changed
        while the approval was waiting. If what would be posted is no longer what was
        shown, nothing is posted and the new details go back to the clerk for approval.
        """
        call, shown = self.awaiting
        self.awaiting = None
        try:
            details = tools.prepare_post(call["args"].get("invoice_id", ""), self.store)
        except tools.ToolError as exc:
            self.refuse(call, str(exc))
            return
        if details != shown:
            changed = ", ".join(k for k in details if details[k] != shown.get(k))
            self.out(f"  · not posted: the invoice changed since you saw it ({changed}). Please review it again.")
            return  # the call is still open, so the loop shows the new details for approval
        self.post(call, details)

    def decline(self, reason: str) -> None:
        call, _ = self.awaiting
        self.awaiting = None
        self.record(
            self.tool_result(
                call,
                {
                    "status": "declined",
                    "posted": False,
                    "reason": reason or "no reason given",
                    "note": "The clerk declined this post. Nothing was posted.",
                },
            )
        )

    def post(self, call: dict, d: dict) -> None:
        """Post to the ledger. Not stoppable: once approved, a post runs to completion (~2s).

        The intent is saved before ledger.post and the call id goes to the ledger as the
        reference, so a crash at any point can be reconciled on the next start.
        """
        if not self.store.begin_post(call["id"], self.conversation, d["vendor_id"], d["invoice_number"]):
            self.refuse(call, f"another post of {d['invoice_number']} is in progress; not posting it again")
            return
        # Check the ledger again now that we hold the claim: a post that finished between
        # prepare_post and the claim is caught here, one still running is caught above.
        if existing := ledger.entries(vendor_id=d["vendor_id"], invoice_number=d["invoice_number"]):
            reason = (
                f"{d['invoice_number']} is already in the ledger as {existing[0]['entry_id']}; not posting it again"
            )
            result = self.refusal(call, reason)
            self.store.finish_post(call["id"], "failed", None, self.conversation, result)  # releases the claim
            self.history.append(result)
            self.out(f"  · not posted: {reason}")
            return
        self.out(f"  · posting {d['invoice_number']} to the ledger...")
        crash_point("before_ledger_post")
        try:
            entry_id = ledger.post(
                vendor_id=d["vendor_id"],
                invoice_number=d["invoice_number"],
                invoice_date=d["invoice_date"],
                currency=d["currency"],
                amount=d["amount"],
                reference=ledger_reference(call["id"]),
            )
        except Exception as exc:
            result = self.tool_result(call, {"status": "error", "posted": False, "reason": str(exc)})
            self.store.finish_post(call["id"], "failed", None, self.conversation, result)
        else:
            crash_point("after_ledger_post")
            result = self.tool_result(call, {"status": "posted", "posted": True, "entry_id": entry_id, **d})
            self.store.finish_post(call["id"], "posted", entry_id, self.conversation, result)
        self.history.append(result)
        crash_point("after_post_saved")

    # -- stop -----------------------------------------------------------------------------

    def work(self, step, *args, ask_model: bool = True) -> None:
        """Run one unit of agent work with Ctrl-C meaning "stop", not "crash".

        While working, SIGINT only sets a flag; the loop checks it between steps and
        while waiting on the model. Saved state is never cut off halfway.
        """
        self.stop = threading.Event()

        def on_sigint(*_):
            if not self.stop.is_set():
                self.out("\n  (stopping...)")
            self.stop.set()

        previous = signal.signal(signal.SIGINT, on_sigint)
        try:
            step(*args)
            if ask_model:
                self.advance()
            else:
                self.run_open_calls()
        except Stopped:
            self.awaiting = None
            for call in self.open_calls():  # keep the history valid for every provider
                self.record(self.tool_result(call, {"status": "cancelled", "reason": "stopped by the clerk"}))
            self.out("Stopped.")
        except Exception as exc:  # provider errors, missing keys, network: report, don't crash
            self.awaiting = None
            in_flight = {i["tool_call_id"] for i in self.store.unfinished_posts()}  # reconcile owns those
            for call in self.open_calls():  # a call left without a result would break the next request
                if call["id"] not in in_flight:
                    self.record(self.tool_result(call, {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}))
            kind = "" if isinstance(exc, ProviderError) else f"{type(exc).__name__}: "
            self.out(
                f"Model error ({self.provider_name}): {kind}{exc}\nSend a message to try again, or /model to switch."
            )
        finally:
            signal.signal(signal.SIGINT, previous)

    # -- start-up -------------------------------------------------------------------------

    def resume(self) -> None:
        """Re-show a waiting approval and finish tool calls left open by a quit or crash.

        Doesn't call the model by itself: the turn may have ended because the clerk
        stopped it, so carrying on waits for their next message.
        """
        self.work(lambda: None, ask_model=False)
        if not self.awaiting and self.history and self.history[-1]["role"] != "assistant":
            self.out("(The last turn didn't finish. Send a message to carry on.)")

    def say(self, text: str) -> None:
        body = textwrap.indent(text.strip(), " " * 7).lstrip()
        self.out(f"agent> {body}")


def crash_point(name: str) -> None:
    """Test hook: AGENT_CRASH_AT=<name> kills the process right here, like kill -9.

    os._exit skips every finally block and cleanup handler, so the tests see exactly
    what a real crash would leave on disk.
    """
    if os.environ.get("AGENT_CRASH_AT") == name:
        os._exit(137)


def ledger_reference(tool_call_id: str) -> str:
    return f"agent:{tool_call_id}"


def reconcile(store: Store, out=print) -> None:
    """Settle posts that started but never recorded an outcome (the program died mid-post).

    The ledger is the truth: if it has an entry with our reference, the post happened.
    """
    for intent in store.unfinished_posts():
        found = ledger.entries(reference=ledger_reference(intent["tool_call_id"]))
        call = {"id": intent["tool_call_id"], "name": tools.POST_TOOL}
        if found:
            entry = found[0]
            result = {
                "status": "posted",
                "posted": True,
                "entry_id": entry["entry_id"],
                "invoice_number": entry["invoice_number"],
                "vendor_id": entry["vendor_id"],
                "currency": entry["currency"],
                "amount": entry["amount"],
                "note": "Confirmed from the ledger after the program restarted.",
            }
            status = "posted"
            out(f"(Recovered an interrupted post: {entry['invoice_number']} is in the ledger as {entry['entry_id']}.)")
        else:
            result = {
                "status": "error",
                "posted": False,
                "reason": "The program stopped during the post and the ledger has no entry. Nothing was posted.",
            }
            status = "failed"
            out(f"(An interrupted post of {intent['invoice_number']} never reached the ledger; nothing was posted.)")
        message = {"role": "tool", "tool_call_id": call["id"], "name": call["name"], "result": result}
        store.finish_post(
            intent["tool_call_id"], status, found[0]["entry_id"] if found else None, intent["conversation"], message
        )


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{v}" for v in args.values())


HELP = """Commands: /model claude|openai   /approve   /decline <reason>   /history   /quit
Press Ctrl-C while the agent is working to stop it."""


def main(argv=None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--conversation", "-c", default="default")
    parser.add_argument("--model", "-m", choices=sorted(PROVIDERS))
    parser.add_argument("--db", default=str(store_mod.DB_PATH), help="where conversations are saved")
    args = parser.parse_args(argv)

    store = Store(args.db)
    if not store.lock_conversation(args.conversation):
        print(f'Conversation "{args.conversation}" is already open in another window. Close it there first.')
        return 1
    reconcile(store)
    provider, is_new = store.open_conversation(args.conversation, args.model or "claude")
    agent = Agent(store, args.conversation, provider)
    if args.model and args.model != provider:
        agent.switch(args.model)

    state = "new" if is_new else f"resumed, {len(agent.history)} messages"
    print(f'Conversation "{args.conversation}" ({state}). Model: {agent.provider_name}.\n{HELP}\n')
    if not is_new:
        last_text = next((m["text"] for m in reversed(agent.history) if m["role"] == "assistant" and m["text"]), None)
        if last_text:
            agent.say(last_text)
    agent.resume()

    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        command, _, rest = line.partition(" ")
        rest = rest.strip()

        if command in ("/quit", "/exit"):
            break
        elif command == "/help":
            print(HELP)
        elif command == "/model":
            if rest not in PROVIDERS:
                print(f"Model: {agent.provider_name}. Use /model {'|'.join(PROVIDERS)}")
            elif not os.environ.get(API_KEYS[rest]):
                print(f"Can't switch: {API_KEYS[rest]} is not set.")
            else:
                agent.switch(rest)
                print(f"Switched to {rest}.")
        elif command == "/history":
            for m in agent.history:
                print(f"  {m['role']:<9} {m.get('text') or m.get('tool_calls') or m.get('result')}"[:200])
        elif command in ("/approve", "/decline"):
            if not agent.awaiting:
                print("Nothing is waiting for approval.")
            elif command == "/approve":
                agent.work(agent.approve)
            else:
                agent.work(agent.decline, rest)
        elif command.startswith("/"):
            print(f"Unknown command {command}.\n{HELP}")
        elif agent.awaiting:
            print("A post is waiting for approval: type /approve or /decline <reason> first.")
        else:
            agent.work(agent.record, {"role": "user", "text": line})
    return 0


if __name__ == "__main__":
    sys.exit(main())
