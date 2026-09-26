from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class AshContainerCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=2, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    capacity_grams: int = Field(gt=0, le=100_000_000)
    actor: str = Field(min_length=1, max_length=120)


class AshPackCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    hall_code: str = Field(min_length=1, max_length=64)
    container_code: str = Field(min_length=2, max_length=80)
    weight_grams: int = Field(gt=0, le=100_000_000)
    handler: str = Field(min_length=1, max_length=120)
    seal_code: str = Field(default="", max_length=120)
    location: str | None = Field(default=None, max_length=200)
    batch_code: str | None = Field(default=None, max_length=80)
    retention_hours: int | None = Field(default=None, ge=1, le=24 * 365)


class AshTransferInitiate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    handover_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    idempotency_key: str | None = Field(default=None, max_length=160)
    container_code: str = Field(min_length=2, max_length=80)
    from_party: str = Field(min_length=1, max_length=120)
    to_party: str = Field(min_length=1, max_length=120)
    to_location: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=120)
    retention_hours: int | None = Field(default=None, ge=1, le=24 * 365)

    @model_validator(mode="after")
    def distinct_parties(self) -> "AshTransferInitiate":
        if self.from_party == self.to_party:
            raise ValueError("交接双方不能是同一经手人")
        return self


class AshTransferConfirm(BaseModel):
    confirmed_by: str = Field(min_length=1, max_length=120)
    measured_weight_grams: int | None = Field(default=None, ge=0, le=100_000_000)


class AshTransferCancel(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


class AshMergeCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    container_code: str = Field(min_length=2, max_length=80)
    source_container_codes: list[str] = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=120)
    weight_grams: int | None = Field(default=None, gt=0, le=100_000_000)
    loss_reason: str = Field(default="", max_length=500)
    seal_code: str = Field(default="", max_length=120)
    location: str | None = Field(default=None, max_length=200)
    batch_code: str | None = Field(default=None, max_length=80)
    retention_hours: int | None = Field(default=None, ge=1, le=24 * 365)

    @model_validator(mode="after")
    def unique_sources(self) -> "AshMergeCreate":
        if len(self.source_container_codes) != len(set(self.source_container_codes)):
            raise ValueError("来源容器不能重复")
        return self


class AshReweigh(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    measured_weight_grams: int = Field(ge=0, le=100_000_000)
    reason: str = Field(min_length=2, max_length=500)


class AshDisposalCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    container_code: str = Field(min_length=2, max_length=80)
    disposal_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    method: Literal["buried", "ritual_burn", "recycled", "transferred_out"] = "ritual_burn"
    actor: str = Field(min_length=1, max_length=120)
    witness: str = Field(default="", max_length=120)
    measured_weight_grams: int | None = Field(default=None, ge=0, le=100_000_000)
    detail: dict = Field(default_factory=dict)


class AshAnomalyAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    resolution: str = Field(default="", max_length=500)
