"""Webhook, decision and action-acknowledgement contracts."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeadEvent(Contract):
    event_id: str = Field(pattern=r"^[A-Za-z0-9_-]{4,80}$")
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    company: str = Field(min_length=2, max_length=120)
    message: str = Field(min_length=10, max_length=2000)

    @field_validator("name", "company", "message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Field must not be blank")
        return value


class Decision(Contract):
    route: Literal["sales", "partnerships", "review"]
    summary: str = Field(min_length=10, max_length=400)
    reason: str = Field(min_length=5, max_length=300)

    @field_validator("summary", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Decision text must not be blank")
        return value


class DecisionResult(Contract):
    decision: Decision
    observation: dict[str, Any]
    usage: dict[str, Any] | None = None


class ActionReceipt(Contract):
    operation: Literal["upsert_person", "notify"]
    request_key: str
    target: str
    receipt_id: str = Field(min_length=1)
    provider: str
    observation: dict[str, Any] = Field(default_factory=dict)


class ActionFailure(RuntimeError):
    def __init__(self, message, *, uncertain=True, observation=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.observation = observation or {}
