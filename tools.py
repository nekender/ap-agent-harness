"""The agent's tools, and the posting rules enforced in code.

The model only ever passes an invoice id to post_invoice. Everything that goes to the
ledger (vendor id, invoice number, date, currency, amount) is derived here from the
invoice file and vendors.json, so the model can't change what gets posted.
"""

from __future__ import annotations

import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import ledger

HERE = Path(__file__).parent
INVOICES_DIR = HERE / "invoices"
VENDORS_FILE = HERE / "vendors.json"

# Set SLOW_TOOLS=3 to make read_invoice take 3 seconds (handy for testing Ctrl-C).
SLOW_TOOLS = float(os.environ.get("SLOW_TOOLS", "0"))

POST_TOOL = "post_invoice"

TOOLS = [
    {
        "name": "list_invoices",
        "description": "List every invoice waiting in the inbox: id, invoice number, vendor name and total.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "read_invoice",
        "description": "Read one invoice in full: dates, vendor, lines, subtotal, taxes and total.",
        "parameters": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string", "description": "e.g. invoice-01"}},
            "required": ["invoice_id"],
        },
    },
    {
        "name": "lookup_vendor",
        "description": (
            "Look up a vendor by vendor id (V-001) or by (part of) its name. Shows status and why it is blocked."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "vendor id or name"}},
            "required": ["query"],
        },
    },
    {
        "name": "check_ledger",
        "description": "Find entries already posted to the ledger, by vendor id and/or invoice number.",
        "parameters": {
            "type": "object",
            "properties": {
                "vendor_id": {"type": "string"},
                "invoice_number": {"type": "string"},
            },
        },
    },
    {
        "name": POST_TOOL,
        "description": (
            "Post an invoice to the ledger so it gets paid. The program checks the vendor and the "
            "ledger, then asks the clerk to approve before anything is posted."
        ),
        "parameters": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string", "description": "e.g. invoice-01"}},
            "required": ["invoice_id"],
        },
    },
]


class ToolError(Exception):
    """A problem the model should hear about as the tool's result."""


# -- data access ------------------------------------------------------------------


def _normalise_id(invoice_id: str) -> str:
    match = re.fullmatch(r"\s*invoice-?(\d{1,2})(\.json)?\s*", str(invoice_id), re.IGNORECASE)
    if not match:
        raise ToolError(f"not an invoice id: {invoice_id!r} (expected e.g. invoice-01)")
    return f"invoice-{int(match.group(1)):02d}"


def load_invoice(invoice_id: str) -> tuple[str, dict]:
    invoice_id = _normalise_id(invoice_id)
    path = INVOICES_DIR / f"{invoice_id}.json"
    if not path.exists():
        raise ToolError(f"no such invoice: {invoice_id}")
    try:
        invoice = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ToolError(f"{invoice_id} can't be read ({exc}); it needs fixing before it can be used") from None
    if not isinstance(invoice, dict):
        raise ToolError(f"{invoice_id} is not an invoice")
    return invoice_id, invoice


def load_vendors() -> list[dict]:
    return json.loads(VENDORS_FILE.read_text())


# -- read-only tools --------------------------------------------------------------


def list_invoices() -> dict:
    invoices = []
    for path in sorted(INVOICES_DIR.glob("invoice-*.json")):
        inv = json.loads(path.read_text())
        invoices.append(
            {
                "invoice_id": path.stem,
                "invoice_number": inv["invoice_number"],
                "vendor_name": inv["vendor_name"],
                "currency": inv["currency"],
                "total": inv["total"],
            }
        )
    return {"invoices": invoices}


def read_invoice(invoice_id: str) -> dict:
    if SLOW_TOOLS:
        time.sleep(SLOW_TOOLS)
    invoice_id, invoice = load_invoice(invoice_id)
    return {"invoice_id": invoice_id, **invoice}


