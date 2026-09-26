from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.temple.repository import TempleRepository
from app.temple.schema import ensure_temple_schema

# 重量统一保留到毫克，平衡校验容差取半个毫克
GRAMS_EPSILON = 0.0005
# 允许发起交接、复核称重、损耗调整和最终处置的容器状态
TRANSFERABLE_STATES = ("sealed", "stored")


def _grams(value: float) -> float:
    return round(float(value), 3)


def _balanced(left: float, right: float) -> bool:
    return abs(_grams(left) - _grams(right)) <= GRAMS_EPSILON


class AshCustodyService:
    """香灰批次与容器交接链：封装、交接、合并、复核、处置与异常处理。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = TempleRepository(self.connection)

    # ------------------------------------------------------------------
    # 封装：建立批次与容器，记录来源殿堂、重量、经手人和时间
    # ------------------------------------------------------------------
    def seal_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        hall = self.repository.hall_by_code(temple["id"], payload["hall_code"])
        if hall is None:
            raise NotFoundError("来源殿堂不存在")
        digest = request_fingerprint(payload)
        now_value = self.clock.now()
        now = to_storage(now_value)
        deadline = to_storage(now_value + timedelta(hours=float(payload["storage_hours"])))
        weight = _grams(payload["weight_grams"])
        with transaction(immediate=True) as connection:
            existing = self._operation_by_key(connection, payload["operation_key"])
            if existing is not None:
                return self._replay(connection, existing, digest, self._seal_result)
            try:
                operation_id = self._insert_operation(connection, payload["operation_key"], "seal", temple["id"], 0.0, payload["sealed_by"], payload.get("note", ""), digest, now)
                container_cursor = connection.execute(
                    "INSERT INTO ash_containers(container_code,temple_id,status,current_custodian,current_weight_grams,storage_deadline,sealed_at,sealed_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (payload["container_code"], temple["id"], "sealed", payload["sealed_by"], weight, deadline, now, payload["sealed_by"], now, now),
                )
                container_id = container_cursor.lastrowid
                batch_cursor = connection.execute(
                    "INSERT INTO ash_batches(batch_code,temple_id,hall_id,container_id,weight_grams,sealed_by,sealed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (payload["batch_code"], temple["id"], hall["id"], container_id, weight, payload["sealed_by"], now, now, now),
                )
                content_cursor = connection.execute(
                    "INSERT INTO ash_container_contents(container_id,batch_id,origin_content_id,operation_id,weight_grams,created_at) VALUES(?,?,?,?,?,?)",
                    (container_id, batch_cursor.lastrowid, None, operation_id, weight, now),
                )
                self._ledger(connection, operation_id, container_id, content_cursor.lastrowid, batch_cursor.lastrowid, "seal_in", weight, weight, payload["sealed_by"], payload.get("note", ""), now)
                self._event(connection, container_id, "sealed", payload["sealed_by"], {"batch_code": payload["batch_code"], "hall_code": payload["hall_code"], "weight_grams": weight, "storage_deadline": deadline}, now)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("容器编码或批次编码已存在") from exc
            return self._seal_result(connection, operation_id)

    # ------------------------------------------------------------------
    # 交接：双方确认，容器状态连续，凭据重放不产生第二次转移
    # ------------------------------------------------------------------
    def initiate_handover(self, payload: dict[str, Any]) -> dict[str, Any]:
        digest = request_fingerprint(payload)
        now_value = self.clock.now()
        now = to_storage(now_value)
        deadline = to_storage(now_value + timedelta(hours=float(payload["confirm_hours"])))
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM ash_handovers WHERE handover_key=?", (payload["handover_key"],)).fetchone()
            if existing is not None:
                if existing["payload_digest"] != digest:
                    raise ConflictError("相同 handover_key 对应了不同交接内容")
                return self._handover_detail(connection, existing["id"], replay=True)
            container = self._container_by_code(connection, payload["container_code"])
            if container["current_custodian"] != payload["from_custodian"]:
                raise ConflictError(
                    "交出方不是容器当前保管人，交接链不连续",
                    context={"current_custodian": container["current_custodian"], "from_custodian": payload["from_custodian"]},
                )
            # 原子领取：并发领取同一容器时只有一个事务能把状态推进到 in_transit
            claimed = connection.execute(
                "UPDATE ash_containers SET status='in_transit',version=version+1,updated_at=? WHERE id=? AND status IN ('sealed','stored')",
                (now, container["id"]),
            )
            if claimed.rowcount != 1:
                raise ConflictError("容器已被领取或当前状态不允许交接", context={"container_code": container["container_code"], "status": container["status"]})
            cursor = connection.execute(
                "INSERT INTO ash_handovers(handover_key,container_id,temple_id,from_custodian,to_custodian,container_status_before,confirm_deadline,initiated_by,initiated_at,payload_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (payload["handover_key"], container["id"], container["temple_id"], payload["from_custodian"], payload["to_custodian"], container["status"], deadline, payload["initiated_by"], now, digest),
            )
            self._event(connection, container["id"], "handover_initiated", payload["initiated_by"], {"handover_key": payload["handover_key"], "from_custodian": payload["from_custodian"], "to_custodian": payload["to_custodian"], "confirm_deadline": deadline}, now)
            return self._handover_detail(connection, cursor.lastrowid)

    def confirm_handover(self, handover_id: int, side: str, actor: str) -> dict[str, Any]:
        if side not in ("from", "to"):
            raise ValidationError("确认侧必须是 from 或 to")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            handover = self._handover(connection, handover_id)
            if handover["status"] == "confirmed":
                return self._handover_detail(connection, handover_id, replay=True)
            if handover["status"] != "pending":
                raise ConflictError("交接已关闭，不能继续确认", context={"status": handover["status"]})
            expected = handover["from_custodian"] if side == "from" else handover["to_custodian"]
            if actor != expected:
                raise ConflictError("确认人必须是本侧交接当事人", context={"side": side, "expected": expected})
            column = "from_confirmed_at" if side == "from" else "to_confirmed_at"
            if handover[column] is not None:
                return self._handover_detail(connection, handover_id, replay=True)
            connection.execute(f"UPDATE ash_handovers SET {column}=?,version=version+1 WHERE id=? AND status='pending' AND {column} IS NULL", (now, handover_id))
            self._event(connection, handover["container_id"], f"handover_{side}_confirmed", actor, {"handover_key": handover["handover_key"]}, now)
            refreshed = self._handover(connection, handover_id)
            if refreshed["from_confirmed_at"] and refreshed["to_confirmed_at"]:
                moved = connection.execute(
                    "UPDATE ash_containers SET status='stored',current_custodian=?,version=version+1,updated_at=? WHERE id=? AND status='in_transit'",
                    (handover["to_custodian"], now, handover["container_id"]),
                )
                if moved.rowcount != 1:
                    raise ConflictError("容器状态已变化，交接无法完成")
                connection.execute("UPDATE ash_handovers SET status='confirmed',confirmed_at=?,version=version+1 WHERE id=?", (now, handover_id))
                self._event(connection, handover["container_id"], "handover_confirmed", actor, {"handover_key": handover["handover_key"], "custodian": handover["to_custodian"]}, now)
            return self._handover_detail(connection, handover_id)

    def cancel_handover(self, handover_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            handover = self._handover(connection, handover_id)
            if handover["status"] != "pending":
                raise ConflictError("只有待确认的交接可以取消", context={"status": handover["status"]})
            if actor not in {handover["initiated_by"], handover["from_custodian"]}:
                raise ConflictError("只有发起人或交出方可以取消交接")
            connection.execute("UPDATE ash_handovers SET status='cancelled',closed_at=?,close_reason=?,version=version+1 WHERE id=? AND status='pending'", (now, reason, handover_id))
            connection.execute("UPDATE ash_containers SET status=?,version=version+1,updated_at=? WHERE id=? AND status='in_transit'", (handover["container_status_before"], now, handover["container_id"]))
            self._event(connection, handover["container_id"], "handover_cancelled", actor, {"handover_key": handover["handover_key"], "reason": reason}, now)
            return self._handover_detail(connection, handover_id)

    def handover_detail(self, handover_id: int) -> dict[str, Any]:
        return self._handover_detail(self.connection, handover_id)

    # ------------------------------------------------------------------
    # 拆包合并：来源容器清空，目标容器承接全部批次，损耗进入守恒流水
    # ------------------------------------------------------------------
    def merge_containers(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        digest = request_fingerprint(payload)
        now = to_storage(self.clock.now())
        loss = _grams(payload["loss_grams"])
        with transaction(immediate=True) as connection:
            existing = self._operation_by_key(connection, payload["operation_key"])
            if existing is not None:
                return self._replay(connection, existing, digest, self._merge_result)
            sources = [self._container_by_code(connection, code) for code in payload["source_container_codes"]]
            for source in sources:
                if source["temple_id"] != temple["id"]:
                    raise ValidationError("来源容器不属于目标寺院")
                if source["status"] not in TRANSFERABLE_STATES:
                    raise ConflictError("来源容器当前状态不允许合并", context={"container_code": source["container_code"], "status": source["status"]})
            total = _grams(sum(source["current_weight_grams"] for source in sources))
            if loss >= total:
                raise ValidationError("合并损耗必须小于来源总重量")
            target_weight = _grams(total - loss)
            deadline = min(source["storage_deadline"] for source in sources)
            operation_id = self._insert_operation(connection, payload["operation_key"], "merge", temple["id"], loss, payload["actor"], payload.get("note", ""), digest, now)
            try:
                target_cursor = connection.execute(
                    "INSERT INTO ash_containers(container_code,temple_id,status,current_custodian,current_weight_grams,storage_deadline,sealed_at,sealed_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (payload["target_container_code"], temple["id"], "stored", payload["actor"], target_weight, deadline, now, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("目标容器编码已存在") from exc
            target_id = target_cursor.lastrowid
            running = 0.0
            for source in sources:
                # 逐个原子领取来源容器，任何并发占用都会让本次合并失败回滚
                claimed = connection.execute(
                    "UPDATE ash_containers SET status='merged',current_weight_grams=0,version=version+1,updated_at=? WHERE id=? AND status IN ('sealed','stored') AND current_weight_grams=?",
                    (now, source["id"], source["current_weight_grams"]),
                )
                if claimed.rowcount != 1:
                    raise ConflictError("来源容器已被占用或重量已变化，合并中止", context={"container_code": source["container_code"]})
                self._ledger(connection, operation_id, source["id"], None, None, "merge_out", -source["current_weight_grams"], 0.0, payload["actor"], payload.get("note", ""), now)
                contents = connection.execute("SELECT * FROM ash_container_contents WHERE container_id=? AND status='active' ORDER BY id", (source["id"],)).fetchall()
                for content in contents:
                    connection.execute("UPDATE ash_container_contents SET status='merged_out',merged_into_container_id=?,closed_at=? WHERE id=?", (target_id, now, content["id"]))
                    moved = connection.execute(
                        "INSERT INTO ash_container_contents(container_id,batch_id,origin_content_id,operation_id,weight_grams,created_at) VALUES(?,?,?,?,?,?)",
                        (target_id, content["batch_id"], content["id"], operation_id, content["weight_grams"], now),
                    )
                    running = _grams(running + content["weight_grams"])
                    self._ledger(connection, operation_id, target_id, moved.lastrowid, content["batch_id"], "merge_in", content["weight_grams"], running, payload["actor"], payload.get("note", ""), now)
                self._event(connection, source["id"], "merged_out", payload["actor"], {"target_container_code": payload["target_container_code"], "weight_grams": source["current_weight_grams"]}, now)
            if loss > 0:
                self._ledger(connection, operation_id, target_id, None, None, "loss_adjustment", -loss, target_weight, payload["actor"], payload.get("note", ""), now)
            self._event(connection, target_id, "merged_in", payload["actor"], {"source_container_codes": payload["source_container_codes"], "weight_grams": target_weight, "loss_grams": loss}, now)
            return self._merge_result(connection, operation_id)

    # ------------------------------------------------------------------
    # 称重复核与损耗调整：只改重量，全部进入守恒流水
    # ------------------------------------------------------------------
    def reweigh_container(self, container_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        container = self._container_by_code(self.connection, container_code)
        digest = request_fingerprint({"container_code": container_code, **payload})
        now = to_storage(self.clock.now())
        measured = _grams(payload["measured_weight_grams"])
        with transaction(immediate=True) as connection:
            existing = self._operation_by_key(connection, payload["operation_key"])
            if existing is not None:
                return self._replay(connection, existing, digest, self._container_op_result)
            delta = _grams(measured - container["current_weight_grams"])
            updated = connection.execute(
                "UPDATE ash_containers SET current_weight_grams=?,version=version+1,updated_at=? WHERE id=? AND status IN ('sealed','stored') AND current_weight_grams=?",
                (measured, now, container["id"], container["current_weight_grams"]),
            )
            if updated.rowcount != 1:
                raise ConflictError("容器当前状态不允许复核称重或重量已被并发修改", context={"container_code": container_code, "status": container["status"]})
            operation_id = self._insert_operation(connection, payload["operation_key"], "reweigh", container["temple_id"], 0.0, payload["actor"], payload.get("note", ""), digest, now)
            self._ledger(connection, operation_id, container["id"], None, None, "reweigh", delta, measured, payload["actor"], payload.get("note", ""), now)
            self._event(connection, container["id"], "reweighed", payload["actor"], {"previous_weight_grams": container["current_weight_grams"], "measured_weight_grams": measured, "delta_grams": delta}, now)
            if abs(delta) > float(payload["tolerance_grams"]):
                self._open_anomaly(connection, "weight_mismatch", container["temple_id"], container["id"], None, {"container_code": container_code, "delta_grams": delta, "tolerance_grams": payload["tolerance_grams"], "operation_key": payload["operation_key"]}, now)
            return self._container_op_result(connection, operation_id)

    def adjust_loss(self, container_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        container = self._container_by_code(self.connection, container_code)
        digest = request_fingerprint({"container_code": container_code, **payload})
        now = to_storage(self.clock.now())
        loss = _grams(payload["loss_grams"])
        if loss > container["current_weight_grams"]:
            raise ValidationError("损耗重量不能超过容器当前重量")
        with transaction(immediate=True) as connection:
            existing = self._operation_by_key(connection, payload["operation_key"])
            if existing is not None:
                return self._replay(connection, existing, digest, self._container_op_result)
            remaining = _grams(container["current_weight_grams"] - loss)
            updated = connection.execute(
                "UPDATE ash_containers SET current_weight_grams=?,version=version+1,updated_at=? WHERE id=? AND status IN ('sealed','stored') AND current_weight_grams=?",
                (remaining, now, container["id"], container["current_weight_grams"]),
            )
            if updated.rowcount != 1:
                raise ConflictError("容器当前状态不允许损耗调整或重量已被并发修改", context={"container_code": container_code, "status": container["status"]})
            operation_id = self._insert_operation(connection, payload["operation_key"], "loss_adjustment", container["temple_id"], loss, payload["actor"], payload["reason"], digest, now)
            self._ledger(connection, operation_id, container["id"], None, None, "loss_adjustment", -loss, remaining, payload["actor"], payload["reason"], now)
            self._event(connection, container["id"], "loss_adjusted", payload["actor"], {"loss_grams": loss, "reason": payload["reason"], "remaining_weight_grams": remaining}, now)
            return self._container_op_result(connection, operation_id)

    # ------------------------------------------------------------------
    # 最终处置：容器终态，批次随之处置，保留完整反向追溯链
    # ------------------------------------------------------------------
    def dispose_container(self, container_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        digest = request_fingerprint({"container_code": container_code, **payload})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            existing = self._operation_by_key(connection, payload["operation_key"])
            if existing is not None:
                return self._replay(connection, existing, digest, self._disposal_result)
            container = self._container_by_code(connection, container_code)
            disposed = connection.execute(
                "UPDATE ash_containers SET status='disposed',current_weight_grams=0,version=version+1,updated_at=? WHERE id=? AND status IN ('sealed','stored')",
                (now, container["id"]),
            )
            if disposed.rowcount != 1:
                raise ConflictError("容器当前状态不允许最终处置", context={"container_code": container_code, "status": container["status"]})
            operation_id = self._insert_operation(connection, payload["operation_key"], "disposal", container["temple_id"], 0.0, payload["operator"], payload.get("note", ""), digest, now)
            try:
                disposal_cursor = connection.execute(
                    "INSERT INTO ash_disposals(disposal_code,container_id,temple_id,operation_id,disposed_weight_grams,method,operator,disposed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (payload["disposal_code"], container["id"], container["temple_id"], operation_id, container["current_weight_grams"], payload["method"], payload["operator"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("处置编码已存在") from exc
            disposal_id = disposal_cursor.lastrowid
            contents = connection.execute("SELECT * FROM ash_container_contents WHERE container_id=? AND status='active' ORDER BY id", (container["id"],)).fetchall()
            for content in contents:
                connection.execute("UPDATE ash_container_contents SET status='disposed',disposal_id=?,closed_at=? WHERE id=?", (disposal_id, now, content["id"]))
                connection.execute("UPDATE ash_batches SET status='disposed',updated_at=? WHERE id=? AND NOT EXISTS(SELECT 1 FROM ash_container_contents WHERE batch_id=? AND status='active')", (now, content["batch_id"], content["batch_id"]))
            self._ledger(connection, operation_id, container["id"], None, None, "disposal_out", -container["current_weight_grams"], 0.0, payload["operator"], payload.get("note", ""), now)
            self._event(connection, container["id"], "disposed", payload["operator"], {"disposal_code": payload["disposal_code"], "method": payload["method"], "disposed_weight_grams": container["current_weight_grams"]}, now)
            return self._disposal_result(connection, operation_id)

    # ------------------------------------------------------------------
    # 异常扫描：暂存超期、交接断链、保管链断裂、流水不平衡
    # ------------------------------------------------------------------
    def scan_anomalies(self, actor: str = "ash-anomaly-scanner") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        created: list[int] = []
        expired_handovers: list[int] = []
        with transaction(immediate=True) as connection:
            overdue = connection.execute("SELECT * FROM ash_containers WHERE status IN ('sealed','stored') AND storage_deadline<? ORDER BY id", (now,)).fetchall()
            for container in overdue:
                anomaly_id = self._open_anomaly(connection, "storage_overdue", container["temple_id"], container["id"], None, {"container_code": container["container_code"], "storage_deadline": container["storage_deadline"], "current_custodian": container["current_custodian"]}, now)
                if anomaly_id is not None:
                    created.append(anomaly_id)
            stale = connection.execute("SELECT * FROM ash_handovers WHERE status='pending' AND confirm_deadline<? ORDER BY id", (now,)).fetchall()
            for handover in stale:
                expired = connection.execute("UPDATE ash_handovers SET status='expired',closed_at=?,close_reason='confirm_deadline_elapsed',version=version+1 WHERE id=? AND status='pending'", (now, handover["id"]))
                if expired.rowcount != 1:
                    continue
                container = connection.execute("SELECT * FROM ash_containers WHERE id=?", (handover["container_id"],)).fetchone()
                if container is not None and container["status"] == "in_transit":
                    connection.execute("UPDATE ash_containers SET status=?,version=version+1,updated_at=? WHERE id=?", (handover["container_status_before"], now, container["id"]))
                self._event(connection, handover["container_id"], "handover_expired", actor, {"handover_key": handover["handover_key"]}, now)
                anomaly_id = self._open_anomaly(connection, "handover_timeout", handover["temple_id"], handover["container_id"], handover["id"], {"handover_key": handover["handover_key"], "from_custodian": handover["from_custodian"], "to_custodian": handover["to_custodian"], "confirm_deadline": handover["confirm_deadline"]}, now)
                if anomaly_id is not None:
                    created.append(anomaly_id)
                expired_handovers.append(handover["id"])
            containers = connection.execute("SELECT * FROM ash_containers ORDER BY id").fetchall()
            for container in containers:
                if container["status"] in TRANSFERABLE_STATES:
                    expected = self._expected_custodian(connection, container)
                    if expected != container["current_custodian"]:
                        anomaly_id = self._open_anomaly(connection, "chain_broken", container["temple_id"], container["id"], None, {"container_code": container["container_code"], "expected_custodian": expected, "actual_custodian": container["current_custodian"]}, now)
                        if anomaly_id is not None:
                            created.append(anomaly_id)
                elif container["status"] == "in_transit":
                    pending = connection.execute("SELECT 1 FROM ash_handovers WHERE container_id=? AND status='pending'", (container["id"],)).fetchone()
                    if pending is None:
                        anomaly_id = self._open_anomaly(connection, "chain_broken", container["temple_id"], container["id"], None, {"container_code": container["container_code"], "reason": "in_transit_without_pending_handover"}, now)
                        if anomaly_id is not None:
                            created.append(anomaly_id)
                ledger_sum = connection.execute("SELECT COALESCE(SUM(delta_grams),0) FROM ash_weight_ledger WHERE container_id=?", (container["id"],)).fetchone()[0]
                if not _balanced(ledger_sum, container["current_weight_grams"]):
                    anomaly_id = self._open_anomaly(connection, "weight_mismatch", container["temple_id"], container["id"], None, {"container_code": container["container_code"], "ledger_sum_grams": _grams(ledger_sum), "current_weight_grams": container["current_weight_grams"]}, now)
                    if anomaly_id is not None:
                        created.append(anomaly_id)
        return {"created_anomaly_ids": created, "expired_handover_ids": expired_handovers, "scanned_at": now}

    def list_anomalies(self, status: str | None = None, anomaly_type: str | None = None, temple_code: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("a.status=?")
            params.append(status)
        if anomaly_type:
            clauses.append("a.anomaly_type=?")
            params.append(anomaly_type)
        if temple_code:
            clauses.append("n.code=?")
            params.append(temple_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT a.*,n.code AS temple_code,c.container_code FROM ash_anomalies a JOIN temple_sites n ON n.id=a.temple_id LEFT JOIN ash_containers c ON c.id=a.container_id" + where + " ORDER BY a.id DESC",
            params,
        ).fetchall()
        return [self._anomaly_dict(row) for row in rows]

    def acknowledge_anomaly(self, anomaly_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            anomaly = self._anomaly(connection, anomaly_id)
            if anomaly["status"] != "open":
                raise ConflictError("只有待处理异常可以认领", context={"status": anomaly["status"]})
            connection.execute("UPDATE ash_anomalies SET status='acknowledged',acknowledged_at=? WHERE id=? AND status='open'", (now, anomaly_id))
            return self._anomaly_dict(connection.execute("SELECT a.*,n.code AS temple_code,c.container_code FROM ash_anomalies a JOIN temple_sites n ON n.id=a.temple_id LEFT JOIN ash_containers c ON c.id=a.container_id WHERE a.id=?", (anomaly_id,)).fetchone())

    def resolve_anomaly(self, anomaly_id: int, actor: str, resolution: str, new_storage_deadline: str | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            anomaly = self._anomaly(connection, anomaly_id)
            if anomaly["status"] == "resolved":
                raise ConflictError("异常已处理完毕")
            deadline = None
            if new_storage_deadline:
                try:
                    deadline = to_storage(from_storage(new_storage_deadline))
                except ValueError as exc:
                    raise ValidationError("新的暂存期限格式不正确") from exc
                if anomaly["anomaly_type"] != "storage_overdue" or anomaly["container_id"] is None:
                    raise ValidationError("只有暂存超期异常可以顺延暂存期限")
                connection.execute("UPDATE ash_containers SET storage_deadline=?,version=version+1,updated_at=? WHERE id=?", (deadline, now, anomaly["container_id"]))
            connection.execute("UPDATE ash_anomalies SET status='resolved',resolved_at=?,resolved_by=?,resolution=? WHERE id=? AND status IN ('open','acknowledged')", (now, actor, resolution, anomaly_id))
            return self._anomaly_dict(connection.execute("SELECT a.*,n.code AS temple_code,c.container_code FROM ash_anomalies a JOIN temple_sites n ON n.id=a.temple_id LEFT JOIN ash_containers c ON c.id=a.container_id WHERE a.id=?", (anomaly_id,)).fetchone())

    # ------------------------------------------------------------------
    # 查询：容器详情、重量平衡、处置反向追溯、日产生量核对
    # ------------------------------------------------------------------
    def list_containers(self, temple_code: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            clauses.append("n.code=?")
            params.append(temple_code)
        if status:
            clauses.append("a.status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT a.*,n.code AS temple_code FROM ash_containers a JOIN temple_sites n ON n.id=a.temple_id" + where + " ORDER BY a.id DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def container_detail(self, container_code: str) -> dict[str, Any]:
        container = self._container_by_code(self.connection, container_code)
        return self._container_detail(self.connection, container["id"])

    def container_balance(self, container_code: str) -> dict[str, Any]:
        container = self._container_by_code(self.connection, container_code)
        return self._balance(self.connection, container)

    def disposal_trace(self, disposal_code: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT d.*,a.container_code FROM ash_disposals d JOIN ash_containers a ON a.id=d.container_id WHERE d.disposal_code=?",
            (disposal_code,),
        ).fetchone()
        if row is None:
            raise NotFoundError("最终处置记录不存在")
        disposal = dict(row)
        container = self.connection.execute("SELECT * FROM ash_containers WHERE id=?", (disposal["container_id"],)).fetchone()
        contents = self.connection.execute(
            "SELECT c.*,b.batch_code,b.sealed_by,b.sealed_at,b.weight_grams AS batch_weight_grams,s.code AS hall_code,s.name AS hall_name "
            "FROM ash_container_contents c JOIN ash_batches b ON b.id=c.batch_id JOIN worship_halls s ON s.id=b.hall_id "
            "WHERE c.disposal_id=? ORDER BY c.id",
            (disposal["id"],),
        ).fetchall()
        sources = []
        for content in contents:
            path = [dict(item) for item in self._content_lineage(self.connection, content)]
            sources.append({
                "batch_code": content["batch_code"],
                "hall_code": content["hall_code"],
                "hall_name": content["hall_name"],
                "weight_grams": content["weight_grams"],
                "sealed_by": content["sealed_by"],
                "sealed_at": content["sealed_at"],
                "container_path": path,
            })
        handovers = [dict(item) for item in self.connection.execute(
            "SELECT h.*,a.container_code FROM ash_handovers h JOIN ash_containers a ON a.id=h.container_id "
            "WHERE h.container_id IN (SELECT DISTINCT container_id FROM ash_container_contents WHERE batch_id IN (SELECT batch_id FROM ash_container_contents WHERE disposal_id=?)) "
            "ORDER BY h.id",
            (disposal["id"],),
        ).fetchall()]
        lineage_containers = self.connection.execute(
            "SELECT * FROM ash_containers WHERE id IN (SELECT DISTINCT container_id FROM ash_container_contents WHERE batch_id IN (SELECT batch_id FROM ash_container_contents WHERE disposal_id=?)) ORDER BY id",
            (disposal["id"],),
        ).fetchall()
        balance = self._balance(self.connection, container)
        totals = self._ledger_totals(self.connection, container["id"])
        equation = _grams(totals["seal_in"] + totals["merge_in"] + totals["merge_out"] + totals["reweigh"] + totals["loss_adjustment"] + totals["disposal_out"])
        adjustments = _grams(totals["reweigh"] + totals["loss_adjustment"])
        contents_total = _grams(sum(content["weight_grams"] for content in contents))
        disposal_balanced = _balanced(disposal["disposed_weight_grams"], contents_total + adjustments)
        chain_checks = {item["container_code"]: self._chain_continuous(self.connection, item) for item in lineage_containers}
        chain_continuous = all(chain_checks.values())
        balanced = balance["balanced"] and _balanced(equation, 0.0) and disposal_balanced and chain_continuous
        return {
            "disposal": disposal,
            "container": dict(container),
            "sources": sources,
            "handovers": handovers,
            "chain_continuous": chain_continuous,
            "chain_checks": chain_checks,
            "weight_equation": {**totals, "sum_grams": equation},
            "contents_total_grams": contents_total,
            "adjustments_grams": adjustments,
            "disposal_balanced": disposal_balanced,
            "balance": balance,
            "balanced": balanced,
        }

    def production_summary(self, temple_code: str, day: str | None = None) -> dict[str, Any]:
        temple = self._temple(temple_code)
        if day is None:
            day = to_storage(self.clock.now())[:10]
        rows = self.connection.execute(
            "SELECT s.code AS hall_code,s.name AS hall_name,COUNT(*) AS batches,COALESCE(SUM(b.weight_grams),0) AS total_grams "
            "FROM ash_batches b JOIN worship_halls s ON s.id=b.hall_id WHERE b.temple_id=? AND substr(b.sealed_at,1,10)=? "
            "GROUP BY s.id ORDER BY s.visit_order,s.id",
            (temple["id"], day),
        ).fetchall()
        halls = [dict(row) for row in rows]
        return {
            "temple_code": temple_code,
            "day": day,
            "halls": halls,
            "total_batches": sum(item["batches"] for item in halls),
            "total_grams": _grams(sum(item["total_grams"] for item in halls)),
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _temple(self, code: str) -> sqlite3.Row:
        row = self.repository.temple_by_code(code)
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    @staticmethod
    def _container_by_code(connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM ash_containers WHERE container_code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("香灰容器不存在")
        return row

    @staticmethod
    def _handover(connection: sqlite3.Connection, handover_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM ash_handovers WHERE id=?", (handover_id,)).fetchone()
        if row is None:
            raise NotFoundError("交接记录不存在")
        return row

    @staticmethod
    def _anomaly(connection: sqlite3.Connection, anomaly_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM ash_anomalies WHERE id=?", (anomaly_id,)).fetchone()
        if row is None:
            raise NotFoundError("异常记录不存在")
        return row

    @staticmethod
    def _operation_by_key(connection: sqlite3.Connection, operation_key: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM ash_operations WHERE operation_key=?", (operation_key,)).fetchone()

    @staticmethod
    def _insert_operation(connection: sqlite3.Connection, operation_key: str, operation_type: str, temple_id: int, loss_grams: float, actor: str, note: str, digest: str, now: str) -> int:
        cursor = connection.execute(
            "INSERT INTO ash_operations(operation_key,operation_type,temple_id,loss_grams,actor,note,payload_digest,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (operation_key, operation_type, temple_id, loss_grams, actor, note, digest, now),
        )
        return cursor.lastrowid

    def _replay(self, connection: sqlite3.Connection, operation: sqlite3.Row, digest: str, builder) -> dict[str, Any]:
        if operation["payload_digest"] != digest:
            raise ConflictError("相同 operation_key 对应了不同操作内容")
        result = builder(connection, operation["id"])
        result["replay"] = True
        return result

    def _seal_result(self, connection: sqlite3.Connection, operation_id: int) -> dict[str, Any]:
        content = connection.execute("SELECT * FROM ash_container_contents WHERE operation_id=?", (operation_id,)).fetchone()
        batch = connection.execute(
            "SELECT b.*,s.code AS hall_code,s.name AS hall_name FROM ash_batches b JOIN worship_halls s ON s.id=b.hall_id WHERE b.id=?",
            (content["batch_id"],),
        ).fetchone()
        return {"operation_id": operation_id, "container": self._container_detail(connection, content["container_id"]), "batch": dict(batch)}

    def _merge_result(self, connection: sqlite3.Connection, operation_id: int) -> dict[str, Any]:
        target_content = connection.execute("SELECT * FROM ash_container_contents WHERE operation_id=? ORDER BY id LIMIT 1", (operation_id,)).fetchone()
        sources = connection.execute("SELECT DISTINCT container_id FROM ash_weight_ledger WHERE operation_id=? AND entry_type='merge_out' ORDER BY container_id", (operation_id,)).fetchall()
        return {
            "operation_id": operation_id,
            "container": self._container_detail(connection, target_content["container_id"]),
            "merged_source_container_ids": [row["container_id"] for row in sources],
        }

    def _container_op_result(self, connection: sqlite3.Connection, operation_id: int) -> dict[str, Any]:
        entry = connection.execute("SELECT * FROM ash_weight_ledger WHERE operation_id=? ORDER BY id LIMIT 1", (operation_id,)).fetchone()
        return {"operation_id": operation_id, "container": self._container_detail(connection, entry["container_id"]), "ledger_entry": dict(entry)}

    def _disposal_result(self, connection: sqlite3.Connection, operation_id: int) -> dict[str, Any]:
        disposal = connection.execute("SELECT * FROM ash_disposals WHERE operation_id=?", (operation_id,)).fetchone()
        return {"operation_id": operation_id, "disposal": dict(disposal), "container": self._container_detail(connection, disposal["container_id"])}

    def _container_detail(self, connection: sqlite3.Connection, container_id: int) -> dict[str, Any]:
        row = connection.execute("SELECT a.*,n.code AS temple_code FROM ash_containers a JOIN temple_sites n ON n.id=a.temple_id WHERE a.id=?", (container_id,)).fetchone()
        result = dict(row)
        result["contents"] = [dict(item) for item in connection.execute(
            "SELECT c.*,b.batch_code,s.code AS hall_code,s.name AS hall_name FROM ash_container_contents c JOIN ash_batches b ON b.id=c.batch_id JOIN worship_halls s ON s.id=b.hall_id WHERE c.container_id=? ORDER BY c.id",
            (container_id,),
        ).fetchall()]
        result["handovers"] = [dict(item) for item in connection.execute("SELECT * FROM ash_handovers WHERE container_id=? ORDER BY id", (container_id,)).fetchall()]
        result["ledger"] = [dict(item) for item in connection.execute("SELECT * FROM ash_weight_ledger WHERE container_id=? ORDER BY id", (container_id,)).fetchall()]
        events = connection.execute("SELECT * FROM ash_container_events WHERE container_id=? ORDER BY id", (container_id,)).fetchall()
        result["events"] = [self._event_dict(event) for event in events]
        return result

    def _handover_detail(self, connection: sqlite3.Connection, handover_id: int, replay: bool = False) -> dict[str, Any]:
        row = connection.execute(
            "SELECT h.*,a.container_code,n.code AS temple_code FROM ash_handovers h JOIN ash_containers a ON a.id=h.container_id JOIN temple_sites n ON n.id=h.temple_id WHERE h.id=?",
            (handover_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("交接记录不存在")
        result = dict(row)
        if replay:
            result["replay"] = True
        return result

    def _balance(self, connection: sqlite3.Connection, container: sqlite3.Row) -> dict[str, Any]:
        entries = connection.execute("SELECT * FROM ash_weight_ledger WHERE container_id=? ORDER BY id", (container["id"],)).fetchall()
        running = 0.0
        nodes = []
        discrepancies = []
        for entry in entries:
            running = _grams(running + entry["delta_grams"])
            node_balanced = _balanced(running, entry["resulting_weight_grams"])
            nodes.append({
                "ledger_id": entry["id"],
                "entry_type": entry["entry_type"],
                "operation_id": entry["operation_id"],
                "delta_grams": entry["delta_grams"],
                "recorded_weight_grams": entry["resulting_weight_grams"],
                "recomputed_weight_grams": running,
                "balanced": node_balanced,
            })
            if not node_balanced:
                discrepancies.append(f"流水 {entry['id']} 记录重量 {entry['resulting_weight_grams']} 与重算重量 {running} 不一致")
        if not _balanced(running, container["current_weight_grams"]):
            discrepancies.append(f"流水合计 {running} 与容器当前重量 {container['current_weight_grams']} 不一致")
        operations = connection.execute(
            "SELECT o.id,o.operation_type,o.loss_grams,(SELECT COALESCE(SUM(l.delta_grams),0) FROM ash_weight_ledger l WHERE l.operation_id=o.id) AS delta_sum "
            "FROM ash_operations o WHERE o.id IN (SELECT DISTINCT operation_id FROM ash_weight_ledger WHERE container_id=?) ORDER BY o.id",
            (container["id"],),
        ).fetchall()
        operation_checks = []
        for operation in operations:
            conserved = self._operation_conserved(connection, operation)
            operation_checks.append({"operation_id": operation["id"], "operation_type": operation["operation_type"], "delta_sum_grams": _grams(operation["delta_sum"]), "loss_grams": operation["loss_grams"], "conserved": conserved})
            if not conserved:
                discrepancies.append(f"操作 {operation['id']}（{operation['operation_type']}）不守恒")
        attributed = None
        if container["status"] in TRANSFERABLE_STATES + ("in_transit",):
            contents_sum = connection.execute("SELECT COALESCE(SUM(weight_grams),0) FROM ash_container_contents WHERE container_id=? AND status='active'", (container["id"],)).fetchone()[0]
            adjustments = connection.execute("SELECT COALESCE(SUM(delta_grams),0) FROM ash_weight_ledger WHERE container_id=? AND entry_type IN ('reweigh','loss_adjustment')", (container["id"],)).fetchone()[0]
            attributed = {"contents_sum_grams": _grams(contents_sum), "adjustments_grams": _grams(adjustments), "matches": _balanced(contents_sum + adjustments, container["current_weight_grams"])}
            if not attributed["matches"]:
                discrepancies.append("在库批次重量加调整量与容器当前重量不一致")
        return {
            "container_code": container["container_code"],
            "status": container["status"],
            "current_weight_grams": container["current_weight_grams"],
            "ledger_sum_grams": running,
            "nodes": nodes,
            "operations": operation_checks,
            "attributed": attributed,
            "discrepancies": discrepancies,
            "balanced": not discrepancies,
        }

    @staticmethod
    def _operation_conserved(connection: sqlite3.Connection, operation: sqlite3.Row) -> bool:
        delta_sum = _grams(operation["delta_sum"])
        loss = _grams(operation["loss_grams"])
        if operation["operation_type"] in ("merge", "loss_adjustment"):
            return _balanced(delta_sum + loss, 0.0)
        if operation["operation_type"] == "seal":
            content = connection.execute("SELECT weight_grams FROM ash_container_contents WHERE operation_id=?", (operation["id"],)).fetchone()
            return content is not None and _balanced(delta_sum, content["weight_grams"])
        if operation["operation_type"] == "disposal":
            disposal = connection.execute("SELECT disposed_weight_grams FROM ash_disposals WHERE operation_id=?", (operation["id"],)).fetchone()
            return disposal is not None and _balanced(delta_sum + disposal["disposed_weight_grams"], 0.0)
        return True

    @staticmethod
    def _ledger_totals(connection: sqlite3.Connection, container_id: int) -> dict[str, float]:
        rows = connection.execute("SELECT entry_type,COALESCE(SUM(delta_grams),0) AS total FROM ash_weight_ledger WHERE container_id=? GROUP BY entry_type", (container_id,)).fetchall()
        totals = {entry_type: 0.0 for entry_type in ("seal_in", "merge_in", "merge_out", "reweigh", "loss_adjustment", "disposal_out")}
        for row in rows:
            totals[row["entry_type"]] = _grams(row["total"])
        return totals

    @staticmethod
    def _content_lineage(connection: sqlite3.Connection, content: sqlite3.Row) -> list[dict[str, Any]]:
        path = []
        current = content
        while current is not None:
            container = connection.execute("SELECT container_code FROM ash_containers WHERE id=?", (current["container_id"],)).fetchone()
            path.append({"container_code": container["container_code"], "content_id": current["id"], "weight_grams": current["weight_grams"]})
            if current["origin_content_id"] is None:
                break
            current = connection.execute("SELECT * FROM ash_container_contents WHERE id=?", (current["origin_content_id"],)).fetchone()
        return path

    @staticmethod
    def _expected_custodian(connection: sqlite3.Connection, container: sqlite3.Row) -> str:
        latest = connection.execute("SELECT to_custodian FROM ash_handovers WHERE container_id=? AND status='confirmed' ORDER BY id DESC LIMIT 1", (container["id"],)).fetchone()
        return latest["to_custodian"] if latest else container["sealed_by"]

    @staticmethod
    def _chain_continuous(connection: sqlite3.Connection, container: sqlite3.Row) -> bool:
        expected = container["sealed_by"]
        handovers = connection.execute("SELECT * FROM ash_handovers WHERE container_id=? ORDER BY id", (container["id"],)).fetchall()
        for handover in handovers:
            if handover["status"] != "confirmed":
                continue
            if handover["from_custodian"] != expected:
                return False
            expected = handover["to_custodian"]
        return expected == container["current_custodian"]

    def _open_anomaly(self, connection: sqlite3.Connection, anomaly_type: str, temple_id: int, container_id: int | None, handover_id: int | None, detail: dict[str, Any], now: str) -> int | None:
        dedupe_key = f"{anomaly_type}:{container_id or 0}:{handover_id or 0}"
        try:
            cursor = connection.execute(
                "INSERT INTO ash_anomalies(dedupe_key,anomaly_type,temple_id,container_id,handover_id,detail_json,opened_at) VALUES(?,?,?,?,?,?,?)",
                (dedupe_key, anomaly_type, temple_id, container_id, handover_id, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    @staticmethod
    def _anomaly_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["detail"] = json.loads(result.pop("detail_json"))
        return result

    @staticmethod
    def _ledger(connection: sqlite3.Connection, operation_id: int, container_id: int, content_id: int | None, batch_id: int | None, entry_type: str, delta: float, resulting: float, actor: str, note: str, now: str) -> None:
        connection.execute(
            "INSERT INTO ash_weight_ledger(operation_id,container_id,content_id,batch_id,entry_type,delta_grams,resulting_weight_grams,actor,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (operation_id, container_id, content_id, batch_id, entry_type, _grams(delta), _grams(resulting), actor, note, now),
        )

    @staticmethod
    def _event(connection: sqlite3.Connection, container_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO ash_container_events(container_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (container_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["detail"] = json.loads(result.pop("detail_json"))
        return result
