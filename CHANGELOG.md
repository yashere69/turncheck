# Changelog

## 0.3.2

- The Graph API mock no longer looks up the hostname of 127.0.0.1 before listening.
  `http.server` does that in `server_bind()`, and on GitHub's macOS runners the lookup took
  about 35 seconds, so scenarios timed out there. The example agent skips it too, and the
  README says how to do the same in your own `http.server` agent.
- Startup retries a verification GET that times out instead of failing on the first one,
  and an agent that accepts connections but never answers gets an error that says so
  rather than `HTTP None`.

## 0.3.1

- Output is UTF-8 even when redirected on Windows, where it used to follow the code page
  and fail on Devanagari text.
- `--transcripts`: agent turns hold the message as the customer saw it (`pong`, or
  `[template order_update]`, or the text with `[buttons: Yes | No]`), not the debug form
  `text: 'pong'`. Opened in chatlens, the conversation now reads like the chat.
- An `--agent` command that doesn't exist fails with "can't start the agent: 'x' not found"
  rather than a bare `FileNotFoundError`.
- `run` and `lint` take several files or folders (`turncheck lint scenarios/*.yaml` works),
  and `lint` reports every broken file in one pass instead of stopping at the first. A
  path that doesn't exist says so rather than "no scenarios found".
- Invalid YAML is reported on one line with its line number, not PyYAML's context dump.
- CI: the `compat` job fails if pywa can't be imported, instead of passing on skips.

## 0.3.0

Closer to how Meta actually delivers and rejects messages.

- Webhooks carry business-scoped user IDs (`user_id`, `from_user_id`, `recipient_user_id`),
  and `customer.share_phone: false` models a username customer whose number Meta withholds.
  Replies may be addressed with `recipient` (BSUID) as well as `to`.
- Free-form sends outside the 24-hour window are rejected with `131047`, as a `failed`
  receipt by default or on the send (`window_errors: response`).
- `graph_error` step: fail the agent's next send(s) with a Cloud API error code.
- `send_batch` step: several notifications in one POST.
- Unacknowledged webhooks (no 2xx within `webhook_timeout_ms`) are redelivered;
  `expect_http` takes `within_ms`.
- Expectations wait up to `--reply-timeout-ms` for the reply. Before this, an agent that
  took longer than `--settle-ms` to answer failed with "agent sent nothing".
- Failure output includes the tail of the agent's stderr. The agent's stderr goes to a
  temporary file; the old unread pipe could fill and block a chatty agent.
- Fixed: `expect_http: 4xx` (documented in 0.2) crashed the runner.
- Fixed: Windows paths in `--agent` lost their backslashes.
- `python -m turncheck` works.
- Reference agent: four more seeded bugs (`noretry`, `batch`, `slowack`, `phoneonly`).
- CI: a `compat` job runs scenarios against a pywa 4.5.0 bot; tests also run on Windows
  and macOS.

Behaviour changes: the window check and webhook redelivery are on by default. A scenario
that relied on free-form messages outside the window, or on a slow agent not being
retried, now fails; set `window_errors: off` or raise `webhook_timeout_ms`.

## 0.2.0

- Fail on replies no expectation covers (opt out with `allow_extra_replies: true`)
- `--webhook-path` and `{placeholder}` values in `--env`, so existing agents run unmodified
- `--transcripts` writes conversations in chatlens's trace format
- `lint` rejects scenarios with no steps

## 0.1.0

First release.

- Scenario files (YAML) with send, press, redeliver, status, event, advance and send_tampered steps
- Webhook envelopes in the Cloud API shape, signed with X-Hub-Signature-256
- Graph API stand-in that records outbound messages and checks the bearer token
- Simulated clock for 24-hour window tests
- Opt-in tool-call tracing (`turncheck.trace.tool_call`)
- Console and JUnit XML output, composite GitHub Action
- Reference shop agent with five switchable seeded bugs and a mutation script
