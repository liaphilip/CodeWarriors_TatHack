from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class TaskInput(StrictInput):
    task: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class DecisionInput(StrictInput):
    conversation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    proposal_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    decision: Literal["approve", "reject"]


class AttackInput(StrictInput):
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class SearchInput(StrictInput):
    query: str = Field(min_length=1, max_length=200)


class ReadInput(StrictInput):
    document_id: str = Field(min_length=1, max_length=80)


class ProposeInput(ReadInput):
    recipient: str = Field(min_length=1, max_length=254)
    cc: list[str] = Field(default_factory=list, max_length=20)
    bcc: list[str] = Field(default_factory=list, max_length=20)
    subject: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=10000)

    @field_validator('recipient', 'cc', 'bcc')
    @classmethod
    def recipients(cls, value):
        values = value if isinstance(value, list) else [value]
        if any(not item.strip() or len(item) > 254 or any(ord(c) < 32 or ord(c) == 127 for c in item) for item in values):
            raise ValueError('Invalid recipient')
        return [item.strip() for item in value] if isinstance(value, list) else value
