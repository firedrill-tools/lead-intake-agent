"""Digest-bound fictional form inputs and SQLite reset state; no model calls."""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import EmailStr, Field, model_validator
from .contracts import Contract, Decision, DecisionResult, LeadEvent
from . import store

DATASET = "northstar-inbound-leads"
VERSION = "1.0.0"
PROFILES = {"starter", "busy", "ambiguous", "boundary"}


def canonical_request(request):
    lead = LeadEvent.model_validate(request).model_dump(mode="json")
    lead["email"] = lead["email"].lower()
    return json.dumps(lead, sort_keys=True, ensure_ascii=False)


class Contact(Contract):
    email: EmailStr
    name: str = Field(min_length=2)
    company: str = Field(min_length=2)
    summary: str = Field(min_length=10)


class History(Contract):
    request: LeadEvent
    decision: Decision
    stage: Literal["complete", "notification_started"]
    notice_recorded: bool

    @model_validator(mode="after")
    def acknowledged_notice(self):
        if self.stage == "complete" and not self.notice_recorded:
            raise ValueError("Complete history requires an existing notice")
        return self


class Seed(Contract):
    schema_version: Literal[1]
    synthetic: Literal[True]
    contacts: list[Contact]
    history: list[History]

    @model_validator(mode="after")
    def coherent(self):
        contacts = {contact.email: contact for contact in self.contacts}
        if len(contacts) != len(self.contacts) or len({item.request.event_id for item in self.history}) != len(self.history):
            raise ValueError("Duplicate contact/event identity")
        for contact in self.contacts:
            if contact.email != contact.email.lower() or not contact.email.endswith(".example"):
                raise ValueError("Dataset contacts must use canonical fictional addresses")
        for item in self.history:
            lead = item.request
            contact = contacts.get(lead.email)
            if not contact or (contact.name, contact.company, contact.summary) != (lead.name, lead.company, item.decision.summary):
                raise ValueError("History/contact relationship is inconsistent")
        return self


def fixture_root():
    bundled = Path(__file__).parent / "sample_data"
    return bundled if bundled.is_dir() else Path(__file__).resolve().parents[2] / "fixtures"


def read_bounded(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"Dataset file exceeds {limit} bytes")
    return data


def load_cases(root=None):
    root = Path(root or fixture_root()).resolve()
    manifest_path = (root / "manifest.json").resolve()
    if not manifest_path.is_relative_to(root):
        raise ValueError("Dataset manifest escapes fixture root")
    manifest = json.loads(read_bounded(manifest_path, 128 * 1024))
    if manifest.get("dataset") != DATASET or manifest.get("version") != VERSION:
        raise ValueError("Unqualified dataset identity/version")
    entries = manifest.get("cases")
    if not isinstance(entries, list) or not entries or len(entries) > 100:
        raise ValueError("Dataset must contain 1–100 cases")
    seen, loaded = set(), []
    for entry in entries:
        case_id, profile = entry["case_id"], entry["profile"]
        if not isinstance(case_id, str) or not case_id or case_id in seen or profile not in PROFILES:
            raise ValueError("Duplicate case or unknown profile")
        seen.add(case_id)
        blobs = {}
        for key, limit in (("request", 64 * 1024), ("state", 512 * 1024)):
            relative = Path(entry[key])
            path = (root / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(root):
                raise ValueError("Dataset path escapes fixture root")
            raw = read_bounded(path, limit)
            if sha256(raw).hexdigest() != entry[key + "_sha256"]:
                raise ValueError(f"Dataset {key} digest drift for {case_id}")
            blobs[key] = raw
        request = json.loads(blobs["request"])
        if not isinstance(request, dict):
            raise ValueError("Form request must be a JSON object")
        seed = Seed.model_validate_json(blobs["state"])
        if profile == "busy" and (len(seed.contacts) < 12 or len(seed.history) < 2):
            raise ValueError("Busy population requires contacts and prior work")
        loaded.append({**entry, "request_bytes": blobs["request"], "request_data": request, "seed": seed,
                       "request_path": str((root / entry["request"]).resolve())})
    if {entry["profile"] for entry in loaded} != PROFILES:
        raise ValueError("Dataset must cover all four profiles")
    return loaded


def list_cases(root=None):
    return {"dataset": DATASET, "version": VERSION, "case_count": len(cases := load_cases(root)),
            "cases": [{key: entry[key] for key in ("case_id", "profile", "description", "request", "request_sha256", "state", "state_sha256")} for entry in cases]}


def initialize_case(case_id, db_path, request_out=None, *, root=None):
    cases = load_cases(root)
    case = next((entry for entry in cases if entry["case_id"] == case_id), None)
    if case is None:
        raise ValueError(f"Unknown case: {case_id}")
    db_path = Path(db_path).absolute()
    request_out = Path(request_out).absolute() if request_out is not None else None
    if request_out == db_path:
        raise ValueError("Database and request output must be different files")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    created_db = created_request = False
    try:
        descriptor = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        created_db = True
        if request_out is not None:
            request_out.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(request_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created_request = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(case["request_bytes"])
        try:
            encoded = canonical_request(case["request_data"])
            request_hash = sha256(encoded.encode()).hexdigest()
        except ValueError:
            request_hash = None  # Intentionally invalid forms are still selectable.
        context = {"dataset": DATASET, "version": VERSION, "profile": case["profile"], "case_id": case_id,
                   "request_sha256": request_hash, "request_file_sha256": case["request_sha256"],
                   "seed_sha256": case["state_sha256"], "reset_coverage": "local_sqlite_only"}
        with closing(sqlite3.connect(db_path)) as conn:
            from .connectors import SyntheticActions
            store.initialize(conn)
            actions = SyntheticActions(conn)
            conn.executemany("INSERT INTO contacts VALUES (?,?,?,?)", [(c.email, c.name, c.company, c.summary) for c in case["seed"].contacts])
            conn.commit()
            scope = json.dumps(actions.scope(), sort_keys=True)
            for item in case["seed"].history:
                lead, decision = item.request.model_dump(mode="json"), item.decision
                crm = actions.upsert_person(lead, decision.summary)
                notice = f"[{decision.route}] {lead['name']} ({lead['email']}) at {lead['company']}: {decision.summary}"
                notification = actions.notify(lead["event_id"], decision.route, notice) if item.notice_recorded else None
                evidence = DecisionResult(decision=decision, observation={"source": "synthetic_fixture_history", "cost_usd": None, "reported_usage": None})
                item_context = context if canonical_request(lead) == (encoded if request_hash else None) else {"dataset": DATASET, "version": VERSION, "source": "synthetic_fixture_history"}
                conn.execute("INSERT INTO events(event_id,payload,decision,stage,model_evidence,dataset_context,connector_scope,scope_sha256,crm_receipt,notification_receipt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (lead["event_id"], canonical_request(lead), decision.model_dump_json(), item.stage, evidence.model_dump_json(), json.dumps(item_context), scope,
                     sha256(scope.encode()).hexdigest(), crm.model_dump_json(), notification.model_dump_json() if item.stage == "complete" else None))
            conn.execute("INSERT INTO dataset_metadata VALUES ('context',?)", (json.dumps(context),))
            conn.commit()
    except Exception:
        if created_request:
            request_out.unlink(missing_ok=True)
        if created_db:
            db_path.unlink(missing_ok=True)
        raise
    return {"dataset": context, "db": str(db_path), "request": str(request_out) if request_out else case["request_path"],
            "contacts": len(case["seed"].contacts), "prior_events": len(case["seed"].history), "model_called": False}
