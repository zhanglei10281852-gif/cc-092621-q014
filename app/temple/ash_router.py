from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.ash_schemas import (
    AshAnomalyAction,
    AshContainerCreate,
    AshDisposalCreate,
    AshMergeCreate,
    AshPackCreate,
    AshReweigh,
    AshTransferCancel,
    AshTransferConfirm,
    AshTransferInitiate,
)
from app.temple.ash_service import AshCustodyService

router = APIRouter(prefix="/api/temple/ash", tags=["香灰批次与容器交接链"])


def service() -> AshCustodyService:
    return AshCustodyService()


@router.post("/containers", status_code=201)
def register_container(payload: AshContainerCreate):
    return service().register_container(payload.model_dump())


@router.get("/containers")
def list_containers(temple_code: str | None = None, status: str | None = None):
    return {"items": service().list_containers(temple_code, status)}


@router.get("/containers/{container_code}")
def container_detail(container_code: str):
    return service().container_detail(container_code)


@router.post("/packs", status_code=201)
def collect_and_pack(payload: AshPackCreate):
    return service().collect_and_pack(payload.model_dump())


@router.post("/transfers", status_code=201)
def initiate_transfer(payload: AshTransferInitiate):
    return service().initiate_transfer(payload.model_dump())


@router.post("/transfers/{handover_code}/confirm")
def confirm_transfer(handover_code: str, payload: AshTransferConfirm):
    return service().confirm_transfer(handover_code, payload.model_dump())


@router.post("/transfers/{handover_code}/cancel")
def cancel_transfer(handover_code: str, payload: AshTransferCancel):
    return service().cancel_transfer(handover_code, payload.model_dump())


@router.get("/transfers/{handover_code}")
def handover_detail(handover_code: str):
    return service().handover_detail(handover_code)


@router.post("/merges", status_code=201)
def merge_batches(payload: AshMergeCreate):
    return service().merge_batches(payload.model_dump())


@router.post("/containers/{container_code}/reweigh")
def reweigh_container(container_code: str, payload: AshReweigh):
    return service().reweigh_container(container_code, payload.model_dump())


@router.post("/disposals", status_code=201)
def dispose_container(payload: AshDisposalCreate):
    return service().dispose_container(payload.model_dump())


@router.get("/disposals/{disposal_code}/trace")
def disposal_trace(disposal_code: str):
    return service().disposal_trace(disposal_code)


@router.get("/batches/{batch_code}/trace")
def batch_trace(batch_code: str):
    return service().batch_trace(batch_code)


@router.post("/anomalies/scan")
def scan_anomalies(actor: str = Query(default="ash-custody-scan", min_length=1), stall_hours: int = Query(default=24, ge=1, le=24 * 365)):
    return service().scan_anomalies(actor, stall_hours)


@router.get("/anomalies")
def list_anomalies(temple_code: str | None = None, state: str | None = None, anomaly_type: str | None = None):
    return {"items": service().list_anomalies(temple_code, state, anomaly_type)}


@router.post("/anomalies/{anomaly_id}/acknowledge")
def acknowledge_anomaly(anomaly_id: int, payload: AshAnomalyAction):
    return service().acknowledge_anomaly(anomaly_id, payload.actor)


@router.post("/anomalies/{anomaly_id}/resolve")
def resolve_anomaly(anomaly_id: int, payload: AshAnomalyAction):
    return service().resolve_anomaly(anomaly_id, payload.actor, payload.resolution)
