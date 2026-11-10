"""A scripted stand-in for a real model, shared by the in-process and subprocess tests."""

from __future__ import annotations

import re
import time
import uuid


def _call(name, **args):
    return {
        "role": "assistant",
        "text": "",
        "tool_calls": [{"id": f"call_{uuid.uuid4().hex[:10]}", "name": name, "args": args}],
    }


class FakeModel:
    """Rule-based: "post invoice-NN" -> post_invoice, "every invoice" -> read them all,
    a tool result -> a one-line text answer naming its status. Records what it was sent."""

    def __init__(self, name="fake", delay=0.0):
        self.name, self.delay, self.seen, self.systems = name, delay, [], []

    def complete(self, history, system, tools_, cancel, notify):
        self.seen.append(list(history))
        self.systems.append(system)
        if self.delay:
            time.sleep(self.delay)
        last = history[-1]
        if last["role"] == "user":
            if m := re.search(r"post (invoice-\d+)(?: for amount (\S+))?", last["text"]):
                extra = {"amount": m.group(2)} if m.group(2) else {}
                return _call("post_invoice", invoice_id=m.group(1), **extra)
            if "every invoice" in last["text"]:
                calls = [_call("read_invoice", invoice_id=f"invoice-0{i}")["tool_calls"][0] for i in range(1, 7)]
                return {"role": "assistant", "text": "", "tool_calls": calls}
            if m := re.search(r"read (invoice-\d+)", last["text"]):
                return _call("read_invoice", invoice_id=m.group(1))
            done = [
                m["result"]["invoice_id"]
                for m in history
                if m["role"] == "tool" and m["name"] == "read_invoice" and "invoice_id" in m["result"]
            ]
            return {"role": "assistant", "text": f"{self.name}: read {done}", "tool_calls": []}
        return self.after_tool(last)

    def after_tool(self, last):
        r = last["result"]
        return {
            "role": "assistant",
            "text": f"{self.name}: {r.get('status')} {r.get('entry_id') or r.get('reason', '')}",
            "tool_calls": [],
        }


class GullibleModel(FakeModel):
    """Does whatever an invoice tells it to: the worst case for prompt injection."""

    def after_tool(self, last):
        text = str(last["result"])
        if last["name"] == "read_invoice" and (m := re.search(r"post (invoice-\d+)", text)):
            return _call("post_invoice", invoice_id=m.group(1), amount="1.00", vendor_id="V-001")
        return super().after_tool(last)
