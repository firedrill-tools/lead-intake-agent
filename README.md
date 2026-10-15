# Lead intake agent

An HTTP webhook receives inbound lead forms. After request validation, Gemini classifies the lead and writes a short summary. The service upserts a contact and sends a routed team notification. A durable event record makes unchanged webhook replays return the prior decision. The default connectors are local SQLite actions; a separate Pipedream Connect adapter calls Attio and Slack actions for connected test accounts.

## Local run

Requires Python 3.12, `uv`, a Gemini API key and a secret for incoming requests.

```sh
uv sync --dev
GEMINI_API_KEY=... INTAKE_TOKEN=local-secret uv run lead-intake
curl -sS -X POST http://127.0.0.1:8765/intake \
  -H 'content-type: application/json' -H 'x-intake-token: local-secret' \
  --data-binary @fixtures/ordinary-lead.json
uv run pytest
```

Use a new `INTAKE_DB` path to reset all state. Run one worker per database file; the current app does not coordinate concurrent workers. The webhook token is required for each intake request. Missing model credentials fail explicitly; no fake model response is substituted.

## Pipedream Connect path

Set `CONNECTOR_MODE=pipedream`, `ALLOW_PIPEDREAM_ACTIONS=1` and the values in `.env.example` for a **development** Pipedream project with connected test Attio and Slack accounts. The adapter calls `attio-create-update-record` with unique `email_addresses` matching and `slack_v2-post-message` in a channel selected by Gemini's route. These action keys and inputs are documented by Pipedream; the exact account-specific Attio schema, auth and results still require live qualification. The service never falls back to synthetic actions if a Pipedream call fails.

The event ledger stores the decision before actions and records CRM completion separately. If a Slack call starts but times out or the worker dies, its outcome cannot safely be inferred. The event moves to `review_required` (or remains `notification_started` after a crash), and replays return HTTP 409 until an operator checks the remote channel. This intentionally avoids an automatic second post. The remote Attio action is an upsert by email, but another remote side effect may still occur if a response is lost.

The local adapter is an application-side replacement for the two Pipedream actions, not an interception of Pipedream's hosted execution. A Firedrill connection will be added only after its new release and official contract are verified.

## Evidence

`scenarios/README.md` records normal, incomplete, replay, partial failure and uncertain-outcome cases. Offline tests cover HTTP validation and local state behavior. No live Gemini, Pipedream, Attio or Slack call has been executed here; model quality, external schemas and usage remain unmeasured. No public license has been selected.
