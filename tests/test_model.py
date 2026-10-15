"""Actual Gemini GenerateContent HTTP serialization/completion/usage; no paid calls."""
from hashlib import sha256
import json
from pathlib import Path

from google import genai
import httpx
import pytest

from lead_intake import model
from lead_intake.model import DecisionRunError, decide

LEAD = {"event_id": "form-1001", "name": "Ari Patel", "email": "ari@cedar.example", "company": "Cedar Freight",
        "message": "We need exception routing before the holiday peak."}
DECISION = {"route": "sales", "summary": "Cedar needs exception routing before the holiday peak.", "reason": "Need and timeline are explicit"}
USAGE = {"promptTokenCount": 18, "candidatesTokenCount": 10, "thoughtsTokenCount": 5, "totalTokenCount": 33}


def response(*, decision=DECISION, text=None, finish="STOP", usage=USAGE, block=None, parts=None):
    body = {"responseId": "synthetic-response", "modelVersion": "gemini-2.5-flash-synthetic-version",
        "candidates": [{"content": {"role": "model", "parts": parts or [{"text": json.dumps(decision) if text is None else text}]}, "finishReason": finish}]}
    if usage is not None:
        body["usageMetadata"] = usage
    if block is not None:
        body["promptFeedback"] = {"blockReason": block}
    return body


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sdk():
    opened = []
    def make(body=None, status=200):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(status, json=response() if body is None else body)
        http = httpx.Client(transport=httpx.MockTransport(handle))
        client = genai.Client(api_key="synthetic-not-a-secret", vertexai=False,
            http_options={"httpx_client": http, "timeout": 30000, "retry_options": {"attempts": 1}})
        opened.append((client,http))
        return client,requests,http
    yield make
    for client,http in opened:
        client.close(); http.close()


def test_real_sdk_sends_one_validated_lead_and_schema_with_explicit_limits(sdk):
    client,requests,http = sdk()
    result = decide(LEAD,client=client)
    assert len(requests) == 1
    assert str(requests[0].url) == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    body = json.loads(requests[0].content)
    assert body["generationConfig"]["maxOutputTokens"] == 2048
    assert body["generationConfig"]["candidateCount"] == 1
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"]["properties"]["route"]["enum"] == ["sales","partnerships","review"]
    assert json.dumps(LEAD,sort_keys=True) in body["contents"][0]["parts"][0]["text"]
    assert result.decision.model_dump() == DECISION
    assert result.observation["input_sha256"] == sha256(json.dumps(LEAD,sort_keys=True).encode()).hexdigest()
    assert result.observation["finish_reasons"] == ["STOP"]
    assert result.observation["reported_usage"]["thoughts_token_count"] == 5
    assert result.usage["total_token_count"] == 33  # Includes thought tokens.
    assert result.observation["cost_usd"] is None and not http.is_closed


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY", "OTHER", "FINISH_REASON_UNSPECIFIED"])
def test_valid_json_from_nonfinal_or_blocked_finish_is_not_a_decision(sdk,finish):
    client,requests,_ = sdk(response(finish=finish))
    with pytest.raises(DecisionRunError) as caught:
        decide(LEAD,client=client)
    assert len(requests) == 1
    assert json.loads(caught.value.observation["raw_output"]) == DECISION
    assert caught.value.observation["finish_reasons"] == [finish]
    assert caught.value.observation["usage_complete"]


@pytest.mark.parametrize("usage,complete", [(None,False), ({},False), ({"promptTokenCount":18},False),
    ({"promptTokenCount":0,"candidatesTokenCount":0,"totalTokenCount":0},True)])
def test_missing_partial_and_zero_usage_remain_distinct(sdk,usage,complete):
    client,_,_ = sdk(response(usage=usage))
    result = decide(LEAD,client=client)
    assert result.observation["usage_complete"] is complete
    assert (result.usage is not None) is complete
    if usage is None:
        assert result.observation["reported_usage"] is None
    elif usage == {}:
        assert result.observation["reported_usage"] == {}
    assert result.observation["cost_usd"] is None


@pytest.mark.parametrize("body", [
    response(text=""), response(text="not json"), response(decision={**DECISION,"summary":" "*20}),
    response(decision={**DECISION,"route":"another-team"}), response(decision={**DECISION,"score":1}),
    response(block="PROHIBITED_CONTENT"), {"candidates":[]},
    response(parts=[{"text":json.dumps(DECISION)},{"functionCall":{"name":"unexpected","args":{}}}]),
])
def test_invalid_or_unexpected_output_keeps_evidence_without_accepted_decision(sdk,body):
    client,_,_ = sdk(body)
    with pytest.raises(DecisionRunError):
        decide(LEAD,client=client)


@pytest.mark.parametrize("fail", [False,True])
def test_owned_client_explicitly_uses_google_ai_and_closes_on_every_exit(sdk,monkeypatch,fail):
    client,_,_ = sdk(response(finish="MAX_TOKENS" if fail else "STOP"))
    closed=[]
    original=client.close
    def close(): closed.append(True); original()
    monkeypatch.setattr(client,"close",close)
    def make(**kwargs):
        assert kwargs == {"api_key":"synthetic-not-a-secret","vertexai":False,
            "http_options":{"timeout":30000,"retry_options":{"attempts":1}}}
        return client
    monkeypatch.setenv("GEMINI_API_KEY","synthetic-not-a-secret")
    monkeypatch.setattr(model.genai,"Client",make)
    if fail:
        with pytest.raises(DecisionRunError): decide(LEAD)
    else: decide(LEAD)
    assert closed == [True]


def test_http_error_has_one_attempt_and_unknown_usage(sdk):
    client,requests,_ = sdk({"error":{"code":503,"message":"synthetic outage","status":"UNAVAILABLE"}},status=503)
    with pytest.raises(DecisionRunError) as caught:
        decide(LEAD,client=client)
    assert len(requests) == 1
    assert caught.value.observation["http_status"] == 503
    assert caught.value.observation["reported_usage"] is None
    assert caught.value.observation["usage_complete"] is False
