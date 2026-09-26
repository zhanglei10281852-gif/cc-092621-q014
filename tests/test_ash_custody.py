from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import get_connection
from app.temple.ash import AshCustodyService


def prepare(client):
    temple = client.post(
        "/api/temple/temples",
        json={"code": "lingyun-temple", "name": "凌云古寺", "temple_type": "heritage", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 10, "ventilation_capacity": 3000},
    )
    assert temple.status_code == 201, temple.text
    for sequence, (code, name) in enumerate((("main-hall", "大雄宝殿"), ("side-hall", "配殿")), start=1):
        hall = client.post(
            "/api/temple/temples/lingyun-temple/halls",
            json={"code": code, "name": name, "visit_order": sequence, "expected_visit_seconds": 600, "ventilation_capacity": 800},
        )
        assert hall.status_code == 201, hall.text


def seal_payload(sequence: int, **overrides):
    payload = {
        "operation_key": f"seal-op-{sequence:06d}",
        "temple_code": "lingyun-temple",
        "hall_code": "main-hall",
        "container_code": f"ash-bin-{sequence:06d}",
        "batch_code": f"ash-batch-{sequence:06d}",
        "weight_grams": 1000.0,
        "sealed_by": "keeper-a",
        "storage_hours": 72,
        "note": "日常收集封装",
    }
    payload.update(overrides)
    return payload


