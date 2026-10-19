"""Invoke the real ASGI application, default classifier and synthetic connectors.

This is a customer-local callable seam, not a hosted-network or provider stub.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import quote

import httpx
from lead_intake.service import create_app


def captured(response):
    try:
        body = response.json()
    except ValueError:
        body = {"text": response.text}
    return {"status": response.status_code, "body": body}


async def execute(database, request_file):
    if os.getenv("CONNECTOR_MODE") != "synthetic" or os.getenv("ALLOW_PIPEDREAM_ACTIONS") != "0":
        raise RuntimeError("This reviewed integration requires explicit synthetic connector mode")
    with request_file.open("rb") as source:
        raw = source.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("Selected form exceeds 64 KiB")
    request = json.loads(raw)
    token = secrets.token_urlsafe(32)
    # Construct the application's normal factory without test model/action seams.
    app = create_app(db_path=database, token=token)
    observed = {}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://application.local", headers={"x-intake-token": token},
        ) as client:
            health = await client.get("/health")
            if health.status_code != 200 or health.json().get("connector_mode") != "synthetic":
                raise RuntimeError("Application did not confirm its explicit synthetic scope")
            response = await client.post("/intake", json=request)
            observed = {"connectorMode": health.json()["connector_mode"], "http": captured(response), "event": None}
            event_id = request.get("event_id")
            if isinstance(event_id, str):
                state = await client.get("/events/" + quote(event_id, safe=""))
                observed["event"] = captured(state)
    observed["appLifespanClosed"] = app.state.closed
    body = observed["http"]["body"]
    state = observed["event"]
    completed = (observed["http"]["status"] == 200 and isinstance(body, dict)
                 and body.get("event_id") == request.get("event_id") and body.get("stage") == "complete")
    # An observed refusal to retry uncertain prior work is a policy outcome.
    # Keep its actual HTTP status and unknown stage; do not call the work complete.
    review = (observed["http"]["status"] == 409 and state is not None and state["status"] == 200
              and state["body"].get("stage") in {"crm_started", "crm_review_required", "notification_started", "review_required"})
    return observed, 0 if completed or review else 1


def main():
    parser = argparse.ArgumentParser(description="Invoke the actual local ASGI intake application")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    result, status = asyncio.run(execute(args.db, args.request))
    print(json.dumps(result))
    return status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
