# AP agent

A command-line agent for posting vendor invoices to `ledger.py`. A person approves every post.
You can switch between Claude and OpenAI mid-conversation, quit and come back later, and stop it
with Ctrl-C. Failure cases are written up in [docs/DESIGN.md](docs/DESIGN.md) and eval results
in [evals/FINDINGS.md](evals/FINDINGS.md).

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install anthropic openai python-dotenv pytest ruff
printf 'ANTHROPIC_API_KEY=...\nOPENAI_API_KEY=...\n' > .env
python agent.py --conversation demo   # --model claude|openai to choose the starting model
pytest                                # 26 tests, uses a fake model, no keys needed
python evals/run_evals.py             # evals against the real models, about $0.80 and 4 minutes
```

Commands: `/model claude|openai`, `/approve`, `/decline <reason>`, `/history`, `/quit`. Ctrl-C
stops the agent while it's working and quits at the prompt.

Models: Claude `claude-opus-5-5` through the `anthropic` SDK, and OpenAI `gpt-5-mini` through the
`openai` SDK (Chat Completions). `ANTHROPIC_MODEL` and `OPENAI_MODEL` override them.
`SLOW_TOOLS=3` makes `read_invoice` take 3 seconds, which gives you time to test Ctrl-C.

The code is in four files. `agent.py` has the loop, approval and stop, `providers.py` the two SDK
adapters, `tools.py` the tools and posting rules, and `store.py` the SQLite storage.

## What gets saved

Everything is in `agent.db` (SQLite, git-ignored). Each write is committed as soon as it happens.

- `conversations`: the current provider, plus the system prompt and tool definitions the
  conversation started with. These are kept fixed because Claude rejects a resumed
  conversation if the prompt has changed.
- `messages`: user messages, model replies and tool results. A model reply is saved before
  any of its tools run.
- `post_intents`: one row per post, written before `ledger.post` is called.

There's no "waiting for approval" flag. On startup the program looks for a `post_invoice` call
in the history that has no result yet, and shows that approval again.

## History and switching providers

History is stored in a provider-neutral format: user messages, assistant messages (text and tool
calls) and tool results. Before each call, the current provider's adapter converts the whole
history into that provider's format, and converts the reply back. `/model` just changes which
adapter is used.

Claude replies also keep their original content blocks. Claude needs its thinking blocks sent
back unchanged, and since the history is only ever appended to, they still work after a few
turns on OpenAI.

## Approval, posting rules and stopping

`post_invoice` only takes an invoice id. The vendor, amount and other details are read from the
files, so the model can't change them. Before showing an approval, the code refuses unknown or
blocked vendors, invoices already in the ledger, and invoices that are being posted right now.
`/approve` checks again and posts only if the details still match what was shown. If the invoice
file changed in the meantime, it shows the new details instead. `/decline` passes the reason back
to the model.

Before calling `ledger.post`, the program records the post in `post_intents`. A unique index
allows only one post per invoice to be in progress at a time, across processes too. Each post
also carries `reference="agent:<tool call id>"`. If the program dies mid-post, the next start
looks that reference up in the ledger to find out whether the post went through. A file lock
stops the same conversation being opened in two terminals.

While the agent is working, Ctrl-C sets a flag instead of raising an exception. Model calls run
in a background thread, so a stop doesn't wait for them, and tool calls that didn't run are
marked `cancelled`. An approved post is the exception: it takes about 2 seconds and is allowed to
finish, since stopping halfway would leave it unclear whether it was posted.

## Not handled / next

- If someone posts directly to the ledger between my last check and `ledger.post`, a duplicate
  can still happen. Fixing that properly needs an idempotency key enforced by the ledger itself.
- No conversation lock on Windows. An abandoned model call keeps running in the background
  until it returns, and a slow tool finishes before a stop takes effect.
- Next I'd add accounting checks to the approval (lines add up, tax amounts match the rates, due
  date matches the vendor's payment terms).
- Prompt changes only apply to new conversations. The full history is sent on every call,
  with no compaction.
