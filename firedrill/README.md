# Firedrill agent-first integration

The optional execution toolchain exact-pins released CLI and TypeScript SDK **2.2.2**, verified October 19, 2026. Node 22+ is required; application/provider dependencies remain in the original Python lockfile. The supported command target invokes the application's real ASGI factory/router through production HTTPX ASGI transport. It does not start a hosted HTTP server or inject a classifier/action implementation.

## Prepare and authorize

From the repository root with `uv` on PATH:

```sh
uv sync --locked --dev
npm ci --prefix firedrill --ignore-scripts --no-audit --no-fund
npm test --prefix firedrill
firedrill_cli=firedrill/node_modules/@firedrill-run/cli/bin/firedrill.mjs
node "$firedrill_cli" validate firedrill/scenario.json --json --non-interactive
```

Offline validation allocates no batch. Select the actual project/environment with `node "$firedrill_cli" init` in a human terminal. Automation supplies `FIREDRILL_TOKEN`, `FIREDRILL_PROJECT_ID` and `FIREDRILL_ENVIRONMENT_ID` through its secret store. New lead classification independently needs `GEMINI_API_KEY` and an enabled `GEMINI_MODEL` or recipe `configuration.parameters.model`. Keep credentials out of recipes and source.

## Execute the real webhook application

With an authorized model budget:

```sh
node firedrill/simulate.mjs --preview
node "$firedrill_cli" simulate firedrill/scenario.json --scenario ordinary --json --non-interactive
```

The adapter uses the actual model-free CLI initializer, a private SQLite database/form, the normal app factory/lifespan, authenticated `POST /intake` and correlated `GET /events/{event_id}`. The default Gemini classifier and actual synthetic connectors are unchanged. A fresh private application token authorizes only these local ASGI requests; it is neither logged nor derived from Firedrill operator authority.

The recipe contains ordinary/unclear-owner inquiries, completed-event replay and uncertain-notification review, with one repetition and concurrency one. `node firedrill/simulate.mjs` runs the same full recipe through the SDK. Completed replay and uncertain prior work make no fresh model call; imported fictional decisions are not new provider measurements.

The adapter explicitly fixes `CONNECTOR_MODE=synthetic` and disables Pipedream actions, including when the caller has ambient hosted configuration. Case input cannot select a live connector. Actual Pipedream account/schema/action qualification remains a separate application path and gate. No production CRM, Slack channel or mailbox is connected.

HTTP 200 with the exact completed event establishes application completion. An observed HTTP 409 for uncertain prior work remains an inspectable review outcome with its actual unknown stage and no automatic action retry. Model/infrastructure failures return nonzero status with actual failed-attempt evidence. A completed callback or review outcome is not a correctness score or confirmed notification delivery.

Each invocation retains a private `.runs/firedrill/attempt-*` directory, exact outer run/attempt/scenario mapping in `invocation.json`, SQLite state and original form. New attempts never overwrite old state. Output contains actual HTTP responses/event inspection under `application`, plus runner-local paths under `localState`; paths are not uploaded artifacts. Framework lifespan closure is recorded only after it returns. Interruption can leave a started attempt or earlier committed effect: inspect state before retrying. Process stop does not prove remote generation/billing stopped or effects rolled back.

The command envelope uses `2026-10-17-target-invocation.1`, ID-only scenario context and explicit case input. Controller/evaluation data and unsupported Firedrill Tool bindings are rejected. Operator/CI credentials are removed before application dispatch. These local connectors need no Firedrill synthetic Tool. In-process ASGI checks are distinct from hosted-network, live Gemini and hosted Pipedream proof.

## Operator MCP, recovery and CI

Configure remote OAuth MCP **https://mcp.firedrill.run/mcp** with the [host-specific setup](https://docs.firedrill.run/guides/coding-agents); no local MCP package is required. Host OAuth and local CLI/SDK login are separate. MCP prepares/queues/inspects; the local execution client runs this application. Attach an existing batch with `node firedrill/simulate.mjs --run BATCH_ID`.

Preserve operation IDs and private journals after uncertainty. Use `--resume OPERATION_ID` for the exact SDK execution or `--result BATCH_ID` for retained reads. A fresh simulation is new work. The local invocation mapping is a locator, separate from SDK recovery authority. Runner exit codes preserve errors, pending work/assessment and optional gates; this observation-only recipe requests no grading or gate.

Ordinary CI is offline. Configure actual scope, protected credentials and budgets before using the [live CI generator](https://docs.firedrill.run/guides/ci). Authenticated persisted success/negative runs, live model quality/cost, real connector proof, publication and licensing remain separate gates.

References: [SDK](https://docs.firedrill.run/sdk/typescript), [command targets](https://docs.firedrill.run/guides/callbacks), [operator MCP](https://docs.firedrill.run/guides/control-mcp).
