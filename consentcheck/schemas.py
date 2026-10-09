"""Validated proposals and the evidence exposed by the local API."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ApprovalStatus = Literal["PENDING", "APPROVED", "EXECUTED", "REJECTED", "EXPIRED"]


class ActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor_id: str = Field(min_length=1, max_length=100)
    task_id: str = Field(min_length=1, max_length=100)
    tool: Literal["send_document"]
    recipient: str = Field(min_length=3, max_length=254)
    document_name: str = Field(min_length=1, max_length=255)
    document_content: str = Field(min_length=1, max_length=100_000)

    @field_validator("recipient")
    @classmethod
    def validate_recipient(cls, value: str) -> str:
        # Deliberately modest mailbox validation; no DNS or email dependency.
        if not re.fullmatch(r"[^\s@<>]+@[^\s@<>.]+(?:\.[^\s@<>.]+)+", value):
            raise ValueError("Enter an email address such as mentor@example.com.")
        return value

    @field_validator("document_name")
    @classmethod
    def validate_document_name(cls, value: str) -> str:
        if any(c in value for c in '/\\\r\n\x00') or value in {".", ".."}:
            raise ValueError("Use a document name, not a file path.")
        return value


class ApprovalResponse(BaseModel):
    approval_id: str
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime


class ApprovalDetail(ActionProposal, ApprovalResponse):
    pass


class ExecutionEvidence(BaseModel):
    id: int
    approval_id: str
    recipient: str
    document_name: str
    document_content: str
    executed_at: datetime
    status: Literal["SENT"]


class ExecutionRequest(BaseModel):
    """Optional assertions, never the source of execution values."""
    model_config = ConfigDict(extra="forbid")
    # Preserve exact strings, including whitespace, for the frozen-value check.
    action: dict[str, str]

    @field_validator("action")
    @classmethod
    def validate_action(cls, value):
        ActionProposal.model_validate(value)
        return value


AttackType = Literal[
    "recipient_substitution", "content_substitution", "replay", "cross_task_reuse"
]


class AttackResult(BaseModel):
    attack_type: AttackType
    approval_id: str
    original_approved_action: ActionProposal
    mutated_action: ActionProposal
    expected_blocked: bool = True
    http_status: int
    response_detail: str
    blocked: bool
    side_effect_created: bool
    unauthorized_effect: bool
    outbox_before: list[ExecutionEvidence]
    outbox_after: list[ExecutionEvidence]
    legitimate_setup_executed: bool
    status: Literal["PASS", "FAIL"]


DEMO_PROPOSAL = ActionProposal(
    actor_id="demo_user",
    task_id="project_submission",
    tool="send_document",
    recipient="mentor@example.com",
    document_name="project_report.pdf",
    document_content="This is a simulated project report for the ConsentCheck MVP.",
)
