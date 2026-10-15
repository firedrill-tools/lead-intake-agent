"""Webhook request orchestration with durable per-event progress."""
import hmac
import json
import os
import sqlite3
from pathlib import Path
from typing import Callable
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field
from .connectors import PipedreamActions, SyntheticActions
from .model import Decision, decide


class LeadEvent(BaseModel):
    event_id: str = Field(pattern=r"^[A-Za-z0-9_-]{4,80}$")
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    company: str = Field(min_length=2, max_length=120)
    message: str = Field(min_length=10, max_length=2000)


def create_app(db_path: Path | None = None, classifier: Callable[[dict], Decision] = decide,
               actions=None, token: str | None = None) -> FastAPI:
    db_path = db_path or Path(os.getenv("INTAKE_DB", "state.sqlite"))
    token = token if token is not None else os.getenv("INTAKE_TOKEN")
    if not token:
        raise RuntimeError("INTAKE_TOKEN required")
    mode = os.getenv("CONNECTOR_MODE", "synthetic")
    if mode not in {"synthetic", "pipedream"}:
        raise RuntimeError("CONNECTOR_MODE must be synthetic or pipedream")
    if mode == "pipedream" and os.getenv("ALLOW_PIPEDREAM_ACTIONS") != "1":
        raise RuntimeError("Pipedream mode requires ALLOW_PIPEDREAM_ACTIONS=1")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, decision TEXT, stage TEXT NOT NULL, error TEXT)")
    conn.commit()
    actions = actions or (SyntheticActions(conn) if mode == "synthetic" else PipedreamActions())
    app = FastAPI(title="Lead intake agent")

    @app.post("/intake")
    def intake(event: LeadEvent, x_intake_token: str = Header(default="")):
        if not hmac.compare_digest(x_intake_token, token):
            raise HTTPException(401, "invalid intake token")
        payload = event.model_dump(mode="json")
        payload["email"] = payload["email"].lower()
        encoded = json.dumps(payload, sort_keys=True)
        row = conn.execute("SELECT payload, decision, stage FROM events WHERE event_id=?", (event.event_id,)).fetchone()
        if row and row[0] != encoded:
            raise HTTPException(409, "event ID reused with different payload")
        if row and row[2] == "complete":
            return {"event_id": event.event_id, "stage": "complete", "decision": json.loads(row[1])}
        if row and row[2] in {"notification_started", "review_required"}:
            raise HTTPException(409, "notification outcome uncertain; review before retry")
        if not row:
            conn.execute("INSERT INTO events VALUES (?, ?, NULL, 'received', NULL)", (event.event_id, encoded))
            conn.commit()
            stage = "received"
        else:
            stage = row[2]

        if stage == "received":
            decision = classifier(payload)
            conn.execute("UPDATE events SET decision=?, stage='decided' WHERE event_id=?",
                         (decision.model_dump_json(), event.event_id))
            conn.commit()
        else:
            decision = Decision.model_validate_json(row[1])
        if stage in {"received", "decided"}:
            try:
                actions.upsert_person(payload, decision.summary)
            except Exception as exc:
                conn.execute("UPDATE events SET error=? WHERE event_id=?", (str(exc), event.event_id))
                conn.commit()
                raise HTTPException(502, "CRM action failed; retry with same event ID") from exc
            conn.execute("UPDATE events SET stage='crm_done', error=NULL WHERE event_id=?", (event.event_id,))
            conn.commit()

        notice = f"[{decision.route}] {payload['name']} ({payload['email']}) at {payload['company']}: {decision.summary}"
        conn.execute("UPDATE events SET stage='notification_started' WHERE event_id=?", (event.event_id,))
        conn.commit()
        try:
            actions.notify(event.event_id, decision.route, notice)
        except Exception as exc:
            conn.execute("UPDATE events SET stage='review_required', error=? WHERE event_id=?", (str(exc), event.event_id))
            conn.commit()
            raise HTTPException(502, "notification outcome uncertain; manual review required") from exc
        conn.execute("UPDATE events SET stage='complete', error=NULL WHERE event_id=?", (event.event_id,))
        conn.commit()
        return {"event_id": event.event_id, "stage": "complete", "decision": decision.model_dump()}

    @app.get("/health")
    def health():
        return {"status": "ok", "connector_mode": mode}

    return app
