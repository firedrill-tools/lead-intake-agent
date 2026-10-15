from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import sqlite3
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient
from lead_intake.contracts import ActionFailure, ActionReceipt
from lead_intake.model import Decision, decide
from lead_intake.service import create_app

LEAD = {"event_id":"form-1001","name":"Ari Patel","email":"ari@cedar.example", "company":"Cedar Freight",
        "message":"We need exception routing before the holiday peak."}
DECISION = Decision(route="sales",summary="Cedar needs exception routing before the holiday peak.",reason="Need and timeline are explicit")


@pytest.fixture(autouse=True)
def local_environment(monkeypatch):
    monkeypatch.setenv("CONNECTOR_MODE","synthetic")
    monkeypatch.delenv("ALLOW_PIPEDREAM_ACTIONS",raising=False)


@pytest.fixture
def app(tmp_path):
    with ExitStack() as stack:
        paths=[]
        def make(actions=None,classifier=None,path=None):
            calls=[]
            def classify(lead):
                calls.append(lead)
                return DECISION
            path = path or tmp_path / f"state-{len(paths)}.sqlite"
            paths.append(path)
            application=create_app(path,classifier or classify,actions,token="secret")
            client=stack.enter_context(TestClient(application))
            return client,path,calls
        yield make


def submit(client,lead=LEAD,token="secret"):
    return client.post("/intake",json=lead,headers={"x-intake-token":token})


def receipt(operation,key,target=None):
    return ActionReceipt(operation=operation,request_key=key,target=target or key,receipt_id=key,provider="injected")


