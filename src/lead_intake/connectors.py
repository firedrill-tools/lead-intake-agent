"""Synthetic actions or real Pipedream Connect actions, selected at startup."""
from copy import deepcopy
from importlib.metadata import version
import os
import re
from urllib.parse import urlsplit
import httpx
import sqlite3
from pipedream import Pipedream
from .contracts import ActionFailure, ActionReceipt


class SyntheticActions:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute("CREATE TABLE IF NOT EXISTS contacts (email TEXT PRIMARY KEY, name TEXT, company TEXT, summary TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS notifications (event_id TEXT PRIMARY KEY, channel TEXT, text TEXT)")
        self.conn.commit()

    def scope(self):
        return {"mode": "synthetic", "version": 1, "routes": ["sales", "partnerships", "review"]}

    def target_for_route(self, route):
        return route

    def close(self):
        pass  # The application owns the shared SQLite connection.

    def upsert_person(self, lead: dict, summary: str):
        self.conn.execute("INSERT INTO contacts VALUES (?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET name=excluded.name, company=excluded.company, summary=excluded.summary",
                          (lead["email"], lead["name"], lead["company"], summary))
        self.conn.commit()
        return ActionReceipt(operation="upsert_person", request_key=lead["email"], target=lead["email"],
                             receipt_id=lead["email"], provider="synthetic")

    def notify(self, event_id: str, channel: str, text: str):
        existing = self.conn.execute("SELECT channel, text FROM notifications WHERE event_id=?", (event_id,)).fetchone()
        if existing and tuple(existing) != (channel, text):
            raise ActionFailure("Existing notification has different content or target")
        self.conn.execute("INSERT OR IGNORE INTO notifications VALUES (?, ?, ?)", (event_id, channel, text))
        self.conn.commit()
        return ActionReceipt(operation="notify", request_key=event_id, target=channel,
                             receipt_id=event_id, provider="synthetic")


