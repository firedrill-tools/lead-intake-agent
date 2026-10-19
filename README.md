# Lead intake agent

An authenticated form webhook asks Gemini to route an inbound inquiry to sales, partnerships or review. The service then upserts one contact and posts one team notice. SQLite records the original decision, every model/action attempt, acknowledgements and replay state. Synthetic SQLite actions are the default; an explicitly enabled Pipedream Connect adapter uses development Attio and Slack accounts.

## Run one form

Requires Python 3.12 and `uv`. Listing and initialization need no credentials and make no model or external connector calls.

```sh
uv sync --dev --locked
uv run lead-intake --list-cases
uv run lead-intake --init-case busy-existing --db run-output/busy.sqlite --request-out run-output/form.json
GEMINI_API_KEY=... INTAKE_TOKEN=local-secret uv run lead-intake --db run-output/busy.sqlite
```

In another terminal:

```sh
curl -sS -X POST http://127.0.0.1:8765/intake \
  -H 'content-type: application/json' -H 'x-intake-token: local-secret' \
  --data-binary @run-output/form.json
curl -sS http://127.0.0.1:8765/events/form-1001 -H 'x-intake-token: local-secret'
```

The POST completes the task synchronously. `stage: complete` means both actions returned matching acknowledgements. A complete unchanged replay returns the stored decision/evidence with `replayed: true` and `classifier_called_this_request: false`. Changing an existing event's input returns 409. Authentication, input validation and replay checks precede model/action execution. Missing model credentials never select a canned answer. Inspect durable progress with authenticated `GET /events/{event_id}` after an error or lost HTTP response.

The default model is `gemini-2.5-flash`, configurable through `GEMINI_MODEL`. Its availability and task quality still need a live proof. The actual `google-genai==2.31.0` GenerateContent path requests one JSON-schema candidate, at most 2,048 output tokens, a 30-second HTTP timeout and one HTTP attempt. Only one unblocked `STOP` candidate with a valid decision is accepted; usable-looking JSON from `MAX_TOKENS` is rejected. Raw finish/block state, output, response/model IDs and provider-reported usage survive rejected responses. Missing/partial usage and cost remain unknown. Total tokens can include thought tokens; totals are taken from the provider rather than recomputed from input/output.

## Datasets and reset

`northstar-inbound-leads` **1.0.0** has ten cases across four profiles:

| Profile | Cases | Starting state |
| --- | --- | --- |
| Starter | `ordinary`, `partnership` | Empty local CRM/notification state |
| Busy | `busy-existing`, `busy-new` | Twelve contacts from different companies, two completed prior events/notices; existing-contact update or new-contact addition |
| Ambiguous | `unclear-owner`, `conflicting-category` | Unclear ownership or contradictory commercial intent |
| Boundary | `incomplete`, `hostile-text`, `prior-complete`, `uncertain-notice` | Invalid form, untrusted instructions, completed replay, or a notice whose acknowledgement was lost |

Ordinary and busy-existing deliberately use the same form against different starting state. These are ten situations, not ten paid executions. The model receives only the validated selected form and routing instructions; it cannot read the CRM population, fixture history or scenario criteria. Classification quality on ambiguity/hostile text remains unmeasured.

The manifest binds each request and seed by SHA-256. Listing validates identities, all file hashes, bounded paths and coherent contact/history relationships. Initialization refuses existing files, creates private files and records dataset/version/profile/case plus request/seed hashes. Modified input does not inherit a case identity. Historical decisions are marked `synthetic_fixture_history`, with no model attempts or invented usage. The uncertain-notice case includes an existing local notice but no acknowledgement in the event ledger.

Choose a **new database file for each independent case**. Initialization resets local SQLite only; it cannot reset Pipedream, Attio or Slack. The service rejects initialized synthetic case databases in Pipedream mode. A remote proof must use a fresh database and separately prepare/inspect the connected accounts. `scripts/build_fixtures.py` reproduces every qualified JSON byte. The built wheel bundles the same data; `--request-out` provides a portable copy.

## Execution, replay and ownership

Run **one application instance and one worker per database**. A process-local lock serializes full intake tasks, including synchronous FastAPI thread-pool requests. It is not a lock between processes. In-flight reads use a separate SQLite connection and expose committed progress.

| Durable state | Same-input replay |
| --- | --- |
| `received`, failed model attempt | Explicit retry calls Gemini again; prior attempt/output/usage remain visible |
| `decided`, known rejection before CRM mutation | Reuse decision and attempt CRM again |
| `crm_done` | Reuse decision/contact receipt; proceed to notice |
| `crm_started`, `crm_review_required` | 409; review remote contact outcome |
| `notification_started`, `review_required` | 409; review channel outcome before any further post |
| `complete` | Return original stored result; no action/model call |

