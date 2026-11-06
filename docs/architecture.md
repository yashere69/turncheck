# How turncheck works

## The shape of a run

```
turncheck run examples/scenarios --agent "python examples/agents/shopagent.py"
```

For each scenario file:

1. **Start a Graph API stand-in** on a random localhost port (`graph_mock.py`). It accepts
   `POST /v{version}/{phone_number_id}/messages`, records the JSON body, checks the
   `Authorization: Bearer` header, and answers with a `wamid.OUT…` id the way Meta does.
   It also accepts `POST /__turncheck/trace` for tool-call events.
2. **Start the agent as a child process** with environment variables pointing it at the
   stand-in: `PORT`, `GRAPH_API_BASE`, `WHATSAPP_TOKEN`, `APP_SECRET`, `VERIFY_TOKEN`,
   `PHONE_NUMBER_ID`, `TURNCHECK_TRACE_URL`. A real agent normally reads these from its
   own config, so pointing it at the stand-in usually takes one line.
3. **Do the verification handshake.** turncheck calls
   `GET /webhook?hub.mode=subscribe&hub.verify_token=…&hub.challenge=4242` and expects
   `4242` back. An agent that fails this would never get past Meta's dashboard either.
4. **Play the steps.** Stimulus steps build an envelope (`envelope.py`), serialise it
   once, sign those exact bytes with the app secret, and POST it to `/webhook`.
5. **Wait for quiet.** Good agents return 200 immediately and do the work on another
   thread, so turncheck cannot read the reply off the HTTP response. It polls the
   recorder until nothing new has arrived for `--settle-ms` (default 150 ms), then hands
   the batch of outbound messages and trace events to the following expectation steps.
6. **Check expectations** against that batch. `expect_reply` takes one message off the
   front; `expect_no_reply` requires the batch to be empty.
7. **Stop the agent.** A fresh process per scenario means one test's in-memory state
   (dedup sets, session history) can't leak into the next.

## Why each piece exists

**Real HTTP instead of calling a Python function.** The bugs this tool targets live at the
transport layer: signature checks over the raw body, slow acknowledgements that make Meta
retry, a status webhook parsed as if it were a message. An in-process call skips all of
that. The cost is about 0.5 s per scenario for process start-up, which is fine for a suite
of tens of scenarios and not fine for thousands.

**A simulated clock.** The 24-hour customer-service window decides whether an agent may
send free-form text or must use a pre-approved template. Testing that against wall time
means waiting a day. Every envelope carries a `timestamp` from `SimClock`, and events
carry the simulated "now", so `advance: 25h` costs nothing. This only works if the agent
computes the window from message timestamps rather than `time.time()`. That is the
correct design anyway: webhooks can arrive late, and the timestamp is when the customer
actually wrote.

**Redelivery means identical bytes.** `redeliver: ask` re-sends the stored payload with
the same `wamid`. Meta retries undelivered webhooks for days and does not promise
exactly-once delivery, so an agent has to deduplicate on `wamid`.

**Tool calls are opt-in.** A webhook agent's tool calls aren't visible from outside. Rather
than parse logs, the agent calls `turncheck.trace.tool_call(name, args)`, which POSTs to
the stand-in when `TURNCHECK_TRACE_URL` is set and does nothing otherwise. That keeps the
production path clean and makes the contract explicit.

**Quiescence, not fixed sleeps.** A fixed sleep is either slow or flaky. Waiting for
"nothing new for N ms" adapts to the agent's speed. It can still be fooled by an agent that
pauses longer than N ms mid-reply, which is why `--settle-ms` exists.

## Determinism and LLMs

The reference agent uses a rule-based router so the suite is reproducible and needs no
API key. With a real LLM behind the agent, replies vary run to run. The assertions are
built for that: `contains`, `not_contains`, `matches` and message type are deliberately
loose about wording and strict about the things that must not vary (the right tool, the
right argument, template vs free-form, no duplicate reply). Recording and replaying LLM
responses is not in 0.1.

## Files

```
src/turncheck/
  envelope.py     webhook payload builders + HMAC signing
  clock.py        simulated clock and duration parsing
  scenario.py     YAML loading and validation
  graph_mock.py   Graph API stand-in and trace endpoint
  runner.py       process lifecycle, stimuli, quiescence, expectations
  assertions.py   reply and tool-call checks
  report.py       console and JUnit output
  trace.py        agent-side tool-call reporting
  cli.py          `turncheck run` / `turncheck lint`
examples/
  agents/shopagent.py   reference agent with SHOPAGENT_BUGS switches
  scenarios/            the six scenarios used in CI and in results/mutation.md
tools/mutation.py       regenerates results/mutation.md
```
