from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

CUSTODIAN = Field(min_length=1, max_length=120)


class AshSealCreate(BaseModel):
    operation_key: str = Field(min_length=6, max_length=160)
    temple_code: str = Field(min_length=2, max_length=64)
    hall_code: str = Field(min_length=1, max_length=64)
    container_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]+$")
    batch_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]+$")
    weight_grams: float = Field(gt=0, le=10_000_000)
    sealed_by: str = CUSTODIAN
    storage_hours: float = Field(default=72.0, gt=0, le=24 * 365)
    note: str = Field(default="", max_length=500)


class AshHandoverCreate(BaseModel):
    handover_key: str = Field(min_length=6, max_length=160)
    container_code: str = Field(min_length=3, max_length=80)
    from_custodian: str = CUSTODIAN
    to_custodian: str = CUSTODIAN
    initiated_by: str = CUSTODIAN
    confirm_hours: float = Field(default=24.0, gt=0, le=24 * 30)

    @model_validator(mode="after")
    def distinct_custodians(self) -> "AshHandoverCreate":
        if self.from_custodian == self.to_custodian:
            raise ValueError("交接双方不能是同一人")
        return self


class AshHandoverConfirm(BaseModel):
    side: Literal["from", "to"]
    actor: str = CUSTODIAN


class AshHandoverCancel(BaseModel):
    actor: str = CUSTODIAN
    reason: str = Field(min_length=2, max_length=500)


class AshMergeCreate(BaseModel):
    operation_key: str = Field(min_length=6, max_length=160)
    temple_code: str = Field(min_length=2, max_length=64)
    source_container_codes: list[str] = Field(min_length=1, max_length=50)
    target_container_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]+$")
    loss_grams: float = Field(default=0, ge=0, le=10_000_000)
    actor: str = CUSTODIAN
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def distinct_sources(self) -> "AshMergeCreate":
        if len(self.source_container_codes) != len(set(self.source_container_codes)):
            raise ValueError("来源容器不能重复")
        if self.target_container_code in self.source_container_codes:
            raise ValueError("目标容器不能与来源容器相同")
        return self


class AshReweighCreate(BaseModel):
    operation_key: str = Field(min_length=6, max_length=160)
    measured_weight_grams: float = Field(ge=0, le=10_000_000)
    tolerance_grams: float = Field(default=50.0, ge=0, le=1_000_000)
    actor: str = CUSTODIAN
    note: str = Field(default="", max_length=500)


class AshLossAdjustCreate(BaseModel):
    operation_key: str = Field(min_length=6, max_length=160)
    loss_grams: float = Field(gt=0, le=10_000_000)
    actor: str = CUSTODIAN
    reason: str = Field(min_length=2, max_length=500)


class AshDisposalCreate(BaseModel):
    operation_key: str = Field(min_length=6, max_length=160)
    disposal_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]+$")
    method: str = Field(min_length=2, max_length=120)
    operator: str = CUSTODIAN
    note: str = Field(default="", max_length=500)


class AshAnomalyAction(BaseModel):
    actor: str = CUSTODIAN


class AshAnomalyResolve(BaseModel):
    actor: str = CUSTODIAN
    resolution: str = Field(min_length=2, max_length=500)
    new_storage_deadline: str | None = None
