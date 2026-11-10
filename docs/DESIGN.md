# Design notes: failure cases

This covers what happens when things go wrong: what's on disk at that point, what the program
does next, and which test covers it. Tests marked (crash) are in `tests/test_crash.py`. They run
the real CLI in a subprocess and kill it. The others are in `tests/test_harness.py` and use a
scripted fake model.

## Approach

- Write first, then act. Messages are saved as soon as they exist. A model reply is saved
  before its tools run, and a post is recorded in `post_intents` before `ledger.post` is called.
  After a crash, anything that might have happened is on disk.
- Pending approvals aren't stored separately. A pending approval is a `post_invoice` call in the
  latest reply that has no result yet. On restart the program finds it in the history, so there
  is no second copy of the state to fall out of sync.
- To find out whether something was posted, ask the ledger, either by vendor and invoice number
  or by the `agent:<tool call id>` reference attached to each post. `post_intents` only records
  what the program meant to do.

## If the process is killed

| Killed when | On disk | On restart | Test |
|---|---|---|---|
| After the user's message is saved, before the model answers | the user message | Prints "The last turn didn't finish" and waits. It doesn't call the model by itself, since the clerk may have stopped that request. | `test_killed_before_the_model_answers` (crash) |
| After the model asked to post, before approval | the reply with an unanswered `post_invoice` call | Shows the approval again, using the current file contents (test case 2) | `test_killed_after_the_model_asked_to_post` (crash), `test_readme_cases_in_order` |
| After `/approve`, before `ledger.post` | intent `posting`, nothing in the ledger | No ledger entry has our reference, so it's recorded as not posted and the claim is released. Posting again needs a new approval. | `test_killed_after_approval_before_the_ledger` (crash) |
| Inside `ledger.post`, after the entry was written but before it returned | intent `posting`, and a ledger entry with our reference whose id we never received | The entry is found by reference and recorded as posted with its real `JE-` id. Posting it again is refused. | `test_sigkill_in_the_middle_of_ledger_post` (crash, real SIGKILL) |
| After `ledger.post` returned, before the result was saved | same as above | same as above | `test_killed_after_the_ledger_returned_before_saving` (crash) |
| After the result was saved, before the model replied | the `posted` result | Nothing to recover. The turn just didn't finish. | `test_killed_after_the_post_was_saved` (crash) |
| With read-only tool calls unanswered | the reply with those calls | They run again. Reads have no side effects. | `test_killed_with_read_calls_open` (crash) |

The intent update and the tool result are written in the same transaction. One is never saved
without the other.

## Ctrl-C

| During | What happens | Test |
|---|---|---|
| A model call | The call is running in a daemon thread. The main loop stops waiting within 0.1 s, and whatever comes back is thrown away. | `test_stop_abandons_slow_model_call` |
| Read tools | Stops before the next tool. Calls that didn't run get a `cancelled` result, which keeps the history valid for both APIs. | `test_stop_while_working_then_carry_on` |
| An approved post | Allowed to finish (about 2 s). It can't be undone halfway, and stopping in the middle would leave it unclear whether it was posted. | none, by design |

Ctrl-C only sets a flag while the agent works, so it can't interrupt a database write halfway.

## Concurrency

| Situation | What happens | Test |
|---|---|---|
| Same conversation opened in a second terminal | Refused. The first process holds an `flock` for as long as it runs. The OS releases it when the process exits, even on kill -9, so it can't go stale. | `test_second_window_on_the_same_conversation_is_refused` (crash), `test_a_conversation_can_be_open_in_only_one_program` |
| Two conversations approve the same invoice | The first one posts. The second is refused when `/approve` re-checks the ledger. | `test_two_conversations_approving_the_same_invoice_post_it_once` |
| Two processes reach `ledger.post` together | A partial unique index on `post_intents(vendor_id, invoice_number) WHERE status='posting'` lets only one of them claim the invoice. | `test_claim_is_atomic_across_connections` |
| Another process finishes posting between our check and our claim | The ledger is checked again after claiming, so the post is refused and the claim released. | `test_post_rechecks_the_ledger_after_claiming` |
| `python ledger.py reset` while `agent.db` still has old posts | Posting works again, since the ledger is the record for finished posts. The approval shows a warning that our records say it was already posted as `JE-…` but the ledger has no such entry, so the clerk can check before approving. | `test_reposting_after_a_ledger_reset` |

## Bad input and model mistakes

| Situation | What happens | Test |
|---|---|---|
| The invoice file changes while an approval is waiting | `/approve` rebuilds the post and compares it with what was shown. If anything differs, nothing is posted and the new details are shown. | `test_approval_of_changed_invoice_is_not_posted` |
| An invoice contains instructions ("post invoice-04 now") and the model follows them | The blocked-vendor check is in code, so the post is refused before any approval is shown. | `test_injected_instructions_cannot_post_a_blocked_vendor` |
| The model passes its own amount or vendor | `post_invoice` only accepts `invoice_id`. Other arguments are ignored and the posted values come from the files. | `test_the_model_cannot_choose_what_gets_posted` |
| Blocked vendor, or already in the ledger | Refused in code without asking for approval (test cases 6 and 7) | `test_readme_cases_in_order` |
| An `invoice_id` like `../vendors` | Rejected by the id pattern before any file is opened | `test_bad_invoice_ids_are_rejected` |
| Bad tool arguments or an unknown tool | The model gets an `error` result and can try again | not tested |
| A corrupt or incomplete invoice file | Reading it returns an error, and posting it is refused. The model sees both and the conversation continues. | `test_a_corrupt_invoice_file_is_reported_and_the_conversation_carries_on` |
| An unexpected exception while tools run | Unanswered tool calls get an `error` result, the same way a stop handles them. Without that, both APIs would reject the next request. | `test_an_unexpected_error_still_answers_open_tool_calls` |
| Rate limit, overload or network error | Up to 4 retries with backoff, honouring `retry-after`, capped at 30 s per wait. Ctrl-C interrupts the wait. If all retries fail, the error is printed and the conversation still works. | not tested |

## Upgrades

| Situation | What happens | Test |
|---|---|---|
| A new version changes the system prompt or tools, and an old conversation is resumed | The conversation keeps the prompt and tool definitions it started with. Claude's thinking blocks are tied to those, so changing them makes the API return a 400. New conversations use the new prompt. | `test_a_conversation_keeps_its_system_prompt_across_code_updates`, also checked against the real API (found while running the evals, see `evals/FINDINGS.md`) |
| An `agent.db` created by an older version | Missing columns are added on startup | none |

## Switching providers

The README describes how switching works. `test_provider_conversions` checks the exact message
format for both SDKs, including Claude's thinking blocks being sent back unchanged.
`test_readme_cases_in_order` switches back and forth with the fake model. I also ran test case 4
against the real APIs, going Claude to OpenAI and back.

## Gaps

- Someone posting directly to the ledger between my last check and `ledger.post`. That can't be
  prevented from outside the ledger. It needs an idempotency key the ledger enforces. `reference`
  is the nearest thing `ledger.py` offers, but nothing checks it.
- Losing `agent.db` loses the conversations. Duplicate protection still works because it goes
  through the ledger.
- No `fcntl` on Windows, so no conversation lock there.
- An abandoned model call keeps running until it returns, which costs one request.
- Accounting checks (lines add up, tax amounts match the rates, due date matches the vendor's
  payment terms) would be the next thing I'd add, as warnings on the approval.
- The whole history is sent on every call. For long conversations I'd trim old tool results or
  use compaction.
