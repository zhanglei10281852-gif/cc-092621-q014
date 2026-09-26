from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.ash import AshCustodyService
from app.temple.ash_schemas import (
    AshAnomalyAction,
    AshAnomalyResolve,
    AshDisposalCreate,
    AshHandoverCancel,
    AshHandoverConfirm,
    AshHandoverCreate,
    AshLossAdjustCreate,
    AshMergeCreate,
    AshReweighCreate,
    AshSealCreate,
)

router = APIRouter(prefix="/api/temple/ash", tags=["香灰批次与容器交接链"])


def service() -> AshCustodyService:
    return AshCustodyService()


@router.post("/seals", status_code=201)
def seal_batch(payload: AshSealCreate):
    return service().seal_batch(payload.model_dump())


@router.get("/containers")
def list_containers(temple_code: str | None = None, status: str | None = None):
    return {"items": service().list_containers(temple_code, status)}


@router.get("/containers/{container_code}")
def container_detail(container_code: str):
    return service().container_detail(container_code)


@router.get("/containers/{container_code}/balance")
def container_balance(container_code: str):
    return service().container_balance(container_code)


@router.post("/handovers", status_code=201)
def initiate_handover(payload: AshHandoverCreate):
    return service().initiate_handover(payload.model_dump())


@router.get("/handovers/{handover_id}")
def handover_detail(handover_id: int):
    return service().handover_detail(handover_id)


@router.post("/handovers/{handover_id}/confirm")
def confirm_handover(handover_id: int, payload: AshHandoverConfirm):
    return service().confirm_handover(handover_id, payload.side, payload.actor)


@router.post("/handovers/{handover_id}/cancel")
def cancel_handover(handover_id: int, payload: AshHandoverCancel):
    return service().cancel_handover(handover_id, payload.actor, payload.reason)


@router.post("/merges", status_code=201)
def merge_containers(payload: AshMergeCreate):
    return service().merge_containers(payload.model_dump())


@router.post("/containers/{container_code}/reweigh")
def reweigh_container(container_code: str, payload: AshReweighCreate):
    return service().reweigh_container(container_code, payload.model_dump())


@router.post("/containers/{container_code}/loss-adjustments")
def adjust_loss(container_code: str, payload: AshLossAdjustCreate):
    return service().adjust_loss(container_code, payload.model_dump())


@router.post("/containers/{container_code}/disposals", status_code=201)
def dispose_container(container_code: str, payload: AshDisposalCreate):
    return service().dispose_container(container_code, payload.model_dump())


@router.get("/disposals/{disposal_code}/trace")
def disposal_trace(disposal_code: str):
    return service().disposal_trace(disposal_code)


@router.get("/anomalies")
def list_anomalies(status: str | None = None, anomaly_type: str | None = None, temple_code: str | None = None):
    return {"items": service().list_anomalies(status, anomaly_type, temple_code)}


@router.post("/anomalies/scan")
def scan_anomalies(actor: str = Query(default="ash-anomaly-scanner", min_length=1, max_length=120)):
    return service().scan_anomalies(actor)


@router.post("/anomalies/{anomaly_id}/acknowledge")
def acknowledge_anomaly(anomaly_id: int, payload: AshAnomalyAction):
    return service().acknowledge_anomaly(anomaly_id, payload.actor)


@router.post("/anomalies/{anomaly_id}/resolve")
def resolve_anomaly(anomaly_id: int, payload: AshAnomalyResolve):
    return service().resolve_anomaly(anomaly_id, payload.actor, payload.resolution, payload.new_storage_deadline)


@router.get("/production")
def production_summary(temple_code: str, day: str | None = None):
    return service().production_summary(temple_code, day)
