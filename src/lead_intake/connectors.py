"""Synthetic actions or real Pipedream Connect actions, selected at startup."""
import os
import sqlite3
from pipedream import Pipedream


class SyntheticActions:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute("CREATE TABLE IF NOT EXISTS contacts (email TEXT PRIMARY KEY, name TEXT, company TEXT, summary TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS notifications (event_id TEXT PRIMARY KEY, channel TEXT, text TEXT)")
        self.conn.commit()

    def upsert_person(self, lead: dict, summary: str):
        self.conn.execute("INSERT INTO contacts VALUES (?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET name=excluded.name, company=excluded.company, summary=excluded.summary",
                          (lead["email"], lead["name"], lead["company"], summary))
        self.conn.commit()
        return {"email": lead["email"]}

    def notify(self, event_id: str, channel: str, text: str):
        self.conn.execute("INSERT OR IGNORE INTO notifications VALUES (?, ?, ?)", (event_id, channel, text))
        self.conn.commit()
        return {"event_id": event_id, "channel": channel}


class PipedreamActions:
    """Calls two registry actions; authentication and schema require live qualification."""
    def __init__(self):
        required = ["PIPEDREAM_PROJECT_ID", "PIPEDREAM_CLIENT_ID", "PIPEDREAM_CLIENT_SECRET",
                    "PIPEDREAM_EXTERNAL_USER_ID", "PIPEDREAM_ATTIO_AUTH_PROVISION_ID",
                    "PIPEDREAM_SLACK_AUTH_PROVISION_ID", "PIPEDREAM_SLACK_SALES_CHANNEL",
                    "PIPEDREAM_SLACK_PARTNERSHIPS_CHANNEL", "PIPEDREAM_SLACK_REVIEW_CHANNEL"]
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise RuntimeError("Missing Pipedream configuration: " + ", ".join(missing))
        self.client = Pipedream(client_id=os.environ["PIPEDREAM_CLIENT_ID"],
                                client_secret=os.environ["PIPEDREAM_CLIENT_SECRET"],
                                project_id=os.environ["PIPEDREAM_PROJECT_ID"],
                                project_environment="development")
        self.user_id = os.environ["PIPEDREAM_EXTERNAL_USER_ID"]

    def upsert_person(self, lead: dict, summary: str):
        # Attio's unique email matching prevents duplicate people on webhook retries.
        return self.client.actions.run(
            id="attio-create-update-record", external_user_id=self.user_id,
            configured_props={
                "attio": {"authProvisionId": os.environ["PIPEDREAM_ATTIO_AUTH_PROVISION_ID"]},
                "objectId": "people", "matchingAttribute": "email_addresses",
                "values": {"email_addresses": [lead["email"]], "name": lead["name"],
                           "description": summary},
            })

    def notify(self, event_id: str, channel: str, text: str):
        return self.client.actions.run(
            id="slack_v2-post-message", external_user_id=self.user_id,
            configured_props={
                "slack_v2": {"authProvisionId": os.environ["PIPEDREAM_SLACK_AUTH_PROVISION_ID"]},
                "channel": os.environ[{"sales": "PIPEDREAM_SLACK_SALES_CHANNEL",
                                        "partnerships": "PIPEDREAM_SLACK_PARTNERSHIPS_CHANNEL",
                                        "review": "PIPEDREAM_SLACK_REVIEW_CHANNEL"}[channel]],
                "text": text,
            })