Unknown exceptions and unverified remote acknowledgements are treated as uncertain. Remote HTTP failures do not prove that a mutation did not happen. Connector bindings are captured at startup and hashed into each event; pending work cannot resume under changed or unknown bindings. Legacy complete events can replay with unknown historical measurements; pending legacy events need review.

There is no automatic operator override for uncertain actions. Inspect the event and actual remote state, then reconcile the ledger with independently verified receipts before resuming. Deleting an event or resetting a local file does not undo a remote write. A client disconnect or stopped observer does not cancel a running synchronous handler. The application starts no job or scheduler. ASGI lifespan owns/closes its SQLite connection and constructed connector; each model call closes its constructed Gemini client. Injected clients/adapters remain caller-owned.

## Pipedream development path

Set `CONNECTOR_MODE=pipedream`, `ALLOW_PIPEDREAM_ACTIONS=1` and the explicit values in `.env.example`. Use a development Pipedream project with connected test Attio/Slack accounts, exact external-user/auth provision IDs and exact `C…`/`G…` channel IDs. The adapter captures configuration once, uses the configured client's OAuth credentials, disables automatic HTTP retries/redirects and uses 30-second request timeouts. An ambient Pipedream access token cannot override this binding. `PIPEDREAM_BASE_URL` optionally selects an HTTPS endpoint or loopback HTTP test endpoint.

The pinned native adapter calls these source-inspected components:

- [`attio-create-update-record` 0.1.1](https://github.com/PipedreamHQ/pipedream/blob/master/components/attio/actions/create-update-record/create-update-record.mjs): `attio` auth, `people` object, unique `email_addresses` match, name and a company/summary description. It verifies a returned record ID and matching email. It does not create/link a company record. The submitted full name is passed as an Attio name string; it does not infer culturally dependent first/last names. See [Attio's personal-name syntax](https://docs.attio.com/rest-api/attribute-types/attribute-types-personal-name).
- [`slack_v2-post-message` 0.1.6](https://github.com/PipedreamHQ/pipedream/blob/master/components/slack_v2/actions/post-message/post-message.mjs): **`slack`** auth prop, explicit route channel, plain text and disabled link/media unfurls. It verifies `ok: true`, the exact channel and a nonblank message timestamp.

The [Connect run-action response](https://pipedream.com/docs/connect/api-reference/run-action) can contain action errors in `os`; HTTP 200 alone is insufficient. Raw action return/observation envelopes and identity-bound receipts are retained. Account-specific schemas, component availability, OAuth grants and actual backend effects still need connected-account qualification. This adapter never falls back to synthetic actions on failure. Local SQLite actions model contact upsert and event-keyed notices; they are not interception of Pipedream's hosted execution.

## Verification and remaining gates

```sh
uv run --no-sync pytest -q
uv build --wheel
uv run --no-sync python scripts/check_wheel.py
```

**59 offline checks passed.** Tests retain the actual Gemini and Pipedream SDKs and replace only their HTTP transports. They exercise native request/response parsing, completed/rejected output, missing/zero usage, one HTTP attempt on provider errors, OAuth/action configuration, receipt identity checks, client cleanup, concurrent duplicate intake, failed-model retry evidence, legacy recovery and all ten initialized situations. A full ASGI request runs both actual SDK paths and an unchanged replay; another preserves usage from a rejected Gemini attempt and its successful explicit retry. Classifier-injected fixture tests prove local state behavior, not model judgment. The wheel is exercised outside the checkout without credentials.

No live Gemini/Pipedream/Attio/Slack call is claimed. Usage in controlled responses is synthetic evidence of parser behavior, not billing. [Scenario briefs](scenarios/README.md) describe task-specific criteria without a fabricated overall score. The new Firedrill release/contract, provider access, connected test accounts and publication/license choices remain external gates. A thin Firedrill integration will follow the qualified release; no retired API is included.

Related applications: [Firedrill example catalog](https://github.com/firedrill-tools/firedrill-example-catalog). Product/setup references: [Firedrill](https://firedrill.run/) and [official developer documentation](https://docs.firedrill.run/). Match documentation to the qualified release. Gemini completion/usage behavior follows the [GenerateContent API contract](https://ai.google.dev/api/generate-content).

## Current Firedrill connection — 2026-10-19

The [agent-first integration](firedrill/README.md) exact-pins CLI/SDK 2.2.2 and invokes the actual ASGI webhook application with fresh private SQLite state and real default Gemini classifier. It uses the local ASGI callable seam and explicit synthetic connectors. Earlier pending-release notes are historical; authenticated saved runs, live models and hosted Pipedream account proof remain pending.
