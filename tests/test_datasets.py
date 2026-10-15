"""Reset integrity, realistic populations, packaging-independent selection and replay."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3

from fastapi.testclient import TestClient
import pytest
from lead_intake import cli
from lead_intake.datasets import fixture_root, initialize_case, list_cases, load_cases, Seed
from lead_intake.service import create_app
from test_service import DECISION, submit


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    monkeypatch.setenv("CONNECTOR_MODE", "synthetic")
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "INTAKE_TOKEN", "ALLOW_PIPEDREAM_ACTIONS"):
        monkeypatch.delenv(name, raising=False)


def test_listing_and_generator_reproduce_all_qualified_bytes_without_credentials(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("generator", Path(__file__).parents[1] / "scripts/build_fixtures.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.build(tmp_path)
    for path in fixture_root().rglob("*.json"):
        assert path.read_bytes() == (tmp_path / path.relative_to(fixture_root())).read_bytes()
    assert cli.main(["--list-cases"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["case_count"] == 10 and listed["version"] == "1.0.0"
    assert {case["profile"] for case in listed["cases"]} == {"starter", "busy", "ambiguous", "boundary"}


@pytest.mark.parametrize("case_id", [entry["case_id"] for entry in load_cases()])
def test_each_initialized_case_preserves_state_and_local_replay_boundaries(tmp_path, case_id):
    entry = next(entry for entry in load_cases() if entry["case_id"] == case_id)
    path, request_path = tmp_path / "state.sqlite", tmp_path / "request.json"
    result = initialize_case(case_id, path, request_path)
    assert request_path.read_bytes() == entry["request_bytes"]
    assert path.stat().st_mode & 0o777 == request_path.stat().st_mode & 0o777 == 0o600
    assert result["model_called"] is False and result["dataset"]["reset_coverage"] == "local_sqlite_only"
    calls = []
    def classify(lead):
        calls.append(lead)
        return DECISION
    with TestClient(create_app(path, classify, token="secret")) as api:
        response = submit(api, entry["request_data"])
        if case_id == "incomplete":
            assert response.status_code == 422 and calls == []
        elif case_id == "uncertain-notice":
            assert response.status_code == 409 and calls == []
        else:
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["dataset_context"] == result["dataset"]
            if case_id == "prior-complete":
                assert body["replayed"] and calls == []
                assert body["model_evidence"]["observation"]["source"] == "synthetic_fixture_history"
                assert body["model_attempts"] == [] and body["all_attempt_usage"] is None
            else:
                assert len(calls) == 1 and not body["replayed"]
                assert body["model_evidence"]["observation"]["source"] == "injected_classifier"
                assert submit(api, entry["request_data"]).json()["replayed"] and len(calls) == 1
    with sqlite3.connect(path) as conn:
        after = {row[0]: row[1:] for row in conn.execute("SELECT email,name,company,summary FROM contacts")}
        for contact in entry["seed"].contacts:
            if contact.email != entry["request_data"].get("email") or case_id in {"prior-complete", "uncertain-notice"}:
                assert after[contact.email] == (contact.name, contact.company, contact.summary)
        expected_new = int(case_id not in {"incomplete", "prior-complete", "uncertain-notice"})
        assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == len(entry["seed"].history) + expected_new


@pytest.mark.parametrize("fault", ["drift", "escape", "duplicate", "incoherent"])
def test_unqualified_dataset_refuses_initialization_without_creating_outputs(tmp_path, fault):
    root = tmp_path / "fixtures"
    shutil.copytree(fixture_root(), root)
    manifest = json.loads((root / "manifest.json").read_text())
    case = manifest["cases"][0]
    if fault == "drift":
        (root / case["request"]).write_text("{}")
    elif fault == "escape":
        outside = tmp_path / "outside.json"
        outside.write_bytes((root / case["request"]).read_bytes())
        (root / case["request"]).unlink()
        (root / case["request"]).symlink_to(outside)
    elif fault == "duplicate":
        manifest["cases"][1]["case_id"] = case["case_id"]
    else:
        busy = manifest["cases"][2]
        state = json.loads((root / busy["state"]).read_text())
        state["history"][0]["request"]["company"] = "Different Company"
        raw = json.dumps(state).encode()
        (root / busy["state"]).write_bytes(raw)
        busy["state_sha256"] = sha256(raw).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        initialize_case("ordinary", tmp_path / "db.sqlite", root=root)
    assert not (tmp_path / "db.sqlite").exists()


def test_exclusive_initialization_preserves_existing_files_and_rolls_back_new_outputs(tmp_path):
    path, request = tmp_path / "state.sqlite", tmp_path / "request.json"
    request.write_text("existing caller file")
    with pytest.raises(FileExistsError):
        initialize_case("ordinary", path, request)
    assert request.read_text() == "existing caller file" and not path.exists()
    initialize_case("busy-existing", path)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        initialize_case("ordinary", path)
    assert path.read_bytes() == original


def test_local_reset_is_not_accepted_as_remote_account_reset(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    initialize_case("busy-existing", path)
    monkeypatch.setenv("CONNECTOR_MODE", "pipedream")
    monkeypatch.setenv("ALLOW_PIPEDREAM_ACTIONS", "1")
    with pytest.raises(RuntimeError, match="resets local SQLite only"):
        with TestClient(create_app(path, token="secret")):
            pass


def test_changed_form_does_not_inherit_selected_case_identity(tmp_path):
    path = tmp_path / "state.sqlite"
    initialize_case("ordinary", path)
    request = next(entry for entry in load_cases() if entry["case_id"] == "ordinary")["request_data"]
    with TestClient(create_app(path, lambda lead: DECISION, token="secret")) as api:
        response = submit(api, {**request, "message": "This is a different inquiry with different intent."})
        assert response.status_code == 200
        assert response.json()["dataset_context"] == {"initial_dataset": "northstar-inbound-leads", "case_match": False}
