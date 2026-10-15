"""Actual Pipedream OAuth/action HTTP and identity-bound receipt parsing."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from lead_intake import connectors
from lead_intake.contracts import ActionFailure
from lead_intake.connectors import PipedreamActions
from lead_intake.service import create_app
from test_service import LEAD,DECISION,submit

CONFIG = {"PIPEDREAM_PROJECT_ID":"proj_synthetic", "PIPEDREAM_CLIENT_ID":"synthetic-client", "PIPEDREAM_CLIENT_SECRET":"synthetic-client-secret",
    "PIPEDREAM_EXTERNAL_USER_ID":"synthetic-user", "PIPEDREAM_ATTIO_AUTH_PROVISION_ID":"apn_test_attio",
    "PIPEDREAM_SLACK_AUTH_PROVISION_ID":"apn_test_slack", "PIPEDREAM_SLACK_SALES_CHANNEL":"C0123456789",
    "PIPEDREAM_SLACK_PARTNERSHIPS_CHANNEL":"C0987654321", "PIPEDREAM_SLACK_REVIEW_CHANNEL":"G0123456789"}


class Platform(httpx.MockTransport):
    def __init__(self,fault=None):
        self.requests=[];self.closed=False;self.fault=fault
        super().__init__(self.handle)
    def handle(self,request):
        self.requests.append(request)
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200,json={"access_token":"synthetic-access-token","expires_in":3600,"token_type":"Bearer"})
        assert request.url.path == "/v1/connect/proj_synthetic/actions/run"
        body=json.loads(request.content)
        if self.fault == "http": return httpx.Response(503,json={"error":"synthetic outage"})
        if self.fault == "action_error": return httpx.Response(200,json={"exports":{},"os":[{"k":"error","ts":1,"err":{"name":"Error","message":"synthetic action failure"}}],"ret":None})
        if body["id"] == "attio-create-update-record":
            returned={"data":{"id":{"record_id":"record-synthetic","workspace_id":"workspace-synthetic","object_id":"object-people"},
                "values":{"email_addresses":[{"email_address":"other@cedar.example" if self.fault == "wrong_contact" else LEAD["email"]}]}}}
        else:
            returned={"ok":False if self.fault == "slack_rejected" else True,
                "channel":"C1111111111" if self.fault == "wrong_channel" else body["configured_props"]["channel"],"ts":"1792100000.000001"}
        if self.fault == "empty":returned=None
        return httpx.Response(200,json={"exports":{"$summary":"synthetic action acknowledgement"},"os":[],"ret":returned})
    def close(self): self.closed=True;super().close()


@pytest.fixture(autouse=True)
def config(monkeypatch):
    for key,value in CONFIG.items():monkeypatch.setenv(key,value)
    monkeypatch.setenv("ALLOW_PIPEDREAM_ACTIONS","1")
    monkeypatch.setenv("CONNECTOR_MODE","pipedream")
    monkeypatch.setenv("PIPEDREAM_ACCESS_TOKEN","ambient-token-must-not-win")
    monkeypatch.delenv("PIPEDREAM_BASE_URL",raising=False)


@pytest.fixture
def adapter(monkeypatch):
    opened=[]
    def make(fault=None):
        platform=Platform(fault)
        actions=PipedreamActions(transport=platform)
        opened.append(actions)
        return actions,platform
    yield make
    for actions in opened:actions.close()


def test_real_sdk_oauth_and_both_actions_use_frozen_development_scope_and_receipts(adapter,monkeypatch):
    actions,platform=adapter()
    crm=actions.upsert_person(LEAD,DECISION.summary)
    monkeypatch.setenv("PIPEDREAM_SLACK_SALES_CHANNEL","C1111111111")
    notification=actions.notify(LEAD["event_id"],"sales","Synthetic lead notice")
    assert crm.receipt_id == "record-synthetic" and crm.request_key == LEAD["email"]
    assert notification.receipt_id == "1792100000.000001" and notification.target == CONFIG["PIPEDREAM_SLACK_SALES_CHANNEL"]
    assert [request.url.path for request in platform.requests] == ["/v1/oauth/token","/v1/connect/proj_synthetic/actions/run","/v1/connect/proj_synthetic/actions/run"]
    token=json.loads(platform.requests[0].content)
    assert token["client_id"] == CONFIG["PIPEDREAM_CLIENT_ID"] and token["client_secret"] == CONFIG["PIPEDREAM_CLIENT_SECRET"]
    for request in platform.requests[1:]:
        assert request.headers["x-pd-environment"] == "development"
        assert request.headers["authorization"] == "Bearer synthetic-access-token"
    upsert=json.loads(platform.requests[1].content)
    assert upsert["id"] == "attio-create-update-record" and upsert["version"] == "0.1.1"
    assert upsert["external_user_id"] == "synthetic-user"
    assert upsert["configured_props"]["matchingAttribute"] == "email_addresses"
    assert upsert["configured_props"]["attio"] == {"authProvisionId":"apn_test_attio"}
    post=json.loads(platform.requests[2].content)
    assert post["id"] == "slack_v2-post-message" and post["version"] == "0.1.6"
    assert post["configured_props"]["slack"] == {"authProvisionId":"apn_test_slack"}
    assert "slack_v2" not in post["configured_props"]
    assert post["configured_props"]["mrkdwn"] is False
    serialized=json.dumps(actions.scope())+crm.model_dump_json()+notification.model_dump_json()
    assert "synthetic-client-secret" not in serialized and "synthetic-access-token" not in serialized


@pytest.mark.parametrize("fault",["action_error","empty","wrong_contact","http"])
def test_unacknowledged_crm_result_never_advances_or_retries_blindly(adapter,tmp_path,fault):
    actions,platform=adapter(fault)
    with TestClient(create_app(tmp_path/"state.sqlite",lambda lead:DECISION,actions,token="secret")) as client:
        assert submit(client).status_code == 502
        assert submit(client).status_code == 409
        evidence=client.get("/events/form-1001",headers={"x-intake-token":"secret"}).json()
    assert len(platform.requests) == 2  # OAuth + one action, including on HTTP 503.
    assert evidence["stage"] == "crm_review_required" and evidence["crm_receipt"] is None
    assert evidence["action_attempts"][0]["status"] == "uncertain"
    if fault == "action_error":
        assert evidence["action_attempts"][0]["observation"]["response"]["os"][0]["k"] == "error"


@pytest.mark.parametrize("fault",["wrong_channel","slack_rejected"])
def test_bad_notification_ack_preserves_crm_and_stops_duplicate_posts(adapter,tmp_path,fault):
    actions,platform=adapter(fault)
    with TestClient(create_app(tmp_path/"state.sqlite",lambda lead:DECISION,actions,token="secret")) as client:
        assert submit(client).status_code == 502
        assert submit(client).status_code == 409
        evidence=client.get("/events/form-1001",headers={"x-intake-token":"secret"}).json()
    assert len(platform.requests) == 3
    assert evidence["stage"] == "review_required"
    assert evidence["crm_receipt"]["receipt_id"] == "record-synthetic" and evidence["notification_receipt"] is None


def test_owned_http_transport_closes_with_asgi_lifespan_and_injected_one_remains_caller_owned(adapter,tmp_path,monkeypatch):
    actions,platform=adapter()
    import lead_intake.service as service
    monkeypatch.setattr(service,"PipedreamActions",lambda:actions)
    with TestClient(create_app(tmp_path/"owned.sqlite",lambda lead:DECISION,token="secret")) as client:
        assert submit(client).status_code == 200
    assert platform.closed and actions.http.is_closed
    supplied,other=adapter()
    with TestClient(create_app(tmp_path/"supplied.sqlite",lambda lead:DECISION,supplied,token="secret")) as client:
        assert submit(client).status_code == 200
    assert not other.closed


def test_connector_opt_in_and_explicit_channel_ids_are_required(monkeypatch):
    monkeypatch.delenv("ALLOW_PIPEDREAM_ACTIONS",raising=False)
    with pytest.raises(RuntimeError,match="ALLOW_PIPEDREAM_ACTIONS"):PipedreamActions()
    monkeypatch.setenv("ALLOW_PIPEDREAM_ACTIONS","1")
    monkeypatch.setenv("PIPEDREAM_SLACK_SALES_CHANNEL","#general")
    with pytest.raises(ValueError,match="channel IDs"):PipedreamActions()


def test_entire_webhook_runs_actual_gemini_and_pipedream_sdks_and_replay_reuses_evidence(tmp_path,monkeypatch):
    from google import genai
    from lead_intake import model,service
    from test_model import response
    actual=genai.Client
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,json=response())
    with httpx.Client(transport=httpx.MockTransport(handle)) as provider_http:
        def client(**kwargs):
            options={**kwargs.pop("http_options"),"httpx_client":provider_http}
            return actual(http_options=options,**kwargs)
        monkeypatch.setenv("GEMINI_API_KEY","synthetic-not-a-secret")
        monkeypatch.setenv("GEMINI_MODEL","gemini-2.5-flash")
        monkeypatch.setattr(model.genai,"Client",client)
        platform=Platform()
        actions=PipedreamActions(transport=platform)
        monkeypatch.setattr(service,"PipedreamActions",lambda:actions)
        with TestClient(create_app(tmp_path/"full.sqlite",token="secret")) as api:
            first=submit(api);replay=submit(api)
            assert first.status_code == replay.status_code == 200
            body=first.json()
            assert body["model_evidence"]["observation"]["source"] == "gemini-sdk"
            assert body["all_attempt_usage"] == {"prompt_token_count":18,"candidates_token_count":10,"total_token_count":33}
            assert replay.json()["model_evidence"] == body["model_evidence"]
            assert replay.json()["replayed"] and not replay.json()["classifier_called_this_request"]
            assert len(body["model_attempts"]) == 1 and len(body["action_attempts"]) == 2
            assert body["crm_receipt"]["provider"] == body["notification_receipt"]["provider"] == "pipedream"
        assert len(requests) == 1 and len(platform.requests) == 3 and platform.closed
