import sqlite3
import pytest
from fastapi.testclient import TestClient
from lead_intake.model import Decision, decide
from lead_intake.service import create_app

LEAD = {"event_id": "form-1001", "name": "Ari Patel", "email": "ari@cedar.example",
        "company": "Cedar Freight", "message": "We need exception routing before the holiday peak."}


def app(tmp_path, actions=None):
    calls = []
    def classify(lead):
        calls.append(lead)
        return Decision(route="sales", summary="Cedar needs exception routing before the holiday peak.",
                        reason="Operational need and timing are explicit")
    path = tmp_path / "state.sqlite"
    return TestClient(create_app(path, classify, actions, token="secret")), path, calls


def submit(client, lead=LEAD, token="secret"):
    return client.post("/intake", json=lead, headers={"x-intake-token": token})


def test_success_and_replay_are_single_effect(tmp_path):
    client, path, calls = app(tmp_path)
    first = submit(client)
    assert first.status_code == 200
    assert submit(client).status_code == 200
    assert len(calls) == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM contacts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
    assert conn.execute("SELECT stage FROM events").fetchone()[0] == "complete"


def test_auth_validation_and_conflicting_replay(tmp_path):
    client, _, calls = app(tmp_path)
    assert submit(client, token="wrong").status_code == 401
    invalid = dict(LEAD, email="bad", company="")
    assert submit(client, invalid).status_code == 422
    assert calls == []
    assert submit(client).status_code == 200
    assert submit(client, dict(LEAD, email="other@cedar.example")).status_code == 409


def test_crm_retry_reuses_decision(tmp_path):
    class Flaky:
        attempts = 0
        notifications = 0
        def upsert_person(self, *_):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporary CRM failure")
        def notify(self, *_):
            self.notifications += 1
    actions = Flaky()
    client, _, calls = app(tmp_path, actions)
    assert submit(client).status_code == 502
    assert submit(client).status_code == 200
    assert len(calls) == 1
    assert actions.attempts == 2
    assert actions.notifications == 1


def test_uncertain_notification_requires_review(tmp_path):
    class Timeout:
        def upsert_person(self, *_):
            pass
        def notify(self, *_):
            raise TimeoutError("no response")
    client, path, _ = app(tmp_path, Timeout())
    assert submit(client).status_code == 502
    assert submit(client).status_code == 409
    assert sqlite3.connect(path).execute("SELECT stage FROM events").fetchone()[0] == "review_required"


def test_live_mode_and_model_require_explicit_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTOR_MODE", "pipedream")
    monkeypatch.delenv("ALLOW_PIPEDREAM_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="ALLOW_PIPEDREAM_ACTIONS"):
        create_app(tmp_path / "other.sqlite", token="secret")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        decide(LEAD)
