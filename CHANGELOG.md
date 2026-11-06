# Changelog

## 0.1.0

First release.

- Scenario files (YAML) with send, press, redeliver, status, event, advance and send_tampered steps
- Webhook envelopes in the Cloud API shape, signed with X-Hub-Signature-256
- Graph API stand-in that records outbound messages and checks the bearer token
- Simulated clock for 24-hour window tests
- Opt-in tool-call tracing (`turncheck.trace.tool_call`)
- Console and JUnit XML output, composite GitHub Action
- Reference shop agent with five switchable seeded bugs and a mutation script