def seal(client, sequence: int, **overrides):
    response = client.post("/api/temple/ash/seals", json=seal_payload(sequence, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def handover_payload(key: str, container_code: str, **overrides):
    payload = {
        "handover_key": key,
        "container_code": container_code,
        "from_custodian": "keeper-a",
        "to_custodian": "keeper-b",
        "initiated_by": "keeper-a",
        "confirm_hours": 24,
    }
    payload.update(overrides)
    return payload


def confirm_handover(client, handover_id: int):
    first = client.post(f"/api/temple/ash/handovers/{handover_id}/confirm", json={"side": "from", "actor": "keeper-a"})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/temple/ash/handovers/{handover_id}/confirm", json={"side": "to", "actor": "keeper-b"})
    assert second.status_code == 200, second.text
    return second.json()


def test_seal_records_provenance_and_is_idempotent(client):
    prepare(client)
    created = seal(client, 1, weight_grams=1234.5)
    container = created["container"]
    batch = created["batch"]
    assert container["status"] == "sealed"
    assert container["current_custodian"] == "keeper-a"
    assert container["current_weight_grams"] == 1234.5
    assert batch["hall_code"] == "main-hall"
    assert batch["sealed_by"] == "keeper-a"
    assert batch["sealed_at"] is not None
    assert container["ledger"][0]["entry_type"] == "seal_in"
    assert container["ledger"][0]["delta_grams"] == 1234.5
    assert container["events"][0]["event_type"] == "sealed"

    replay = client.post("/api/temple/ash/seals", json=seal_payload(1, weight_grams=1234.5))
    assert replay.status_code == 201, replay.text
    assert replay.json()["replay"] is True
    assert replay.json()["container"]["id"] == container["id"]
    assert replay.json()["batch"]["id"] == batch["id"]

    conflict = client.post("/api/temple/ash/seals", json=seal_payload(1, container_code="ash-bin-900001", batch_code="ash-batch-900001", weight_grams=999))
    assert conflict.status_code == 409

    duplicate_code = client.post("/api/temple/ash/seals", json=seal_payload(2, container_code="ash-bin-000001"))
    assert duplicate_code.status_code == 409

    missing_hall = client.post("/api/temple/ash/seals", json=seal_payload(3, hall_code="missing-hall"))
    assert missing_hall.status_code == 404


def test_handover_requires_dual_confirm_and_keeps_chain_continuous(client):
    prepare(client)
    seal(client, 1)

    wrong_party = client.post("/api/temple/ash/handovers", json=handover_payload("handover-000001", "ash-bin-000001", from_custodian="keeper-x"))
    assert wrong_party.status_code == 409

    created = client.post("/api/temple/ash/handovers", json=handover_payload("handover-000001", "ash-bin-000001"))
    assert created.status_code == 201, created.text
    handover = created.json()
    assert handover["status"] == "pending"
    assert handover["container_status_before"] == "sealed"

    replay = client.post("/api/temple/ash/handovers", json=handover_payload("handover-000001", "ash-bin-000001"))
    assert replay.status_code == 201
    assert replay.json()["replay"] is True
    assert replay.json()["id"] == handover["id"]

    mismatched = client.post("/api/temple/ash/handovers", json=handover_payload("handover-000001", "ash-bin-000001", to_custodian="keeper-c"))
    assert mismatched.status_code == 409

    stranger = client.post(f"/api/temple/ash/handovers/{handover['id']}/confirm", json={"side": "from", "actor": "keeper-x"})
    assert stranger.status_code == 409

    from_confirmed = client.post(f"/api/temple/ash/handovers/{handover['id']}/confirm", json={"side": "from", "actor": "keeper-a"})
    assert from_confirmed.status_code == 200
    assert from_confirmed.json()["status"] == "pending"

    mid_state = client.get("/api/temple/ash/containers/ash-bin-000001").json()
    assert mid_state["status"] == "in_transit"
    assert mid_state["current_custodian"] == "keeper-a"

    done = client.post(f"/api/temple/ash/handovers/{handover['id']}/confirm", json={"side": "to", "actor": "keeper-b"})
    assert done.status_code == 200
    assert done.json()["status"] == "confirmed"
    assert done.json()["confirmed_at"] is not None

    confirm_replay = client.post(f"/api/temple/ash/handovers/{handover['id']}/confirm", json={"side": "to", "actor": "keeper-b"})
    assert confirm_replay.status_code == 200
    assert confirm_replay.json()["replay"] is True

    container = client.get("/api/temple/ash/containers/ash-bin-000001").json()
    assert container["status"] == "stored"
    assert container["current_custodian"] == "keeper-b"
    event_types = [event["event_type"] for event in container["events"]]
    assert event_types == ["sealed", "handover_initiated", "handover_from_confirmed", "handover_to_confirmed", "handover_confirmed"]

    next_handover = client.post("/api/temple/ash/handovers", json=handover_payload("handover-000002", "ash-bin-000001", from_custodian="keeper-b", to_custodian="keeper-c", initiated_by="keeper-b"))
    assert next_handover.status_code == 201
    cancelled = client.post(f"/api/temple/ash/handovers/{next_handover.json()['id']}/cancel", json={"actor": "keeper-b", "reason": "接收方临时无人"})
    assert cancelled.status_code == 200
    restored = client.get("/api/temple/ash/containers/ash-bin-000001").json()
    assert restored["status"] == "stored"
    assert restored["current_custodian"] == "keeper-b"


def test_concurrent_claim_of_same_container_succeeds_once(client):
    prepare(client)
    seal(client, 1)
    threads = 8
    barrier = threading.Barrier(threads)
    results: list[dict] = []
    errors: list[Exception] = []

    def claim(index: int):
        service = AshCustodyService()
        payload = handover_payload(f"handover-race-{index:04d}", "ash-bin-000001")
        try:
            barrier.wait(timeout=10)
            results.append(service.initiate_handover(payload))
        except ConflictError as exc:
            errors.append(exc)
        except Exception as exc:  # pragma: no cover - 便于诊断并发故障
            errors.append(exc)

    workers = [threading.Thread(target=claim, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert len(results) == 1
    assert len(errors) == threads - 1
    assert all(isinstance(error, ConflictError) for error in errors)
    handovers = client.get("/api/temple/ash/containers/ash-bin-000001").json()["handovers"]
    assert len(handovers) == 1
    assert handovers[0]["status"] == "pending"


def test_merge_forms_conservation_ledger_and_lineage(client):
    prepare(client)
    seal(client, 1, weight_grams=1000.0)
    seal(client, 2, weight_grams=800.0, hall_code="side-hall")
    merge_payload = {
        "operation_key": "merge-op-000001",
        "temple_code": "lingyun-temple",
        "source_container_codes": ["ash-bin-000001", "ash-bin-000002"],
        "target_container_code": "ash-bin-merged-1",
        "loss_grams": 10.0,
        "actor": "keeper-b",
        "note": "拆包合并入暂存大桶",
    }
    merged = client.post("/api/temple/ash/merges", json=merge_payload)
    assert merged.status_code == 201, merged.text
    target = merged.json()["container"]
    assert target["status"] == "stored"
    assert target["current_weight_grams"] == 1790.0
    assert target["current_custodian"] == "keeper-b"
    assert {content["batch_code"] for content in target["contents"] if content["status"] == "active"} == {"ash-batch-000001", "ash-batch-000002"}

    for code, weight in (("ash-bin-000001", 1000.0), ("ash-bin-000002", 800.0)):
        source = client.get(f"/api/temple/ash/containers/{code}").json()
        assert source["status"] == "merged"
        assert source["current_weight_grams"] == 0
        assert source["contents"][0]["status"] == "merged_out"
        assert source["contents"][0]["merged_into_container_id"] == target["id"]
        balance = client.get(f"/api/temple/ash/containers/{code}/balance").json()
        assert balance["balanced"] is True

    balance = client.get("/api/temple/ash/containers/ash-bin-merged-1/balance").json()
    assert balance["balanced"] is True
    assert balance["ledger_sum_grams"] == 1790.0
    assert balance["attributed"] == {"contents_sum_grams": 1800.0, "adjustments_grams": -10.0, "matches": True}
    merge_operation = [operation for operation in balance["operations"] if operation["operation_type"] == "merge"][0]
    assert merge_operation["conserved"] is True
    assert merge_operation["loss_grams"] == 10.0

    replay = client.post("/api/temple/ash/merges", json=merge_payload)
    assert replay.status_code == 201
    assert replay.json()["replay"] is True
    assert replay.json()["container"]["id"] == target["id"]

    remerge = client.post("/api/temple/ash/merges", json={**merge_payload, "operation_key": "merge-op-000002", "target_container_code": "ash-bin-merged-2"})
    assert remerge.status_code == 409


def test_reweigh_and_loss_adjustment_stay_balanced(client):
    prepare(client)
    seal(client, 1, weight_grams=1000.0)

    reweighed = client.post("/api/temple/ash/containers/ash-bin-000001/reweigh", json={"operation_key": "reweigh-op-1", "measured_weight_grams": 995.0, "tolerance_grams": 10.0, "actor": "keeper-a", "note": "例行复核"})
    assert reweighed.status_code == 200, reweighed.text
    assert reweighed.json()["ledger_entry"]["delta_grams"] == -5.0
    assert client.get("/api/temple/ash/anomalies").json()["items"] == []

    replay = client.post("/api/temple/ash/containers/ash-bin-000001/reweigh", json={"operation_key": "reweigh-op-1", "measured_weight_grams": 995.0, "tolerance_grams": 10.0, "actor": "keeper-a", "note": "例行复核"})
    assert replay.status_code == 200
    assert replay.json()["replay"] is True

    drifted = client.post("/api/temple/ash/containers/ash-bin-000001/reweigh", json={"operation_key": "reweigh-op-2", "measured_weight_grams": 900.0, "tolerance_grams": 10.0, "actor": "keeper-a"})
    assert drifted.status_code == 200
    anomalies = client.get("/api/temple/ash/anomalies", params={"anomaly_type": "weight_mismatch"}).json()["items"]
    assert len(anomalies) == 1
    assert anomalies[0]["detail"]["delta_grams"] == -95.0

    adjusted = client.post("/api/temple/ash/containers/ash-bin-000001/loss-adjustments", json={"operation_key": "loss-op-1", "loss_grams": 20.0, "actor": "keeper-a", "reason": "受潮结块剔除"})
    assert adjusted.status_code == 200, adjusted.text
    container = client.get("/api/temple/ash/containers/ash-bin-000001").json()
    assert container["current_weight_grams"] == 880.0

    excessive = client.post("/api/temple/ash/containers/ash-bin-000001/loss-adjustments", json={"operation_key": "loss-op-2", "loss_grams": 9999.0, "actor": "keeper-a", "reason": "异常损耗"})
    assert excessive.status_code == 422

    balance = client.get("/api/temple/ash/containers/ash-bin-000001/balance").json()
    assert balance["balanced"] is True
    assert balance["ledger_sum_grams"] == 880.0
    assert balance["attributed"]["matches"] is True


def test_storage_overdue_and_handover_timeout_raise_processable_anomalies(client):
    prepare(client)
    clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=UTC))
    service = AshCustodyService(get_connection(), clock)
    service.seal_batch(seal_payload(1, storage_hours=1))
    service.seal_batch(seal_payload(2, hall_code="side-hall", storage_hours=72))
    service.initiate_handover(handover_payload("handover-timeout-1", "ash-bin-000002", confirm_hours=1))

    clock.advance(hours=2)
    scan = service.scan_anomalies()
    assert len(scan["created_anomaly_ids"]) == 2
    assert scan["expired_handover_ids"] != []

    anomalies = {item["anomaly_type"]: item for item in service.list_anomalies()}
    overdue = anomalies["storage_overdue"]
    assert overdue["status"] == "open"
    assert overdue["detail"]["container_code"] == "ash-bin-000001"
    timeout = anomalies["handover_timeout"]
    assert timeout["detail"]["handover_key"] == "handover-timeout-1"

    container = service.container_detail("ash-bin-000002")
    assert container["status"] == "sealed"
    assert container["current_custodian"] == "keeper-a"
    assert container["handovers"][0]["status"] == "expired"

    rescanned = service.scan_anomalies()
    assert rescanned["created_anomaly_ids"] == []
    assert rescanned["expired_handover_ids"] == []

    acknowledged = service.acknowledge_anomaly(overdue["id"], "keeper-a")
    assert acknowledged["status"] == "acknowledged"
    resolved = service.resolve_anomaly(overdue["id"], "keeper-a", "已转运至集中暂存点并顺延期限", "2026-09-30T00:00:00Z")
    assert resolved["status"] == "resolved"
    assert service.container_detail("ash-bin-000001")["storage_deadline"] == "2026-09-30T00:00:00+00:00"

    handled = service.resolve_anomaly(timeout["id"], "keeper-b", "交接双方已重新当面交接")
    assert handled["status"] == "resolved"
    assert service.scan_anomalies()["created_anomaly_ids"] == []


def test_disposal_trace_recovers_sources_and_verifies_weight_balance(client):
    prepare(client)
    seal(client, 1, weight_grams=1000.0)
    seal(client, 2, weight_grams=800.0, hall_code="side-hall")
    handover = client.post("/api/temple/ash/handovers", json=handover_payload("handover-000001", "ash-bin-000001")).json()
    confirm_handover(client, handover["id"])
    merged = client.post(
        "/api/temple/ash/merges",
        json={
            "operation_key": "merge-op-000001",
            "temple_code": "lingyun-temple",
            "source_container_codes": ["ash-bin-000001", "ash-bin-000002"],
            "target_container_code": "ash-bin-merged-1",
            "loss_grams": 10.0,
            "actor": "keeper-b",
        },
    )
    assert merged.status_code == 201, merged.text
    client.post("/api/temple/ash/containers/ash-bin-merged-1/reweigh", json={"operation_key": "reweigh-op-1", "measured_weight_grams": 1785.0, "tolerance_grams": 10.0, "actor": "keeper-b"})
    client.post("/api/temple/ash/containers/ash-bin-merged-1/loss-adjustments", json={"operation_key": "loss-op-1", "loss_grams": 5.0, "actor": "keeper-b", "reason": "清扫损耗"})

    disposed = client.post("/api/temple/ash/containers/ash-bin-merged-1/disposals", json={"operation_key": "disposal-op-1", "disposal_code": "disposal-000001", "method": "交由资质单位资源化处置", "operator": "keeper-b"})
    assert disposed.status_code == 201, disposed.text
    assert disposed.json()["disposal"]["disposed_weight_grams"] == 1780.0

    replay = client.post("/api/temple/ash/containers/ash-bin-merged-1/disposals", json={"operation_key": "disposal-op-1", "disposal_code": "disposal-000001", "method": "交由资质单位资源化处置", "operator": "keeper-b"})
    assert replay.status_code == 201
    assert replay.json()["replay"] is True

    again = client.post("/api/temple/ash/containers/ash-bin-merged-1/disposals", json={"operation_key": "disposal-op-2", "disposal_code": "disposal-000002", "method": "重复处置", "operator": "keeper-b"})
    assert again.status_code == 409

    trace = client.get("/api/temple/ash/disposals/disposal-000001/trace").json()
    assert trace["balanced"] is True
    assert trace["chain_continuous"] is True
    assert trace["disposal_balanced"] is True
    assert {source["batch_code"] for source in trace["sources"]} == {"ash-batch-000001", "ash-batch-000002"}
    halls = {source["batch_code"]: source["hall_code"] for source in trace["sources"]}
    assert halls == {"ash-batch-000001": "main-hall", "ash-batch-000002": "side-hall"}
    for source in trace["sources"]:
        assert source["sealed_by"] == "keeper-a"
        assert source["container_path"][0]["container_code"] == "ash-bin-merged-1"
        assert source["container_path"][-1]["container_code"] in {"ash-bin-000001", "ash-bin-000002"}
    assert [handover["handover_key"] for handover in trace["handovers"]] == ["handover-000001"]
    equation = trace["weight_equation"]
    assert equation["merge_in"] == 1800.0
    assert equation["loss_adjustment"] == -15.0
    assert equation["reweigh"] == -5.0
    assert equation["disposal_out"] == -1780.0
    assert equation["sum_grams"] == 0.0
    assert trace["contents_total_grams"] == 1800.0
    assert trace["balance"]["balanced"] is True
    assert all(node["balanced"] for node in trace["balance"]["nodes"])

    batches = client.get("/api/temple/ash/containers/ash-bin-000001").json()
    assert batches["status"] == "merged"


def test_production_summary_reconciles_daily_output(client):
    prepare(client)
    clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=UTC))
    service = AshCustodyService(get_connection(), clock)
    service.seal_batch(seal_payload(1, weight_grams=1000.0))
    service.seal_batch(seal_payload(2, hall_code="side-hall", weight_grams=800.0))
    clock.advance(days=1)
    service.seal_batch(seal_payload(3, weight_grams=600.0))

    first_day = service.production_summary("lingyun-temple", "2026-09-26")
    assert first_day["total_batches"] == 2
    assert first_day["total_grams"] == 1800.0
    assert {item["hall_code"]: item["total_grams"] for item in first_day["halls"]} == {"main-hall": 1000.0, "side-hall": 800.0}
    second_day = service.production_summary("lingyun-temple", "2026-09-27")
    assert second_day["total_batches"] == 1
    assert second_day["total_grams"] == 600.0


