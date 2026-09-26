from __future__ import annotations

from datetime import timedelta

from app.core.clock import FrozenClock
from app.database import get_connection
from app.temple.ash_service import AshCustodyService


def _temple(client):
    response = client.post(
        "/api/temple/temples",
        json={
            "code": "lingyun-temple",
            "name": "凌云古寺",
            "temple_type": "heritage",
            "timezone": "Asia/Shanghai",
            "max_concurrent_mitigation_sessions": 10,
            "ventilation_capacity": 3000,
        },
    )
    assert response.status_code == 201, response.text
    for code, name in (("main-hall", "大雄宝殿"), ("guanyin-hall", "观音殿")):
        hall = client.post(
            "/api/temple/temples/lingyun-temple/halls",
            json={"code": code, "name": name, "visit_order": 1 if code == "main-hall" else 2,
                  "expected_visit_seconds": 900, "ventilation_capacity": 1200},
        )
        assert hall.status_code == 201, hall.text


def _container(client, code: str, capacity: int = 50_000):
    response = client.post(
        "/api/temple/ash/containers",
        json={"temple_code": "lingyun-temple", "code": code, "capacity_grams": capacity, "actor": "tests"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pack(client, container: str, hall: str, weight: int, **extra):
    payload = {
        "temple_code": "lingyun-temple",
        "hall_code": hall,
        "container_code": container,
        "weight_grams": weight,
        "handler": "monk-zhang",
        "seal_code": f"seal-{container}",
    }
    payload.update(extra)
    response = client.post("/api/temple/ash/packs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_pack_records_source_hall_weight_handler_and_balances_ledger(client):
    _temple(client)
    _container(client, "cask-01")
    detail = _pack(client, "cask-01", "main-hall", 12_000)
    assert detail["status"] == "sealed"
    assert detail["current_weight_grams"] == 12_000
    assert detail["current_batch_code"].startswith("ASH-B")
    assert detail["weight_balanced"] is True
    assert detail["chain_continuous"] is True
    event_types = [event["event_type"] for event in detail["events"]]
    assert event_types == ["register", "pack"]
    pack_event = detail["events"][-1]
    assert pack_event["actor"] == "monk-zhang"
    assert pack_event["detail"]["hall_code"] == "main-hall"
    assert [row["direction"] for row in detail["ledger"]] == ["in"]
    assert detail["ledger"][0]["balance_after_grams"] == 12_000


def test_over_capacity_pack_is_rejected(client):
    _temple(client)
    _container(client, "cask-small", capacity=100)
    response = client.post("/api/temple/ash/packs", json={
        "temple_code": "lingyun-temple", "hall_code": "main-hall",
        "container_code": "cask-small", "weight_grams": 200, "handler": "monk-zhang",
    })
    assert response.status_code == 422


def test_handover_requires_two_party_confirmation_and_snapshot(client):
    _temple(client)
    _container(client, "cask-01")
    _pack(client, "cask-01", "main-hall", 8_000)
    initiated = client.post("/api/temple/ash/transfers", json={
        "temple_code": "lingyun-temple", "handover_code": "HO-0001",
        "container_code": "cask-01", "from_party": "monk-zhang", "to_party": "keeper-li",
        "to_location": "西库房", "actor": "monk-zhang",
    })
    assert initiated.status_code == 201, initiated.text
    assert initiated.json()["status"] == "pending"
    detail = client.get("/api/temple/ash/containers/cask-01").json()
    assert detail["status"] == "in_transit"
    confirmed = client.post("/api/temple/ash/transfers/HO-0001/confirm", json={"confirmed_by": "keeper-li"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    detail = client.get("/api/temple/ash/containers/cask-01").json()
    assert detail["status"] == "sealed"
    assert detail["custodian"] == "keeper-li"
    assert detail["location"] == "西库房"
    types = [event["event_type"] for event in detail["events"]]
    assert types == ["register", "pack", "transfer_initiate", "transfer_confirm"]
    assert detail["chain_continuous"] is True
    assert detail["weight_balanced"] is True


def test_same_receipt_replay_does_not_transfer_twice(client):
    _temple(client)
    _container(client, "cask-01")
    _pack(client, "cask-01", "main-hall", 8_000)
    payload = {
        "temple_code": "lingyun-temple", "handover_code": "HO-REPLAY", "idempotency_key": "idem-1",
        "container_code": "cask-01", "from_party": "monk-zhang", "to_party": "keeper-li",
        "to_location": "西库房", "actor": "monk-zhang",
    }
    first = client.post("/api/temple/ash/transfers", json=payload)
    assert first.status_code == 201
    client.post("/api/temple/ash/transfers/HO-REPLAY/confirm", json={"confirmed_by": "keeper-li"})
    replayed = client.post("/api/temple/ash/transfers", json=payload)
    assert replayed.status_code == 201
    assert replayed.json()["id"] == first.json()["id"]
    assert replayed.json()["replayed"] is True
    # 交接单仍然只有一张
    handovers = get_connection().execute("SELECT COUNT(*) FROM ash_handovers").fetchone()[0]
    assert handovers == 1


def test_concurrent_claim_of_same_container_succeeds_once(client):
    _temple(client)
    _container(client, "cask-01")
    _pack(client, "cask-01", "main-hall", 8_000)
    first = client.post("/api/temple/ash/transfers", json={
        "temple_code": "lingyun-temple", "handover_code": "HO-C1",
        "container_code": "cask-01", "from_party": "monk-zhang", "to_party": "keeper-li",
        "to_location": "西库房", "actor": "monk-zhang",
    })
    assert first.status_code == 201
    second = client.post("/api/temple/ash/transfers", json={
        "temple_code": "lingyun-temple", "handover_code": "HO-C2",
        "container_code": "cask-01", "from_party": "keeper-li", "to_party": "driver-wang",
        "to_location": "处置场", "actor": "keeper-li",
    })
    assert second.status_code == 409
    # 同一张交接凭据重复领取也只能成功一次
    client.post("/api/temple/ash/transfers/HO-C1/confirm", json={"confirmed_by": "keeper-li"})
    again = client.post("/api/temple/ash/transfers/HO-C1/confirm", json={"confirmed_by": "keeper-li"})
    assert again.status_code == 409


def test_receipt_weight_mismatch_opens_actionable_anomaly_and_holds_transfer(client):
    _temple(client)
    _container(client, "cask-01")
    _pack(client, "cask-01", "main-hall", 8_000)
    client.post("/api/temple/ash/transfers", json={
        "temple_code": "lingyun-temple", "handover_code": "HO-W",
        "container_code": "cask-01", "from_party": "monk-zhang", "to_party": "keeper-li",
        "to_location": "西库房", "actor": "monk-zhang",
    })
    rejected = client.post("/api/temple/ash/transfers/HO-W/confirm",
                           json={"confirmed_by": "keeper-li", "measured_weight_grams": 7_500})
    assert rejected.status_code == 409
    anomalies = client.get("/api/temple/ash/anomalies").json()["items"]
    assert len(anomalies) == 1
    assert anomalies[0]["anomaly_type"] == "receipt_weight_mismatch"
    assert anomalies[0]["state"] == "open"
    # 交接单仍挂起，容器仍在运输中
    assert client.get("/api/temple/ash/transfers/HO-W").json()["status"] == "pending"
    assert client.get("/api/temple/ash/containers/cask-01").json()["status"] == "in_transit"
    # 异常可以认领并处理
    acknowledged = client.post(f"/api/temple/ash/anomalies/{anomalies[0]['id']}/acknowledge", json={"actor": "manager"})
    assert acknowledged.status_code == 200
    assert acknowledged.json()["state"] == "acknowledged"
    resolved = client.post(f"/api/temple/ash/anomalies/{anomalies[0]['id']}/resolve",
                           json={"actor": "manager", "resolution": "复核称重恢复8000克"})
    assert resolved.json()["state"] == "resolved"


def test_merge_conserves_weight_with_loss_adjustment(client):
    _temple(client)
    _container(client, "cask-a")
    _container(client, "cask-b")
    _container(client, "cask-m")
    _pack(client, "cask-a", "main-hall", 5_000, batch_code="ASH-B-A")
    _pack(client, "cask-b", "guanyin-hall", 3_200, batch_code="ASH-B-B")
    merged = client.post("/api/temple/ash/merges", json={
        "temple_code": "lingyun-temple", "container_code": "cask-m",
        "source_container_codes": ["cask-a", "cask-b"],
        "weight_grams": 8_000, "loss_reason": "拆包合并扬尘损耗200克",
        "actor": "keeper-li", "batch_code": "ASH-M-1", "location": "东库房",
    })
    assert merged.status_code == 201, merged.text
    body = merged.json()
    assert body["merge"] == {"batch_code": "ASH-M-1", "input_total_grams": 8_200, "output_grams": 8_000, "loss_adjustment_grams": 200}
    assert body["current_weight_grams"] == 8_000
    assert body["weight_balanced"] is True
    # 来源容器已清空且各自流水闭合到零
    for code in ("cask-a", "cask-b"):
        source = client.get(f"/api/temple/ash/containers/{code}").json()
        assert source["status"] == "empty"
        assert source["current_weight_grams"] == 0
        assert source["weight_balanced"] is True
    # 没有损耗原因必须拒绝
    _container(client, "cask-c")
    _container(client, "cask-d")
    _container(client, "cask-n2")
    _pack(client, "cask-c", "main-hall", 1_000, batch_code="ASH-B-C")
    _pack(client, "cask-d", "main-hall", 1_000, batch_code="ASH-B-D")
    refused = client.post("/api/temple/ash/merges", json={
        "temple_code": "lingyun-temple", "container_code": "cask-n2",
        "source_container_codes": ["cask-c", "cask-d"],
        "weight_grams": 1_900, "actor": "keeper-li",
    })
    assert refused.status_code == 422


def test_reweigh_records_adjustment_and_anomaly(client):
    _temple(client)
    _container(client, "cask-01")
    _pack(client, "cask-01", "main-hall", 8_000)
    reweighed = client.post("/api/temple/ash/containers/cask-01/reweigh", json={
        "actor": "manager", "measured_weight_grams": 7_900, "reason": "例行复核发现少量扬尘",
    })
    assert reweighed.status_code == 200
    body = reweighed.json()
    assert body["current_weight_grams"] == 7_900
    assert body["weight_balanced"] is True
    directions = [row["direction"] for row in body["ledger"]]
    assert directions == ["in", "adjust"]
    anomalies = client.get("/api/temple/ash/anomalies").json()["items"]
    assert any(item["anomaly_type"] == "weight_verification" for item in anomalies)


def test_disposal_traces_back_to_all_sources_and_verifies_balance(client):
    _temple(client)
    _container(client, "cask-a")
    _container(client, "cask-b")
    _container(client, "cask-m")
    _pack(client, "cask-a", "main-hall", 5_000, batch_code="ASH-B-A")
    _pack(client, "cask-b", "guanyin-hall", 3_000, batch_code="ASH-B-B")
    merged = client.post("/api/temple/ash/merges", json={
        "temple_code": "lingyun-temple", "container_code": "cask-m",
        "source_container_codes": ["cask-a", "cask-b"], "actor": "keeper-li",
        "batch_code": "ASH-M-1",
    })
    assert merged.status_code == 201
    disposal = client.post("/api/temple/ash/disposals", json={
        "temple_code": "lingyun-temple", "container_code": "cask-m",
        "disposal_code": "DSP-0001", "method": "ritual_burn",
        "actor": "manager", "witness": "abbot",
    })
    assert disposal.status_code == 201, disposal.text
    trace = client.get("/api/temple/ash/disposals/DSP-0001/trace").json()
    assert trace["disposal"]["disposal_code"] == "DSP-0001"
    check = trace["weight_check"]
    assert check == {"leaf_total_grams": 8_000, "adjustments_grams": 0,
                     "expected_grams": 8_000, "disposed_grams": 8_000, "balanced": True}
    halls = {item["hall_code"]: item["total_grams"] for item in trace["sources"]["by_hall"]}
    assert halls == {"main-hall": 5_000, "guanyin-hall": 3_000}
    origin_types = {node["batch_code"]: node["origin_type"] for node in trace["nodes"]}
    assert origin_types == {"ASH-B-A": "collection", "ASH-B-B": "collection", "ASH-M-1": "merge"}
    assert all(node["balanced"] for node in trace["nodes"])
    assert trace["chain_continuous"] is True
    # 处置后容器流水闭合为零
    container = client.get("/api/temple/ash/containers/cask-m").json()
    assert container["status"] == "disposed"
    assert container["weight_balanced"] is True
    # 批次正向也能追到最终处置
    forward = client.get("/api/temple/ash/batches/ASH-B-A/trace").json()
    assert "DSP-0001" in {item["disposal_code"] for item in forward["disposals"]}


def test_disposal_trace_balances_when_chain_includes_loss_and_reweigh(client):
    _temple(client)
    _container(client, "cask-a")
    _container(client, "cask-b")
    _container(client, "cask-m")
    _pack(client, "cask-a", "main-hall", 5_000, batch_code="ASH-B-A")
    _pack(client, "cask-b", "guanyin-hall", 3_200, batch_code="ASH-B-B")
    client.post("/api/temple/ash/merges", json={
        "temple_code": "lingyun-temple", "container_code": "cask-m",
        "source_container_codes": ["cask-a", "cask-b"],
        "weight_grams": 8_000, "loss_reason": "合并扬尘损耗200克",
        "actor": "keeper-li", "batch_code": "ASH-M-1",
    })
    client.post("/api/temple/ash/containers/cask-m/reweigh", json={
        "actor": "manager", "measured_weight_grams": 7_900, "reason": "复核再损耗100克",
    })
    disposal = client.post("/api/temple/ash/disposals", json={
        "temple_code": "lingyun-temple", "container_code": "cask-m",
        "disposal_code": "DSP-0002", "actor": "manager", "measured_weight_grams": 7_900,
    })
    assert disposal.status_code == 201, disposal.text
    trace = client.get("/api/temple/ash/disposals/DSP-0002/trace").json()
    check = trace["weight_check"]
    # 8200(原始封装) - 200(合并损耗) - 100(复核损耗) = 7900
    assert check == {"leaf_total_grams": 8_200, "adjustments_grams": -300,
                     "expected_grams": 7_900, "disposed_grams": 7_900, "balanced": True}
    assert all(node["balanced"] for node in trace["nodes"])


def test_storage_overdue_and_stalled_handover_scan(client):
    _temple(client)
    _container(client, "cask-01")
    _pack(client, "cask-01", "main-hall", 8_000, retention_hours=48)
    detail = client.get("/api/temple/ash/containers/cask-01").json()
    assert detail["status"] == "stored"
    connection = get_connection()
    stored_until = connection.execute("SELECT stored_until FROM ash_containers WHERE code='cask-01'").fetchone()[0]
    from app.core.clock import from_storage
    clock = FrozenClock(from_storage(stored_until) + timedelta(hours=1))
    AshCustodyService(connection, clock).scan_anomalies()
    anomalies = client.get("/api/temple/ash/anomalies").json()["items"]
    assert any(item["anomaly_type"] == "storage_overdue" and item["state"] == "open" for item in anomalies)

    _container(client, "cask-02")
    _pack(client, "cask-02", "guanyin-hall", 2_000, batch_code="ASH-B-X")
    client.post("/api/temple/ash/transfers", json={
        "temple_code": "lingyun-temple", "handover_code": "HO-STALL",
        "container_code": "cask-02", "from_party": "monk-zhang", "to_party": "keeper-li",
        "to_location": "西库房", "actor": "monk-zhang",
    })
    stall_clock = FrozenClock(from_storage(stored_until) + timedelta(hours=48))
    AshCustodyService(connection, stall_clock).scan_anomalies(stall_hours=24)
    anomalies = client.get("/api/temple/ash/anomalies", params={"anomaly_type": "handover_stalled"}).json()["items"]
    assert len(anomalies) == 1
    assert anomalies[0]["state"] == "open"
    # 重复扫描不会重复开单
    AshCustodyService(connection, stall_clock).scan_anomalies(stall_hours=24)
    assert client.get("/api/temple/ash/anomalies", params={"anomaly_type": "handover_stalled"}).json()["items"].__len__() == 1


def test_parallel_claims_on_same_container_only_one_wins(client):
    import threading

    from app.core.errors import ConflictError
    from app.temple.ash_service import AshCustodyService

    _temple(client)
    _container(client, "cask-01")
    _pack(client, "cask-01", "main-hall", 8_000)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def claim(index: int) -> None:
        service = AshCustodyService()
        payload = {
            "temple_code": "lingyun-temple", "handover_code": f"HO-P{index}",
            "container_code": "cask-01", "from_party": f"party-{index}",
            "to_party": f"keeper-{index}", "to_location": f"库房-{index}", "actor": "tests",
        }
        barrier.wait()
        try:
            service.initiate_transfer(payload)
            results.append("ok")
        except ConflictError:
            results.append("conflict")

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1
    assert results.count("conflict") == 7


def test_disposal_during_transfer_is_blocked_as_broken_chain(client):
    _temple(client)
    _container(client, "cask-01")
    _pack(client, "cask-01", "main-hall", 8_000)
    client.post("/api/temple/ash/transfers", json={
        "temple_code": "lingyun-temple", "handover_code": "HO-X",
        "container_code": "cask-01", "from_party": "monk-zhang", "to_party": "keeper-li",
        "to_location": "西库房", "actor": "monk-zhang",
    })
    response = client.post("/api/temple/ash/disposals", json={
        "temple_code": "lingyun-temple", "container_code": "cask-01",
        "disposal_code": "DSP-X", "actor": "manager",
    })
    assert response.status_code == 409
    anomalies = client.get("/api/temple/ash/anomalies", params={"anomaly_type": "handover_stalled"}).json()["items"]
    assert len(anomalies) == 1
