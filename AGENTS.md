# Agent working notes

- Entrypoint: `lead-intake`; FastAPI app in `src/lead_intake/service.py`.
- The model decision is `src/lead_intake/model.py`; it calls Gemini only after Pydantic input validation and webhook authentication.
- Dependency seam: `src/lead_intake/connectors.py` selects local SQLite actions or Pipedream Connect actions at startup. Never silently switch modes.
- State: SQLite `events`, `contacts`, `notifications`; use a new file for independent cases. Run one worker. For remote connector mode, inspect uncertain notification outcomes manually.
- Live mode needs a Pipedream development project, connected test Attio and Slack accounts, exact auth provision IDs and channel IDs. Do not use production accounts.
- No Firedrill integration exists until the new interface is qualified. Do not copy retired APIs or claim model results from offline tests.