class PipedreamActions:
    """Native Connect actions; success requires an identity-bound returned receipt."""
    ATTIO_ACTION, ATTIO_VERSION = "attio-create-update-record", "0.1.1"
    SLACK_ACTION, SLACK_VERSION = "slack_v2-post-message", "0.1.6"

    def __init__(self, *, transport=None):
        if os.getenv("ALLOW_PIPEDREAM_ACTIONS") != "1":
            raise RuntimeError("Pipedream actions require ALLOW_PIPEDREAM_ACTIONS=1")
        required = ["PIPEDREAM_PROJECT_ID", "PIPEDREAM_CLIENT_ID", "PIPEDREAM_CLIENT_SECRET",
                    "PIPEDREAM_EXTERNAL_USER_ID", "PIPEDREAM_ATTIO_AUTH_PROVISION_ID",
                    "PIPEDREAM_SLACK_AUTH_PROVISION_ID", "PIPEDREAM_SLACK_SALES_CHANNEL",
                    "PIPEDREAM_SLACK_PARTNERSHIPS_CHANNEL", "PIPEDREAM_SLACK_REVIEW_CHANNEL"]
        missing = [key for key in required if not os.getenv(key) or not os.environ[key].strip()]
        if missing:
            raise RuntimeError("Missing Pipedream configuration: " + ", ".join(missing))
        config = {key: os.environ[key] for key in required}
        self.user_id = config["PIPEDREAM_EXTERNAL_USER_ID"]
        self.attio_auth = config["PIPEDREAM_ATTIO_AUTH_PROVISION_ID"]
        self.slack_auth = config["PIPEDREAM_SLACK_AUTH_PROVISION_ID"]
        self.channels = {route: config[f"PIPEDREAM_SLACK_{route.upper()}_CHANNEL"] for route in ("sales", "partnerships", "review")}
        if any(not re.fullmatch(r"[CG][A-Z0-9]{8,}", value) for value in self.channels.values()):
            raise ValueError("Pipedream routes require explicit Slack channel IDs, not names or user IDs")
        base_url = os.getenv("PIPEDREAM_BASE_URL", "https://api.pipedream.com")
        parsed = urlsplit(base_url)
        if not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Pipedream endpoint must use HTTPS (or loopback HTTP), without URL credentials/query/fragment")
        self._scope = {"mode": "pipedream", "sdk_version": version("pipedream"), "project_id": config["PIPEDREAM_PROJECT_ID"],
            "environment": "development", "external_user_id": self.user_id, "endpoint": base_url,
            "auth_provisions": {"attio": self.attio_auth, "slack": self.slack_auth}, "channels": self.channels,
            "actions": {self.ATTIO_ACTION: self.ATTIO_VERSION, self.SLACK_ACTION: self.SLACK_VERSION}}
        self.http = httpx.Client(transport=transport, timeout=30, follow_redirects=False)
        try:
            # Explicit None prevents an ambient, import-time access token from
            # replacing this configured development client's OAuth credentials.
            self.client = Pipedream(access_token=None, client_id=config["PIPEDREAM_CLIENT_ID"],
                client_secret=config["PIPEDREAM_CLIENT_SECRET"], project_id=config["PIPEDREAM_PROJECT_ID"],
                project_environment="development", base_url=base_url, max_retries=0, timeout=30,
                follow_redirects=False, httpx_client=self.http)
        except Exception:
            self.http.close()
            raise

    def scope(self):
        return deepcopy(self._scope)

    def target_for_route(self, route):
        return self.channels[route]

    def close(self):
        self.http.close()

    def _run(self, action, action_version, properties):
        observation = {"action": action, "version": action_version, "response": None}
        try:
            result = self.client.actions.run(id=action, version=action_version, external_user_id=self.user_id,
                configured_props=properties, request_options={"max_retries": 0, "timeout_in_seconds": 30})
            body = result.model_dump(mode="json", exclude_none=True)
            observation["response"] = body
            if body.get("error") or body.get("errors") or any(item.get("err") or item.get("k") == "error" for item in body.get("os", [])):
                raise ActionFailure("Pipedream returned action error observations", observation=observation)
            if not isinstance(result.ret, dict):
                raise ActionFailure("Pipedream returned no verifiable action result", observation=observation)
            return result.ret, observation
        except ActionFailure:
            raise
        except Exception as error:
            observation["error_type"] = type(error).__name__
            observation["http_status"] = getattr(error, "status_code", None)
            raise ActionFailure("Pipedream action outcome is unacknowledged", observation=observation) from error

    def upsert_person(self, lead: dict, summary: str):
        returned, observation = self._run(self.ATTIO_ACTION, self.ATTIO_VERSION, {
            "attio": {"authProvisionId": self.attio_auth}, "objectId": "people", "matchingAttribute": "email_addresses",
            "values": {"email_addresses": [lead["email"]], "name": lead["name"], "description": f"{lead['company']}: {summary}"}})
        record = returned.get("data")
        record = record if isinstance(record, dict) else {}
        identifier = (record.get("id") or {}).get("record_id") if isinstance(record.get("id"), dict) else None
        addresses = (record.get("values") or {}).get("email_addresses", []) if isinstance(record.get("values"), dict) else []
        matched = any(isinstance(item, dict) and str(item.get("email_address", "")).lower() == lead["email"] for item in addresses)
        if not isinstance(identifier, str) or not identifier.strip() or not matched:
            raise ActionFailure("Attio result did not acknowledge the expected contact", observation=observation)
        return ActionReceipt(operation="upsert_person", request_key=lead["email"], target=lead["email"],
            receipt_id=identifier, provider="pipedream", observation=observation)

    def notify(self, event_id: str, channel: str, text: str):
        target = self.target_for_route(channel)
        returned, observation = self._run(self.SLACK_ACTION, self.SLACK_VERSION, {
            "slack": {"authProvisionId": self.slack_auth}, "channel": target, "text": text,
            "mrkdwn": False, "unfurlLinks": False, "unfurlMedia": False})
        timestamp = returned.get("ts")
        if returned.get("ok") is not True or returned.get("channel") != target or not isinstance(timestamp, str) or not timestamp.strip():
            raise ActionFailure("Slack result did not acknowledge the intended channel/message", observation=observation)
        return ActionReceipt(operation="notify", request_key=event_id, target=target,
            receipt_id=timestamp, provider="pipedream", observation=observation)