def lookup_vendor(query: str) -> dict:
    q = str(query).strip().lower()
    vendors = load_vendors()
    exact = [v for v in vendors if v["vendor_id"].lower() == q or v["name"].lower() == q]
    matches = exact or [v for v in vendors if q and q in v["name"].lower()]
    return {"vendors": matches} if matches else {"vendors": [], "note": f"no vendor matches {query!r}"}


def check_ledger(vendor_id: str | None = None, invoice_number: str | None = None) -> dict:
    filters = {k: v for k, v in {"vendor_id": vendor_id, "invoice_number": invoice_number}.items() if v}
    return {"entries": ledger.entries(**filters)}


READ_TOOLS = {
    "list_invoices": list_invoices,
    "read_invoice": read_invoice,
    "lookup_vendor": lookup_vendor,
    "check_ledger": check_ledger,
}


def run_read_tool(name: str, args: dict) -> dict:
    """Run a non-posting tool. Errors become results the model can read."""
    func = READ_TOOLS.get(name)
    if func is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return func(**args)
    except ToolError as exc:
        return {"error": str(exc)}
    except TypeError as exc:  # wrong / missing arguments from the model
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # anything else still has to answer the call
        return {"error": f"{name} failed: {type(exc).__name__}: {exc}"}


# -- posting ------------------------------------------------------------------------


def prepare_post(invoice_id: str, store) -> dict:
    """Work out exactly what would be posted, or raise ToolError saying why it can't be.

    Called when the model asks to post (so refusals never reach the clerk) and again
    right before ledger.post (the ledger may have changed while approval was waiting).
    """
    invoice_id, inv = load_invoice(invoice_id)
    missing = [k for k in ("invoice_number", "invoice_date", "vendor_name", "currency", "total") if not inv.get(k)]
    if missing:
        raise ToolError(f"{invoice_id} is missing {', '.join(missing)}; not posting it")

    try:
        amount = Decimal(inv["total"])
    except (InvalidOperation, KeyError, TypeError):
        raise ToolError(f"{invoice_id} has no valid total") from None
    if not amount.is_finite() or amount <= 0:
        raise ToolError(f"{invoice_id} total must be positive, got {inv['total']}")

    vendor = next((v for v in load_vendors() if v["name"] == inv["vendor_name"]), None)
    if vendor is None:
        raise ToolError(f"vendor {inv['vendor_name']!r} on {invoice_id} is not in the vendor list; not posting")
    if vendor["status"] != "active":
        reason = vendor.get("blocked_reason", "no reason given")
        raise ToolError(
            f"vendor {vendor['vendor_id']} ({vendor['name']}) is {vendor['status']}: {reason}. "
            "Invoices from this vendor must not be posted."
        )

    existing = ledger.entries(vendor_id=vendor["vendor_id"], invoice_number=inv["invoice_number"])
    if existing:
        raise ToolError(
            f"invoice {inv['invoice_number']} from {vendor['vendor_id']} is already in the ledger "
            f"as {existing[0]['entry_id']}; not posting it again"
        )
    if store.post_in_flight(vendor["vendor_id"], inv["invoice_number"]):
        raise ToolError(f"a post of {inv['invoice_number']} is already in progress; not posting it again")

    details = {
        "invoice_id": invoice_id,
        "vendor_id": vendor["vendor_id"],
        "vendor_name": vendor["name"],
        "invoice_number": inv["invoice_number"],
        "invoice_date": inv["invoice_date"],
        "currency": inv["currency"],
        "amount": inv["total"],
    }
    # The ledger decides, but if our own records say we posted this before and the ledger has
    # no such entry, something removed it (a reset, a reversal): the clerk should know first.
    earlier = store.earlier_posts(vendor["vendor_id"], inv["invoice_number"])
    if earlier:
        details["warning"] = (
            f"our records show this invoice was already posted as {', '.join(earlier)}, "
            "but the ledger has no such entry. Check before approving."
        )
    return details