def test_scan_detects_weight_imbalance_and_broken_chain(client):
    prepare(client)
    seal(client, 1)
    seal(client, 2, hall_code="side-hall")
    connection = get_connection()
    connection.execute("UPDATE ash_containers SET current_weight_grams=current_weight_grams+250 WHERE container_code='ash-bin-000001'")
    connection.execute("UPDATE ash_containers SET current_custodian='intruder' WHERE container_code='ash-bin-000002'")

    service = AshCustodyService(connection)
    scan = service.scan_anomalies()
    assert len(scan["created_anomaly_ids"]) == 2
    anomalies = {item["anomaly_type"]: item for item in service.list_anomalies()}
    mismatch = anomalies["weight_mismatch"]
    assert mismatch["detail"]["container_code"] == "ash-bin-000001"
    assert mismatch["detail"]["ledger_sum_grams"] == 1000.0
    assert mismatch["detail"]["current_weight_grams"] == 1250.0
    broken = anomalies["chain_broken"]
    assert broken["detail"]["container_code"] == "ash-bin-000002"
    assert broken["detail"]["expected_custodian"] == "keeper-a"
    assert broken["detail"]["actual_custodian"] == "intruder"

    balance = client.get("/api/temple/ash/containers/ash-bin-000001/balance").json()
    assert balance["balanced"] is False
    assert balance["discrepancies"]