def test_success_and_replay_are_single_effect_with_original_evidence(app):
    client,path,calls=app()
    first=submit(client)
    assert first.status_code == 200,first.text
    second=submit(client)
    assert second.status_code == 200 and second.json()["replayed"]
    assert len(calls) == 1
    assert first.json()["model_evidence"] == second.json()["model_evidence"]
    assert first.json()["classifier_called_this_request"] and not second.json()["classifier_called_this_request"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM contacts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
        assert conn.execute("SELECT stage FROM events").fetchone()[0] == "complete"
    assert first.json()["all_attempt_usage"] is None
    assert first.json()["crm_receipt"]["provider"] == "synthetic"


def test_auth_validation_and_conflicting_replay(app):
    client,_,calls=app()
    assert submit(client,token="wrong").status_code == 401
    assert submit(client,dict(LEAD,email="bad",company="")).status_code == 422
    assert submit(client,dict(LEAD,message=" "*20)).status_code == 422
    assert submit(client,dict(LEAD,extra="unknown")).status_code == 422
    assert calls == []
    assert submit(client).status_code == 200
    assert submit(client,dict(LEAD,email="other@cedar.example")).status_code == 409
    assert client.get("/events/form-1001").status_code == 401
    assert client.get("/events/missing",headers={"x-intake-token":"secret"}).status_code == 404


def test_definite_crm_rejection_can_retry_without_reclassifying(app):
    class Flaky:
        attempts=0;notifications=0
        def upsert_person(self,lead,summary):
            self.attempts+=1
            if self.attempts == 1: raise ActionFailure("known rejection before mutation",uncertain=False)
            return receipt("upsert_person",lead["email"])
        def notify(self,event_id,route,text):
            self.notifications+=1
            return receipt("notify",event_id,route)
    actions=Flaky();client,_,calls=app(actions)
    assert submit(client).status_code == 502
    assert submit(client).status_code == 200
    assert len(calls) == 1 and actions.attempts == 2 and actions.notifications == 1


@pytest.mark.parametrize("operation",["crm","notification"])
def test_uncertain_action_requires_review_and_cannot_be_blindly_repeated(app,operation):
    class Timeout:
        attempted=0
        def upsert_person(self,lead,summary):
            if operation == "crm":
                self.attempted+=1;raise TimeoutError("unobserved CRM outcome")
            return receipt("upsert_person",lead["email"])
        def notify(self,*args):
            self.attempted+=1;raise TimeoutError("unobserved notification outcome")
    actions=Timeout();client,path,_=app(actions)
    assert submit(client).status_code == 502
    assert submit(client).status_code == 409
    assert actions.attempted == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT stage FROM events").fetchone()[0] == ("crm_review_required" if operation == "crm" else "review_required")


def test_missing_or_wrong_action_acknowledgements_cannot_complete(app):
    class Bad:
        notifications=0
        def upsert_person(self,*args):return {"error":"synthetic action error"}
        def notify(self,*args):self.notifications+=1
    actions=Bad();client,_,_=app(actions)
    assert submit(client).status_code == 502
    assert submit(client).status_code == 409 and actions.notifications == 0


def test_pending_event_rejects_changed_connector_scope_after_restart(app,tmp_path):
    class Before:
        def scope(self):return {"mode":"injected","channel":"channel-a"}
        def upsert_person(self,*args):raise ActionFailure("definite rejection",uncertain=False)
    path=tmp_path/"restart.sqlite"
    with TestClient(create_app(path,lambda lead:DECISION,Before(),token="secret")) as client:
        assert submit(client).status_code == 502
    class After(Before):
        def scope(self):return {"mode":"injected","channel":"channel-b"}
    with TestClient(create_app(path,lambda lead:pytest.fail("must not call model"),After(),token="secret")) as client:
        assert submit(client).status_code == 409


def test_concurrent_replay_is_serialized_and_committed_progress_is_inspectable(tmp_path):
    first,second,release=Event(),Event(),Event()
    calls=[];arrivals=[];guard=Lock()
    def classify(lead):
        calls.append(lead);first.set()
        assert release.wait(5)
        return DECISION
    application=create_app(tmp_path/"concurrent.sqlite",classify,token="secret")
    @application.middleware("http")
    async def arrivals_probe(request,call_next):
        if request.url.path == "/intake":
            with guard:
                arrivals.append(True)
                if len(arrivals)==2:second.set()
        return await call_next(request)
    with TestClient(application) as client, ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(submit,client)
        try:
            assert first.wait(5)
            progress=client.get("/events/form-1001",headers={"x-intake-token":"secret"}).json()
            assert progress["stage"] == "received" and progress["model_attempts"][0]["status"] == "started"
            two=pool.submit(submit,client)
            assert second.wait(5)
        finally: release.set()
        assert one.result().status_code == 200 and two.result().status_code == 200
        assert len(calls) == 1
    assert application.state.closed
    with pytest.raises(sqlite3.ProgrammingError):application.state.conn.execute("SELECT 1")


def test_live_mode_and_model_require_explicit_configuration(tmp_path,monkeypatch):
    monkeypatch.setenv("CONNECTOR_MODE","pipedream")
    with pytest.raises(RuntimeError,match="ALLOW_PIPEDREAM_ACTIONS"):
        create_app(tmp_path/"other.sqlite",token="secret")
    monkeypatch.delenv("GEMINI_API_KEY",raising=False)
    with pytest.raises(RuntimeError,match="GEMINI_API_KEY"):decide(LEAD)


def test_failed_native_model_attempt_keeps_usage_and_explicit_retry_preserves_both(tmp_path):
    from google import genai
    import httpx
    from test_model import response
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,json=response(finish="MAX_TOKENS" if len(requests) == 1 else "STOP"))
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        sdk=genai.Client(api_key="synthetic-not-a-secret",vertexai=False,http_options={"httpx_client":http,"retry_options":{"attempts":1}})
        try:
            with TestClient(create_app(tmp_path/"retry.sqlite",lambda lead:decide(lead,client=sdk),token="secret")) as api:
                assert submit(api).status_code == 502
                before=api.get("/events/form-1001",headers={"x-intake-token":"secret"}).json()
                assert before["decision"] is None and before["action_attempts"] == []
                assert before["model_attempts"][0]["status"] == "failed"
                result=submit(api).json()
                assert result["stage"] == "complete"
                assert [item["status"] for item in result["model_attempts"]] == ["failed","success"]
                assert result["all_attempt_usage"] == {"prompt_token_count":36,"candidates_token_count":20,"total_token_count":66}
                assert submit(api).json()["model_attempts"] == result["model_attempts"]
            assert len(requests) == 2
        finally:sdk.close()


def test_legacy_unknown_evidence_is_not_invented_and_pending_scope_requires_review(tmp_path):
    path=tmp_path/"legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE events(event_id TEXT PRIMARY KEY,payload TEXT,decision TEXT,stage TEXT,error TEXT)")
        for event_id,stage in (("form-1001","complete"),("form-1002","received")):
            conn.execute("INSERT INTO events VALUES (?,?,?,?,NULL)",(event_id,json.dumps(dict(LEAD,event_id=event_id),sort_keys=True),DECISION.model_dump_json(),stage))
    with TestClient(create_app(path,lambda lead:pytest.fail("unknown legacy state must not run model"),token="secret")) as api:
        result=submit(api).json()
        assert result["replayed"] and result["model_evidence"] is None
        assert result["all_attempt_usage"] is None and result["model_attempts"] == []
        assert submit(api,dict(LEAD,event_id="form-1002")).status_code == 409
