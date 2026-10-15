"""Durable event/model/action observations; application serializes writers."""
import json
import sqlite3


def initialize(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, decision TEXT, stage TEXT NOT NULL, error TEXT)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    for name in ("model_evidence", "dataset_context", "connector_scope", "scope_sha256", "crm_receipt", "notification_receipt"):
        if name not in columns:
            conn.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS model_attempts (event_id TEXT, attempt INTEGER, status TEXT, observation TEXT, usage TEXT, wall_ms REAL, PRIMARY KEY(event_id, attempt))")
    conn.execute("CREATE TABLE IF NOT EXISTS action_attempts (event_id TEXT, attempt INTEGER, operation TEXT, status TEXT, observation TEXT, PRIMARY KEY(event_id, attempt))")
    conn.execute("CREATE TABLE IF NOT EXISTS dataset_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()


def decode(value):
    return json.loads(value) if value is not None else None


def view(conn, event_id):
    row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        return None
    event = dict(row)
    for key in ("payload", "decision", "model_evidence", "dataset_context", "connector_scope", "crm_receipt", "notification_receipt"):
        event[key] = decode(event[key])
    models = [dict(row) for row in conn.execute("SELECT attempt, status, observation, usage, wall_ms FROM model_attempts WHERE event_id=? ORDER BY attempt", (event_id,))]
    for item in models:
        item["observation"], item["usage"] = decode(item["observation"]), decode(item["usage"])
    actions = [dict(row) for row in conn.execute("SELECT attempt, operation, status, observation FROM action_attempts WHERE event_id=? ORDER BY attempt", (event_id,))]
    for item in actions:
        item["observation"] = decode(item["observation"])
    event["model_attempts"], event["action_attempts"] = models, actions
    keys = ("prompt_token_count", "candidates_token_count", "total_token_count")
    complete = bool(models) and all(isinstance(item["usage"], dict) and
        all(type(item["usage"].get(key)) is int and item["usage"][key] >= 0 for key in keys) for item in models)
    event["all_attempt_usage_complete"] = complete
    event["all_attempt_usage"] = {key: sum(item["usage"][key] for item in models) for key in keys} if complete else None
    event["cost_usd"] = None
    return event
