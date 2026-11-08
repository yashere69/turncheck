# turncheck

Regression tests for WhatsApp agents, run at the webhook level.

turncheck plays scripted conversations against your agent as real WhatsApp Cloud API
webhooks: signed with `X-Hub-Signature-256`, carrying `wamid`s, timestamps and the
business-scoped user IDs Meta added in 2026. It stands in for the Graph API and records
what the agent sends back, and it fails the way the real platform fails: it rejects
free-form messages outside the 24-hour window, can answer a send with a rate-limit or
server error, redelivers webhooks your agent was too slow to acknowledge, and batches
notifications. A scenario then checks that the agent did the right thing.

It's for people who ship WhatsApp agents to clients and want CI to fail before a prompt
change or a refactor reaches a customer's phone.

![turncheck output: ten scenarios pass against the reference agent, then two fail when the window and noretry bugs are switched on](docs/demo.png)

## Quick start

```bash
git clone https://github.com/yashere69/turncheck && cd turncheck
pip install .
turncheck run examples/scenarios --agent "python examples/agents/shopagent.py"
```

No Meta account and no API keys are needed. The example agent is stdlib Python with a
rule-based router standing in for the LLM.

## Why not just test the prompt?

Prompt-level eval tools such as [promptfoo](https://www.promptfoo.dev/docs/configuration/chat/)
are good at "does the model say the right thing". Many of the ways a WhatsApp agent breaks
in production are somewhere else:

- **Duplicates.** Meta delivers webhooks at least once. If your endpoint errors or is slow
  to answer, it retries "immediately, then a few more times with decreasing frequency over
  the next 36 hours" ([Meta's webhook docs](https://developers.facebook.com/docs/graph-api/webhooks/getting-started)).
  Without deduplication on `wamid`, the customer gets the same answer twice. An agent that
  calls its LLM before acknowledging the webhook invites exactly that retry.
- **The 24-hour window.** Outside 24 hours since the customer's last message, the
  business can only send pre-approved templates. Since Meta's July 2025 pricing change,
  the window also decides what a message costs.
- **Status webhooks.** `sent`, `delivered` and `read` receipts arrive on the same endpoint
  as messages. Agents that don't tell them apart reply to receipts.
- **Signatures.** An endpoint that skips HMAC verification will act on anyone's POST.
- **Tool arguments.** "order #KC1042" and "order KC1042" should end up as the same tool
  call.
- **Platform errors.** A send can come back as `130429 Rate limit hit` or a server error,
  or be accepted and then reported `failed` in a receipt. Agents that don't retry, or never
  read failed receipts, lose replies silently.
- **Customers without a phone number.** Since April 2026 webhooks carry a business-scoped
  user ID (`user_id`, `from_user_id`), and a customer who has set a WhatsApp username can
  arrive with no `wa_id` and no `from` at all ([Meta: BSUIDs](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids)).
  Code that keys everything on `from` breaks for them.

None of these show up in a prompt test. They do show up when you send the real envelope
to the real endpoint, so that's what turncheck does.

Other tools emulate the API too. [chatwright](https://pkg.go.dev/chatwright.dev/runtime/whatsapp)
is the closest: it drives a bot in any language over HTTP from Go tests, and its broader
framework covers Telegram and model-driven test users. Its WhatsApp emulator is text-first
and, as of v0.10 (checked against its main branch in November 2026), doesn't sign webhooks,
model the 24-hour window, redeliver, or send status receipts. [whatsapp-dev](https://github.com/mjarkk/whatsapp-dev) is a local mock
with a web UI for manual testing. Botium's [WhatsApp connector](https://github.com/codeforequity-at/botium-connector-whatsapp)
targets the old on-premise API and hasn't changed since 2020. turncheck is the one to use
when the agent is a separate process in any language and you want CI to check Meta's
delivery rules, not only the text of replies.

## Writing a scenario

```yaml
name: outside the 24-hour window, proactive updates use a template
customer: {wa_id: "919800000004", name: Kabir}
steps:
  - send: "order KC1043 status"
  - expect_tool: {name: lookup_order, args: {order_id: KC1043}}
  - expect_reply: {contains: "packed"}
  - advance: 25h
  - event: {path: /events/order-shipped, json: {order_id: KC1043}}
  - expect_reply: {type: template, template: order_update}
```

| Step | What it does |
|---|---|
| `send: "text"` | customer text message (tag it with `as: name` to redeliver it later) |
| `send_batch: ["a", "b"]` | several messages in one POST, one `entry` each |
| `press: {id, title}` | customer taps an interactive reply button |
| `redeliver: name` | re-POST the identical payload, same `wamid` |
| `status: {state: delivered}` | delivery receipt for the agent's last message |
| `send_tampered: "text"` | message with a wrong signature |
| `advance: 25h` | move the simulated clock (s, m, h, d) |
| `event: {path, json}` | POST to one of your agent's own endpoints, with the simulated time added as `timestamp` |
| `graph_error: {code: 130429}` | the agent's next send fails with that Cloud API error; `times`, `http` (default 400), `via: webhook` to accept the send and report it `failed` in a receipt instead |
| `expect_reply: {...}` | next outbound message; `type`, `contains`, `not_contains`, `matches`, `template`, `buttons` |
| `expect_no_reply: {}` | the agent sent nothing in response to the last stimulus |
| `expect_tool: {name, args}` | next traced tool call |
| `expect_http: 401` | status of the last webhook POST: a code, `4xx`, or `{status: 200, within_ms: 500}` to also check how fast it was acknowledged |

`turncheck lint <dir>` validates scenario files without running anything.

Expectations wait for the agent. An `expect_reply` passes as soon as the reply arrives and
fails only after `--reply-timeout-ms` (default 5000), so an agent that acknowledges first and
spends two seconds in an LLM call is tested the same way as a fast one.

Top-level options, all optional:

| Option | Default | Meaning |
|---|---|---|
| `customer` | `{wa_id: "919800000001"}` | also `name`, `username`, `user_id` (BSUID, derived from `wa_id` if absent), and `share_phone: false` for a customer whose number Meta withholds |
| `webhook_timeout_ms` | 10000 | a webhook not answered with a 2xx in this time counts as unacknowledged |
| `webhook_retries` | 1 | how many times an unacknowledged webhook is redelivered, byte for byte |
| `window_errors` | `webhook` | a free-form send outside the 24-hour window is accepted and then reported as a `failed` receipt with `131047`; `response` returns the error on the send instead; `off` disables the check |
| `allow_extra_replies` | false | see below |

Meta's error-code reference says errors arrive "either synchronously as a Graph API response,
asynchronously via Webhook", which is why both modes exist. It doesn't list HTTP statuses
for each code, so `graph_error` lets you set the one you see in production.

**Every reply has to be accounted for.** If the agent sends a message that no
`expect_reply` consumes before the next customer step (or the end of the scenario), the
scenario fails with "reply not covered by any expect". Double replies are one of the most
common WhatsApp agent bugs, and they don't only come from webhook retries. If a scenario
legitimately sends extra messages you don't care about, set `allow_extra_replies: true` at
the top of the file.

## Connecting your own agent

turncheck starts your agent as a subprocess and passes it these environment variables:

| Variable | Use it for |
|---|---|
| `PORT` | where your webhook server listens; turncheck POSTs to `/webhook` |
| `GRAPH_API_BASE` | base URL for sending messages, in place of `https://graph.facebook.com` |
| `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID` | as in production |
| `APP_SECRET`, `VERIFY_TOKEN` | signature check and `GET /webhook` verification |
| `TURNCHECK_TRACE_URL` | read by `turncheck.trace.tool_call()`; unset in production, so the call is a no-op |

To make tool calls assertable, add one line where the agent executes a tool:

```python
from turncheck import trace
trace.tool_call("lookup_order", {"order_id": order_id})
```

Your agent probably already has its own names for these settings and its own webhook
path. You don't need to change it; map them on the command line:

```bash
turncheck run tests/whatsapp --agent "node dist/server.js" --webhook-path /api/whatsapp \
  --env META_APP_SECRET={app_secret} --env WA_API_BASE={graph_api_base} \
  --env WA_ACCESS_TOKEN={access_token}
```

Placeholders: `{graph_api_base}`, `{app_secret}`, `{access_token}`, `{verify_token}`,
`{phone_number_id}`, `{port}`, `{trace_url}`. Use `{{`/`}}` for a literal brace.

The agent should compute the 24-hour window from message `timestamp`s, not from
`time.time()`. That's what makes `advance: 25h` work, and it's also correct when webhooks
arrive late. When a scenario fails, the last lines of the agent's stderr are printed under
the failure (and included in the JUnit output).

To reply to a customer who has no `wa_id`, send to their BSUID in the `recipient` field
instead of `to`, as Meta's docs describe. turncheck accepts either for customers who do
share a number.

### Agents built on a framework

Frameworks that hard-code `graph.facebook.com` can still be pointed at turncheck from a
test configuration. [examples/agents/pywa_bot.py](examples/agents/pywa_bot.py) is a bot on
[pywa](https://github.com/david-lev/pywa) whose handlers are untouched; a ten-line httpx
transport routes its Graph calls to `GRAPH_API_BASE`. The `compat` CI job runs the
scenarios in `examples/compat/` against it, with pywa pinned to 4.5.0. It passes the
signature, duplicate, receipt and username scenarios. Two scenarios show pywa 4.5.0
departing from Meta's documented format, and the job pins both down so a pywa upgrade that
changes them is noticed:

- It reads only `entry[0]` of a POST, so a batched notification after the first is ignored.
- It indexes `contacts[]` on every status webhook, but Meta omits `contacts` from `failed`
  receipts, so `on_message_status(filters.failed)` handlers raise `KeyError` instead of
  running.

## In CI

```yaml
- uses: yashere69/turncheck@v0.3.0
  with:
    scenarios: tests/whatsapp
    agent: node dist/server.js
```

The action writes JUnit XML to `turncheck-junit.xml` for your test reporter.

## Reading what happened

`--transcripts runs.jsonl` writes every scenario as a conversation (customer messages,
agent replies, tool calls) in the trace format [chatlens](https://github.com/yashere69/chatlens)
reads, with pass/fail and the first failure in `meta`. When a real LLM is behind the
agent, `chatlens serve runs.jsonl` is the quickest way to read its replies and label what
went wrong. chatlens can also turn failed conversations back into turncheck scenarios.

## Does it actually catch things?

The reference agent (`examples/agents/shopagent.py`) has nine seeded bugs, switched on
through `SHOPAGENT_BUGS`. `python tools/mutation.py` runs the suite once against the clean
agent and once per bug, and writes [results/mutation.md](results/mutation.md):

| Seeded bug | Caught by |
|---|---|
| `dedup` — no idempotency on `wamid` | 03-duplicate-delivery |
| `window` — free-form text after 24 h | 04-window |
| `orderid` — regex misses `#KC1042` | 02-order-status |
| `signature` — HMAC check skipped | 05-signature |
| `status` — replies to receipts | 06-status-receipts |
| `noretry` — gives up on `130429` | 07-rate-limit |
| `batch` — reads only `entry[0]` | 08-batched-webhook |
| `slowack` — does the work before acknowledging | 09-fast-ack |
| `phoneonly` — assumes every sender has `from` | 10-username-customer |

9 of 9 caught, each by its own scenario only, with no failures on the clean agent. The
same check runs in CI as `tests/test_end_to_end.py`. These bugs were written to be caught, so this shows the
harness works end to end. It doesn't show turncheck will find your bugs; your scenarios
decide that.

## Design notes

[docs/architecture.md](docs/architecture.md) covers the run lifecycle. The short version:

- **Real HTTP, fresh process per scenario.** Transport bugs only show up over the
  transport, and a restart stops dedup sets or session state leaking between tests. That
  costs about half a second per scenario.
- **A simulated clock**, so window tests take milliseconds, not a day.
- **Wait for the reply, not fixed sleeps.** Agents should acknowledge first and reply on
  another thread, so expectations poll until the message arrives (up to
  `--reply-timeout-ms`), and "no reply" is decided after `--settle-ms` of quiet.
- **Realistic by default.** The window check, redelivery of unacknowledged webhooks and
  BSUIDs are on unless a scenario turns them off, because those are the behaviours agents
  get wrong in production.
- **Tool tracing is opt-in and explicit**, rather than scraped from logs.

## Limits

- Media messages, WhatsApp Flows, calling, groups and multiple phone numbers per test are
  not modelled.
- Payloads are written from Meta's documentation (checked November 2026, including BSUIDs)
  and run through pywa in CI. They have not been diffed against captured production
  webhooks, so field-level drift is still possible; a payload Meta sends you that turncheck
  can't express is a bug worth reporting.
- Timing is simulated, not measured against Meta: the 36-hour retry schedule collapses into
  `webhook_retries` immediate redeliveries, and the default 10 s acknowledgement budget is a
  conservative choice, not a documented Meta limit.
- There is no record/replay of LLM responses. With a real LLM behind the agent, runs are
  non-deterministic and assertions should be loose (`contains`, type, tool arguments).

Stable: the scenario format and the CLI flags above. Experimental: the transcript format
(shared with chatlens) and the exact wording of failure messages.

## Layout

```
src/turncheck/      library and CLI
examples/agents/    reference agent with switchable bugs
examples/scenarios/ the ten scenarios above
examples/compat/    scenarios run against the pywa bot in CI
tools/mutation.py   regenerates results/mutation.md
docs/               architecture notes, screenshots
```

## Development

```bash
pip install -e ".[dev]"            # add ,compat for the pywa tests
ruff check . && ruff format --check . && mypy src && pytest
```

MIT licensed.
