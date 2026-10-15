"""Authenticated, serialized webhook execution with durable phase evidence."""
from contextlib import asynccontextmanager
from functools import partial
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from time import perf_counter
from typing import Callable

from fastapi import FastAPI, Header, HTTPException
from .connectors import PipedreamActions, SyntheticActions
from .contracts import ActionFailure, ActionReceipt, Decision, DecisionResult, LeadEvent
from .model import decide
from . import store


def create_app(db_path: Path | None = None, classifier: Callable | None = None,
               actions=None, token: str | None = None) -> FastAPI:
    db_path = Path(db_path) if db_path is not None else Path(os.getenv("INTAKE_DB", "state.sqlite"))
    if str(db_path) == ":memory:":
        raise ValueError("A durable SQLite file is required")
    token = token if token is not None else os.getenv("INTAKE_TOKEN")
    if not token or not token.strip():
        raise RuntimeError("INTAKE_TOKEN required")
    configured_mode = os.getenv("CONNECTOR_MODE", "synthetic")
    if configured_mode not in {"synthetic", "pipedream"}:
        raise RuntimeError("CONNECTOR_MODE must be synthetic or pipedream")
    if configured_mode == "pipedream" and os.getenv("ALLOW_PIPEDREAM_ACTIONS") != "1":
        raise RuntimeError("Pipedream mode requires ALLOW_PIPEDREAM_ACTIONS=1")
    selected_classifier = classifier or partial(decide, model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    lock = RLock()

    @asynccontextmanager
    async def lifespan(app):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if not db_path.exists():
            descriptor = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        selected_actions = None
        try:
            store.initialize(conn)
            if configured_mode == "pipedream" and conn.execute("SELECT 1 FROM dataset_metadata WHERE key='context'").fetchone():
                raise RuntimeError("Synthetic case initialization resets local SQLite only; use a fresh database for Pipedream")
            selected_actions = actions if actions is not None else (SyntheticActions(conn) if configured_mode == "synthetic" else PipedreamActions())
            scope = selected_actions.scope() if hasattr(selected_actions, "scope") else {"mode": "injected", "adapter": type(selected_actions).__qualname__}
            scope_json = json.dumps(scope, sort_keys=True)
            app.state.conn, app.state.actions, app.state.scope = conn, selected_actions, scope
            app.state.scope_json, app.state.scope_sha256 = scope_json, sha256(scope_json.encode()).hexdigest()
            app.state.closed = False
            yield
        finally:
            try:
                if actions is None and selected_actions is not None and hasattr(selected_actions, "close"):
                    selected_actions.close()
            finally:
                conn.close()
                app.state.closed = True

    app = FastAPI(title="Lead intake agent", lifespan=lifespan)

    def authorize(supplied):
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "invalid intake token")

    def answer(event_id, *, replayed=False, model_called=False):
        result = store.view(app.state.conn, event_id)
        result.update({"replayed": replayed, "classifier_called_this_request": model_called})
        return result

    @app.post("/intake")
    def intake(event: LeadEvent, x_intake_token: str = Header(default="")):
        authorize(x_intake_token)
        # FastAPI sync endpoints run in a thread pool even with one worker.
        # Serialize the full task, not just individual SQLite statements.
        with lock:
            conn, selected = app.state.conn, app.state.actions
            payload = event.model_dump(mode="json")
            payload["email"] = payload["email"].lower()
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            row = conn.execute("SELECT * FROM events WHERE event_id=?", (event.event_id,)).fetchone()
            if row and row["payload"] != encoded:
                raise HTTPException(409, "event ID reused with different payload")
            if row and row["stage"] == "complete":
                return answer(event.event_id, replayed=True)
            if row and row["scope_sha256"] != app.state.scope_sha256:
                raise HTTPException(409, "connector execution scope changed or is unknown; review before resuming")
            if row and row["stage"] in {"crm_started", "crm_review_required", "notification_started", "review_required"}:
                raise HTTPException(409, "action outcome uncertain; inspect event evidence and remote state before retry")
            stage = row["stage"] if row else "received"
            if stage not in {"received", "decided", "crm_done"}:
                raise HTTPException(409, "unsupported event progress; review before resuming")
            if not row:
                metadata = conn.execute("SELECT value FROM dataset_metadata WHERE key='context'").fetchone()
                context = store.decode(metadata[0]) if metadata else None
                if context and context.get("request_sha256") != sha256(encoded.encode()).hexdigest():
                    context = {"initial_dataset": context.get("dataset"), "case_match": False}
                conn.execute("INSERT INTO events (event_id,payload,stage,connector_scope,scope_sha256,dataset_context) VALUES (?,?,'received',?,?,?)",
                    (event.event_id, encoded, app.state.scope_json, app.state.scope_sha256, json.dumps(context) if context else None))
                conn.commit()
            model_called = False
            if stage == "received":
                attempt = conn.execute("SELECT coalesce(max(attempt),0)+1 FROM model_attempts WHERE event_id=?", (event.event_id,)).fetchone()[0]
                conn.execute("INSERT INTO model_attempts(event_id,attempt,status) VALUES (?,?,'started')", (event.event_id,attempt))
                conn.commit()
                started = perf_counter()
                model_called = True
                try:
                    classified = selected_classifier(payload)
                    if isinstance(classified, Decision):
                        classified = DecisionResult(decision=classified, observation={"source": "injected_classifier", "reported_usage": None, "cost_usd": None})
                    if not isinstance(classified, DecisionResult):
                        raise TypeError("Classifier must return DecisionResult")
                    decision = classified.decision
                except Exception as error:
                    evidence = getattr(error, "observation", {"source": "classifier_error", "error_type": type(error).__name__, "reported_usage": None, "cost_usd": None})
                    failed_usage = evidence.get("reported_usage") if evidence.get("usage_complete") else None
                    conn.execute("UPDATE model_attempts SET status='failed',observation=?,usage=?,wall_ms=? WHERE event_id=? AND attempt=?",
                        (json.dumps(evidence), json.dumps(failed_usage) if failed_usage is not None else None, (perf_counter()-started)*1000, event.event_id,attempt))
                    conn.execute("UPDATE events SET error=? WHERE event_id=?", (f"model:{type(error).__name__}",event.event_id))
                    conn.commit()
                    raise HTTPException(502, "model did not produce a completed decision; inspect event evidence") from error
                conn.execute("UPDATE model_attempts SET status='success',observation=?,usage=?,wall_ms=? WHERE event_id=? AND attempt=?",
                    (json.dumps(classified.observation),json.dumps(classified.usage) if classified.usage is not None else None,
                     (perf_counter()-started)*1000,event.event_id,attempt))
                conn.execute("UPDATE events SET decision=?,model_evidence=?,stage='decided',error=NULL WHERE event_id=?",
                    (decision.model_dump_json(),classified.model_dump_json(),event.event_id))
                conn.commit()
            else:
                decision = Decision.model_validate_json(row["decision"])

            def perform(operation, invoke, target, next_stage, receipt_column):
                attempt = conn.execute("SELECT coalesce(max(attempt),0)+1 FROM action_attempts WHERE event_id=?", (event.event_id,)).fetchone()[0]
                conn.execute("INSERT INTO action_attempts(event_id,attempt,operation,status) VALUES (?,?,?,'started')", (event.event_id,attempt,operation))
                conn.execute("UPDATE events SET stage=? WHERE event_id=?", ("crm_started" if operation == "upsert_person" else "notification_started",event.event_id))
                conn.commit()
                try:
                    receipt = invoke()
                    receipt = receipt if isinstance(receipt, ActionReceipt) else ActionReceipt.model_validate(receipt)
                    key = payload["email"] if operation == "upsert_person" else event.event_id
                    if receipt.operation != operation or receipt.request_key != key or receipt.target != target:
                        raise ActionFailure("Action acknowledgement does not match the requested operation/target")
                    if app.state.scope["mode"] in {"synthetic", "pipedream"} and receipt.provider != app.state.scope["mode"]:
                        raise ActionFailure("Action acknowledgement provider differs from configured scope")
                except Exception as error:
                    uncertain = getattr(error, "uncertain", True)
                    failed_stage = ("crm_review_required" if uncertain else "decided") if operation == "upsert_person" else "review_required"
                    evidence = getattr(error, "observation", {"error_type": type(error).__name__})
                    conn.execute("UPDATE action_attempts SET status=?,observation=? WHERE event_id=? AND attempt=?",
                        ("uncertain" if uncertain else "rejected",json.dumps(evidence),event.event_id,attempt))
                    conn.execute("UPDATE events SET stage=?,error=? WHERE event_id=?", (failed_stage,f"{operation}:{type(error).__name__}",event.event_id))
                    conn.commit()
                    raise HTTPException(502, "action was not acknowledged; inspect event evidence before retry") from error
                conn.execute("UPDATE action_attempts SET status='acknowledged',observation=? WHERE event_id=? AND attempt=?",
                    (receipt.model_dump_json(),event.event_id,attempt))
                conn.execute(f"UPDATE events SET stage=?,{receipt_column}=?,error=NULL WHERE event_id=?", (next_stage,receipt.model_dump_json(),event.event_id))
                conn.commit()

            if stage in {"received", "decided"}:
                perform("upsert_person", lambda: selected.upsert_person(payload,decision.summary), payload["email"], "crm_done", "crm_receipt")
            notice = f"[{decision.route}] {payload['name']} ({payload['email']}) at {payload['company']}: {decision.summary}"
            target = selected.target_for_route(decision.route) if hasattr(selected,"target_for_route") else decision.route
            perform("notify", lambda: selected.notify(event.event_id,decision.route,notice), target, "complete", "notification_receipt")
            return answer(event.event_id,model_called=model_called)

    @app.get("/events/{event_id}")
    def inspect_event(event_id: str, x_intake_token: str = Header(default="")):
        authorize(x_intake_token)
        # Separate read connection can observe committed phases while a task is
        # running. It cannot mutate the shared action connection.
        with sqlite3.connect(db_path) as reader:
            reader.row_factory = sqlite3.Row
            result = store.view(reader,event_id)
        if result is None:
            raise HTTPException(404,"event not found")
        return result

    @app.get("/health")
    def health():
        return {"status": "ok", "connector_mode": app.state.scope["mode"]}

    return app
