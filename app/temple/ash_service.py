from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.temple.ash_schema import ensure_ash_schema
from app.temple.repository import TempleRepository

# 复核秤允许的最小分度差（克），超过即视为重量异常需要处理
SCALE_EPSILON_GRAMS = 5
# 交接单停留在待确认状态超过该小时数，判定为交接断链
DEFAULT_HANDOVER_STALL_HOURS = 24

# 容器状态机：每次交接事件前后允许的状态对，用于写入校验与链式复核
ALLOWED_STATUS_FLOW: dict[str, set[tuple[str, str]]] = {
    "register": {("", "empty")},
    "pack": {("empty", "sealed"), ("empty", "stored")},
    "transfer_initiate": {("sealed", "in_transit"), ("stored", "in_transit")},
    "transfer_confirm": {("in_transit", "sealed"), ("in_transit", "stored")},
    "transfer_cancel": {("in_transit", "sealed"), ("in_transit", "stored")},
    "merge_split": {("sealed", "empty"), ("stored", "empty")},
    "merge_pack": {("empty", "sealed"), ("empty", "stored")},
    "reweigh": {("sealed", "sealed"), ("stored", "stored"), ("in_transit", "in_transit")},
    "dispose": {("sealed", "disposed"), ("stored", "disposed")},
}


class AshCustodyService:
    """香灰批次与容器交接链服务。

    所有写操作都在 BEGIN IMMEDIATE 事务内完成，并同步写入守恒重量流水，
    因此相同凭据重放与并发领取只会成功一次，任意时刻都可以用流水重建重量平衡。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_ash_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = TempleRepository(self.connection)

    # ------------------------------------------------------------------ 容器

    def register_container(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO ash_containers(temple_id,code,capacity_grams,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (temple["id"], payload["code"], payload["capacity_grams"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("容器编码已存在") from exc
            self._event(connection, temple["id"], cursor.lastrowid, "register", payload["actor"], "", "", "empty", 0, {}, now)
            return self.container_detail_by_id(cursor.lastrowid, connection)

    def list_containers(self, temple_code: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            clauses.append("c.temple_id=?")
            params.append(self._temple(temple_code)["id"])
        if status:
            clauses.append("c.status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT c.*,n.code AS temple_code,b.batch_code AS current_batch_code "
            "FROM ash_containers c JOIN temple_sites n ON n.id=c.temple_id "
            "LEFT JOIN ash_batches b ON b.id=c.current_batch_id" + where + " ORDER BY c.id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def container_detail(self, container_code: str) -> dict[str, Any]:
        return self.container_detail_by_id(self._container(None, container_code)["id"])

    def container_detail_by_id(self, container_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT c.*,n.code AS temple_code,n.name AS temple_name,b.batch_code AS current_batch_code "
            "FROM ash_containers c JOIN temple_sites n ON n.id=c.temple_id "
            "LEFT JOIN ash_batches b ON b.id=c.current_batch_id WHERE c.id=?",
            (container_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("香灰容器不存在")
        result = dict(row)
        result["events"] = self._events(connection, container_id)
        result["ledger"] = self._ledger(connection, container_id)
        result["ledger_balance_grams"] = sum(item["signed_weight_grams"] for item in result["ledger"])
        result["weight_balanced"] = result["ledger_balance_grams"] == result["current_weight_grams"]
        result["chain_continuous"] = self._chain_continuous(result["events"])
        return result

    # ------------------------------------------------------------------ 封装

    def collect_and_pack(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        hall = self.repository.hall_by_code(temple["id"], payload["hall_code"])
        if hall is None:
            raise NotFoundError("来源殿堂不存在")
        weight = int(payload["weight_grams"])
        if weight <= 0:
            raise ValidationError("封装重量必须大于零")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            container = self._locked_container(connection, temple["id"], payload["container_code"])
            self._assert_flow(container["status"], "sealed" if not payload.get("retention_hours") else "stored", "pack")
            if weight > container["capacity_grams"]:
                raise ValidationError("香灰重量超过容器容量")
            batch_code = payload.get("batch_code") or self._next_business_code(connection, temple["id"], "ASH-B", "ash_batches", "batch_code")
            batch = connection.execute(
                "INSERT INTO ash_batches(batch_code,temple_id,hall_id,origin_type,declared_weight_grams,weight_grams,created_by,created_at) "
                "VALUES(?,?,?,'collection',?,?,?,?)",
                (batch_code, temple["id"], hall["id"], weight, weight, payload["handler"], now),
            )
            stored_until = self._stored_until(now, payload.get("retention_hours"))
            event = self._event(
                connection, temple["id"], container["id"], "pack", payload["handler"], "",
                container["status"], "stored" if stored_until else "sealed", weight,
                {"batch_code": batch_code, "hall_code": hall["code"]}, now,
            )
            self._ledger_row(connection, temple["id"], event["id"], container["id"], batch.lastrowid, "in", weight, weight, "collection", payload["handler"], now)
            self._update_container(
                connection, container["id"],
                status="stored" if stored_until else "sealed",
                batch_id=batch.lastrowid, weight=weight,
                custodian=payload["handler"], seal_code=payload.get("seal_code", ""),
                location=payload.get("location") or hall["name"],
                stored_at=now if stored_until else None, stored_until=stored_until,
            )
            return self.container_detail_by_id(container["id"], connection)

    # ------------------------------------------------------------------ 交接

    def initiate_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            # 相同交接凭据重放：直接返回原交接单，不会产生第二次转移
            if payload.get("idempotency_key"):
                existing = connection.execute(
                    "SELECT * FROM ash_handovers WHERE temple_id=? AND idempotency_key=?",
                    (temple["id"], payload["idempotency_key"]),
                ).fetchone()
                if existing is not None:
                    detail = self.handover_detail_by_id(existing["id"], connection)
                    detail["replayed"] = True
                    return detail
            if connection.execute("SELECT 1 FROM ash_handovers WHERE handover_code=?", (payload["handover_code"],)).fetchone():
                raise ConflictError("交接凭据编号已存在")
            container = self._locked_container(connection, temple["id"], payload["container_code"])
            self._assert_flow(container["status"], "in_transit", "transfer_initiate")
            cursor = connection.execute(
                "INSERT INTO ash_handovers(handover_code,idempotency_key,temple_id,container_id,from_party,to_party,to_location,"
                "retention_hours,prior_status,weight_snapshot_grams,initiated_by,initiated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["handover_code"], payload.get("idempotency_key"), temple["id"], container["id"],
                    payload["from_party"], payload["to_party"], payload["to_location"], payload.get("retention_hours"),
                    container["status"], container["current_weight_grams"], payload["actor"], now,
                ),
            )
            self._event(
                connection, temple["id"], container["id"], "transfer_initiate", payload["actor"], payload["to_party"],
                container["status"], "in_transit", container["current_weight_grams"],
                {"handover_code": payload["handover_code"], "to_location": payload["to_location"]}, now,
            )
            connection.execute(
                "UPDATE ash_containers SET status='in_transit',location=?,version=version+1,updated_at=? WHERE id=?",
                (f"in_transit:{payload['to_location']}", now, container["id"]),
            )
            return self.handover_detail_by_id(cursor.lastrowid, connection)

    def confirm_transfer(self, handover_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        mismatch: dict[str, Any] | None = None
        with transaction(immediate=True) as connection:
            handover = self._locked_handover(connection, handover_code)
            if handover["status"] != "pending":
                raise ConflictError("交接凭据已处理，不能重复领取")
            container = self._locked_container(connection, handover["temple_id"], None, handover["container_id"])
            if container["status"] != "in_transit":
                raise ConflictError("容器不处于运输中，交接链已断裂")
            measured = payload.get("measured_weight_grams")
            if measured is not None and int(measured) != handover["weight_snapshot_grams"]:
                self._open_anomaly(
                    connection, handover["temple_id"], "receipt_weight_mismatch", "major", now,
                    container_id=container["id"], handover_id=handover["id"],
                    detail={"expected_grams": handover["weight_snapshot_grams"], "measured_grams": int(measured)},
                )
                mismatch = {"expected_grams": handover["weight_snapshot_grams"], "measured_grams": int(measured)}
            else:
                target_status = "stored" if handover["retention_hours"] else "sealed"
                self._assert_flow("in_transit", target_status, "transfer_confirm")
                stored_until = self._stored_until(now, handover["retention_hours"])
                connection.execute(
                    "UPDATE ash_handovers SET status='confirmed',confirmed_by=?,confirmed_at=?,version=version+1 WHERE id=?",
                    (payload["confirmed_by"], now, handover["id"]),
                )
                self._event(
                    connection, handover["temple_id"], container["id"], "transfer_confirm", payload["confirmed_by"],
                    handover["from_party"], "in_transit", target_status, container["current_weight_grams"],
                    {"handover_code": handover["handover_code"], "to_location": handover["to_location"]}, now,
                )
                self._update_container(
                    connection, container["id"], status=target_status,
                    custodian=handover["to_party"], location=handover["to_location"],
                    stored_at=now if stored_until else None, stored_until=stored_until,
                )
                result = self.handover_detail_by_id(handover["id"], connection)
        if mismatch is not None:
            raise ConflictError("接收重量与交接快照不一致，已挂起交接并生成异常", context=mismatch)
        return result

    def cancel_transfer(self, handover_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            handover = self._locked_handover(connection, handover_code)
            if handover["status"] != "pending":
                raise ConflictError("只有待确认交接可以撤销")
            container = self._locked_container(connection, handover["temple_id"], None, handover["container_id"])
            if container["status"] != "in_transit":
                raise ConflictError("容器不处于运输中，无法撤销交接")
            target_status = handover["prior_status"]
            self._assert_flow("in_transit", target_status, "transfer_cancel")
            connection.execute(
                "UPDATE ash_handovers SET status='cancelled',cancel_reason=?,confirmed_by=?,confirmed_at=?,version=version+1 WHERE id=?",
                (payload["reason"], payload["actor"], now, handover["id"]),
            )
            self._event(
                connection, handover["temple_id"], container["id"], "transfer_cancel", payload["actor"],
                handover["to_party"], "in_transit", target_status, container["current_weight_grams"],
                {"handover_code": handover["handover_code"], "reason": payload["reason"]}, now,
            )
            self._update_container(connection, container["id"], status=target_status, custodian=handover["from_party"])
            return self.handover_detail_by_id(handover["id"], connection)

    def handover_detail(self, handover_code: str) -> dict[str, Any]:
        return self.handover_detail_by_id(self._handover(handover_code)["id"])

    def handover_detail_by_id(self, handover_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT h.*,c.code AS container_code,n.code AS temple_code FROM ash_handovers h "
            "JOIN ash_containers c ON c.id=h.container_id JOIN temple_sites n ON n.id=h.temple_id WHERE h.id=?",
            (handover_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("交接凭据不存在")
        return dict(row)

    # ------------------------------------------------------------------ 合并

    def merge_batches(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        source_codes = payload["source_container_codes"]
        if not source_codes:
            raise ValidationError("拆包合并至少选择一个来源容器")
        if len(source_codes) != len(set(source_codes)):
            raise ValidationError("来源容器不能重复")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            destination = self._locked_container(connection, temple["id"], payload["container_code"])
            self._assert_flow(destination["status"], "stored" if payload.get("retention_hours") else "sealed", "merge_pack")
            sources: list[sqlite3.Row] = []
            input_total = 0
            for code in source_codes:
                source = self._locked_container(connection, temple["id"], code)
                if source["id"] == destination["id"]:
                    raise ValidationError("合并目标容器不能同时是来源容器")
                self._assert_flow(source["status"], "empty", "merge_split")
                if source["current_batch_id"] is None:
                    raise ValidationError(f"来源容器 {code} 内没有香灰批次")
                sources.append(source)
                input_total += source["current_weight_grams"]
            output_weight = int(payload.get("weight_grams") or input_total)
            if output_weight <= 0:
                raise ValidationError("合并后重量必须大于零")
            if output_weight > destination["capacity_grams"]:
                raise ValidationError("合并香灰重量超过目标容器容量")
            loss = input_total - output_weight
            if loss != 0 and not payload.get("loss_reason"):
                raise ValidationError("合并出现损耗或增重时必须填写损耗调整原因")
            batch_code = payload.get("batch_code") or self._next_business_code(connection, temple["id"], "ASH-M", "ash_batches", "batch_code")
            batch = connection.execute(
                "INSERT INTO ash_batches(batch_code,temple_id,hall_id,origin_type,declared_weight_grams,weight_grams,created_by,created_at) "
                "VALUES(?,?,NULL,'merge',?,?,?,?)",
                (batch_code, temple["id"], input_total, output_weight, payload["actor"], now),
            )
            # 逐个拆包：来源容器重量流出，守恒流水闭合到零
            for source in sources:
                split_event = self._event(
                    connection, temple["id"], source["id"], "merge_split", payload["actor"], "",
                    source["status"], "empty", source["current_weight_grams"],
                    {"into_batch_code": batch_code}, now,
                )
                self._ledger_row(
                    connection, temple["id"], split_event["id"], source["id"], source["current_batch_id"],
                    "out", -source["current_weight_grams"], 0, f"merge:{batch_code}", payload["actor"], now,
                )
                connection.execute(
                    "INSERT INTO ash_batch_components(merge_batch_id,component_batch_id,weight_grams) VALUES(?,?,?)",
                    (batch.lastrowid, source["current_batch_id"], source["current_weight_grams"]),
                )
                self._update_container(connection, source["id"], status="empty", batch_id=None, weight=0, custodian="", location="",
                                       stored_at=None, stored_until=None, seal_code="")
            # 合并装袋：投入重量先入流水
            pack_event = self._event(
                connection, temple["id"], destination["id"], "merge_pack", payload["actor"], "",
                "empty", "stored" if payload.get("retention_hours") else "sealed", input_total,
                {"batch_code": batch_code, "source_container_codes": source_codes}, now,
            )
            balance = input_total
            self._ledger_row(connection, temple["id"], pack_event["id"], destination["id"], batch.lastrowid, "in", input_total, balance, "merge", payload["actor"], now)
            # 损耗或增重以调整单独立账，保证来源合计与最终重量守恒
            if loss != 0:
                adjustment = output_weight - input_total
                balance += adjustment
                reweigh_event = self._event(
                    connection, temple["id"], destination["id"], "reweigh", payload["actor"], "",
                    "stored" if payload.get("retention_hours") else "sealed",
                    "stored" if payload.get("retention_hours") else "sealed",
                    adjustment, {"batch_code": batch_code, "reason": payload["loss_reason"]}, now,
                )
                self._ledger_row(connection, temple["id"], reweigh_event["id"], destination["id"], batch.lastrowid, "adjust", adjustment, balance, payload["loss_reason"], payload["actor"], now)
            connection.execute("UPDATE ash_batches SET merge_node_event_id=? WHERE id=?", (pack_event["id"], batch.lastrowid))
            stored_until = self._stored_until(now, payload.get("retention_hours"))
            self._update_container(
                connection, destination["id"], status="stored" if stored_until else "sealed",
                batch_id=batch.lastrowid, weight=output_weight, custodian=payload["actor"],
                seal_code=payload.get("seal_code", ""), location=payload.get("location") or "合并暂存",
                stored_at=now if stored_until else None, stored_until=stored_until,
            )
            result = self.container_detail_by_id(destination["id"], connection)
            result["merge"] = {"batch_code": batch_code, "input_total_grams": input_total, "output_grams": output_weight, "loss_adjustment_grams": loss}
            return result

    # ------------------------------------------------------------------ 复核

    def reweigh_container(self, container_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            container = self._container_row(connection, None, container_code)
            if container["current_batch_id"] is None:
                raise ValidationError("空容器不能称重复核")
            self._assert_flow(container["status"], container["status"], "reweigh")
            measured = int(payload["measured_weight_grams"])
            if measured < 0:
                raise ValidationError("复核重量不能为负")
            delta = measured - container["current_weight_grams"]
            event = self._event(
                connection, container["temple_id"], container["id"], "reweigh", payload["actor"], "",
                container["status"], container["status"], delta,
                {"previous_grams": container["current_weight_grams"], "measured_grams": measured, "reason": payload["reason"]}, now,
            )
            if delta != 0:
                self._ledger_row(
                    connection, container["temple_id"], event["id"], container["id"], container["current_batch_id"],
                    "adjust", delta, measured, payload["reason"], payload["actor"], now,
                )
                connection.execute("UPDATE ash_batches SET weight_grams=? WHERE id=?", (measured, container["current_batch_id"]))
            connection.execute(
                "UPDATE ash_containers SET current_weight_grams=?,version=version+1,updated_at=? WHERE id=?",
                (measured, now, container["id"]),
            )
            if abs(delta) > SCALE_EPSILON_GRAMS:
                severity = "critical" if abs(delta) >= 1000 or abs(delta) * 10 >= max(measured, 1) else ("major" if abs(delta) >= 100 else "minor")
                self._open_anomaly(
                    connection, container["temple_id"], "weight_verification", severity, now,
                    container_id=container["id"],
                    detail={"previous_grams": container["current_weight_grams"], "measured_grams": measured, "delta_grams": delta, "reason": payload["reason"]},
                )
            return self.container_detail_by_id(container["id"], connection)

    # ------------------------------------------------------------------ 处置

    def dispose_container(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        now = to_storage(self.clock.now())
        error: ConflictError | None = None
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM ash_disposals WHERE disposal_code=?", (payload["disposal_code"],)).fetchone():
                raise ConflictError("最终处置凭据编号已存在")
            container = self._locked_container(connection, temple["id"], payload["container_code"])
            if container["current_batch_id"] is None:
                raise ValidationError("空容器不能登记最终处置")
            if container["status"] == "in_transit":
                self._open_anomaly(
                    connection, temple["id"], "handover_stalled", "critical", now,
                    container_id=container["id"],
                    detail={"reason": "运输中容器被申请最终处置，交接未确认"},
                )
                error = ConflictError("容器尚在运输交接中，不能最终处置，请先处理交接异常")
            else:
                self._assert_flow(container["status"], "disposed", "dispose")
                measured = payload.get("measured_weight_grams")
                if measured is not None and int(measured) != container["current_weight_grams"]:
                    self._open_anomaly(
                        connection, temple["id"], "receipt_weight_mismatch", "major", now, container_id=container["id"],
                        detail={"expected_grams": container["current_weight_grams"], "measured_grams": int(measured), "stage": "disposal"},
                    )
                    error = ConflictError("处置称重与容器重量不一致，已生成异常", context={"expected_grams": container["current_weight_grams"], "measured_grams": int(measured)})
                else:
                    event = self._event(
                        connection, temple["id"], container["id"], "dispose", payload["actor"], payload.get("witness", ""),
                        container["status"], "disposed", container["current_weight_grams"],
                        {"disposal_code": payload["disposal_code"], "method": payload["method"]}, now,
                    )
                    self._ledger_row(
                        connection, temple["id"], event["id"], container["id"], container["current_batch_id"],
                        "out", -container["current_weight_grams"], 0, f"dispose:{payload['disposal_code']}", payload["actor"], now,
                    )
                    cursor = connection.execute(
                        "INSERT INTO ash_disposals(disposal_code,temple_id,container_id,batch_id,weight_grams,method,actor,witness,node_event_id,detail_json,disposed_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            payload["disposal_code"], temple["id"], container["id"], container["current_batch_id"],
                            container["current_weight_grams"], payload["method"], payload["actor"], payload.get("witness", ""),
                            event["id"], json.dumps(payload.get("detail", {}), ensure_ascii=False, sort_keys=True), now,
                        ),
                    )
                    self._update_container(connection, container["id"], status="disposed", weight=0, custodian="", location=payload["method"],
                                           stored_at=None, stored_until=None)
                    result = self.disposal_trace_by_id(cursor.lastrowid, connection)
        if error is not None:
            raise error
        return result

    def disposal_trace(self, disposal_code: str) -> dict[str, Any]:
        return self.disposal_trace_by_id(self._disposal(disposal_code)["id"])

    def disposal_trace_by_id(self, disposal_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        disposal = connection.execute(
            "SELECT d.*,c.code AS container_code,c.capacity_grams,n.code AS temple_code,b.batch_code "
            "FROM ash_disposals d JOIN ash_containers c ON c.id=d.container_id "
            "JOIN temple_sites n ON n.id=d.temple_id JOIN ash_batches b ON b.id=d.batch_id WHERE d.id=?",
            (disposal_id,),
        ).fetchone()
        if disposal is None:
            raise NotFoundError("最终处置记录不存在")
        disposal = dict(disposal)
        tree = self._batch_tree(connection, disposal["batch_id"])
        nodes = self._node_verifications(connection, tree)
        sources = self._source_aggregation(connection, tree)
        container_ids = {row["container_id"] for row in connection.execute(
            "SELECT DISTINCT container_id FROM ash_weight_ledger WHERE batch_id IN (%s)" % ",".join("?" * len(tree["all"])),
            tree["all"],
        ).fetchall()} if tree["all"] else set()
        container_ids.add(disposal["container_id"])
        containers = [self.container_detail_by_id(cid, connection) for cid in sorted(container_ids)]
        leaf_total = sum(item["declared_weight_grams"] for item in nodes if item["origin_type"] == "collection")
        adjustments_total = sum(item["adjustment_grams"] for item in nodes)
        disposal["detail"] = json.loads(disposal.pop("detail_json"))
        return {
            "disposal": disposal,
            "nodes": nodes,
            "sources": sources,
            "containers": containers,
            "weight_check": {
                "leaf_total_grams": leaf_total,
                "adjustments_grams": adjustments_total,
                "expected_grams": leaf_total + adjustments_total,
                "disposed_grams": disposal["weight_grams"],
                "balanced": leaf_total + adjustments_total == disposal["weight_grams"],
            },
            "chain_continuous": all(item["chain_continuous"] for item in containers),
        }

    # ------------------------------------------------------------------ 批次追溯

    def batch_trace(self, batch_code: str) -> dict[str, Any]:
        connection = self.connection
        batch = connection.execute("SELECT * FROM ash_batches WHERE batch_code=?", (batch_code,)).fetchone()
        if batch is None:
            raise NotFoundError("香灰批次不存在")
        batch = dict(batch)
        tree = self._batch_tree(connection, batch["id"])
        nodes = self._node_verifications(connection, tree)
        downstream = self._downstream_batches(connection, batch["id"])
        disposals = [dict(row) for row in connection.execute(
            "SELECT d.*,c.code AS container_code FROM ash_disposals d JOIN ash_containers c ON c.id=d.container_id "
            "WHERE d.batch_id IN (%s) ORDER BY d.id" % ",".join("?" * len(downstream)),
            downstream,
        ).fetchall()] if downstream else []
        for item in disposals:
            item["detail"] = json.loads(item.pop("detail_json"))
        return {
            "batch": batch,
            "nodes": nodes,
            "downstream_batch_ids": [bid for bid in downstream if bid != batch["id"]],
            "disposals": disposals,
        }

    # ------------------------------------------------------------------ 异常

    def scan_anomalies(self, actor: str = "ash-custody-scan", stall_hours: int = DEFAULT_HANDOVER_STALL_HOURS) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        stall_before = to_storage(now_value - timedelta(hours=stall_hours))
        opened: list[dict[str, Any]] = []
        with transaction(immediate=True) as connection:
            overdue = connection.execute(
                "SELECT * FROM ash_containers WHERE status='stored' AND stored_until IS NOT NULL AND stored_until<=? ORDER BY id",
                (now,),
            ).fetchall()
            for container in overdue:
                anomaly = self._open_anomaly(
                    connection, container["temple_id"], "storage_overdue", "major", now,
                    container_id=container["id"],
                    detail={"stored_until": container["stored_until"], "overdue_at": now},
                    reuse=True,
                )
                if anomaly:
                    opened.append(anomaly)
            stalled = connection.execute(
                "SELECT * FROM ash_handovers WHERE status='pending' AND initiated_at<=? ORDER BY id",
                (stall_before,),
            ).fetchall()
            for handover in stalled:
                anomaly = self._open_anomaly(
                    connection, handover["temple_id"], "handover_stalled", "critical", now,
                    container_id=handover["container_id"], handover_id=handover["id"],
                    detail={"handover_code": handover["handover_code"], "initiated_at": handover["initiated_at"]},
                    reuse=True,
                )
                if anomaly:
                    opened.append(anomaly)
        return {"opened_at": now, "opened": opened, "opened_count": len(opened)}

    def list_anomalies(self, temple_code: str | None = None, state: str | None = None, anomaly_type: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            clauses.append("a.temple_id=?")
            params.append(self._temple(temple_code)["id"])
        if state:
            clauses.append("a.state=?")
            params.append(state)
        if anomaly_type:
            clauses.append("a.anomaly_type=?")
            params.append(anomaly_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT a.*,n.code AS temple_code,c.code AS container_code,h.handover_code FROM ash_anomalies a "
            "JOIN temple_sites n ON n.id=a.temple_id LEFT JOIN ash_containers c ON c.id=a.container_id "
            "LEFT JOIN ash_handovers h ON h.id=a.handover_id" + where + " ORDER BY a.id DESC",
            params,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def acknowledge_anomaly(self, anomaly_id: int, actor: str) -> dict[str, Any]:
        return self._resolve_state(anomaly_id, actor, "acknowledged", None)

    def resolve_anomaly(self, anomaly_id: int, actor: str, resolution: str) -> dict[str, Any]:
        return self._resolve_state(anomaly_id, actor, "resolved", resolution)

    def _resolve_state(self, anomaly_id: int, actor: str, target: str, resolution: str | None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            anomaly = connection.execute("SELECT * FROM ash_anomalies WHERE id=?", (anomaly_id,)).fetchone()
            if anomaly is None:
                raise NotFoundError("异常不存在")
            if anomaly["state"] == "resolved":
                raise ConflictError("异常已处理完成")
            if target == "acknowledged" and anomaly["state"] == "acknowledged":
                raise ConflictError("异常已认领")
            connection.execute(
                "UPDATE ash_anomalies SET state=?,acknowledged_at=COALESCE(acknowledged_at,?),resolved_at=?,resolved_by=?,resolution=? WHERE id=?",
                (target, now, now if target == "resolved" else None, actor, resolution or anomaly["resolution"], anomaly_id),
            )
            return self._anomaly_dict(connection.execute("SELECT * FROM ash_anomalies WHERE id=?", (anomaly_id,)).fetchone())

    # ------------------------------------------------------------------ 内部方法

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.repository.temple_by_code(code)
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    def _container(self, temple_id: int | None, code: str) -> sqlite3.Row:
        return self._container_row(self.connection, temple_id, code)

    def _container_row(self, connection: sqlite3.Connection, temple_id: int | None, code: str) -> sqlite3.Row:
        if temple_id is None:
            row = connection.execute("SELECT * FROM ash_containers WHERE code=?", (code,)).fetchone()
        else:
            row = connection.execute("SELECT * FROM ash_containers WHERE temple_id=? AND code=?", (temple_id, code)).fetchone()
        if row is None:
            raise NotFoundError(f"香灰容器不存在：{code}")
        return row

    def _locked_container(self, connection: sqlite3.Connection, temple_id: int, code: str | None, container_id: int | None = None) -> sqlite3.Row:
        if container_id is None:
            row = connection.execute(
                "SELECT * FROM ash_containers WHERE temple_id=? AND code=?", (temple_id, code),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"香灰容器不存在：{code}")
        else:
            row = connection.execute("SELECT * FROM ash_containers WHERE id=?", (container_id,)).fetchone()
            if row is None:
                raise NotFoundError("香灰容器不存在")
        return row

    def _handover(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM ash_handovers WHERE handover_code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("交接凭据不存在")
        return row

    def _locked_handover(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM ash_handovers WHERE handover_code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("交接凭据不存在")
        return row

    def _disposal(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM ash_disposals WHERE disposal_code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("最终处置记录不存在")
        return row

    @staticmethod
    def _assert_flow(from_status: str, to_status: str, event_type: str) -> None:
        if (from_status, to_status) not in ALLOWED_STATUS_FLOW[event_type]:
            raise ConflictError(f"容器状态不允许 {event_type}：{from_status}->{to_status}")

    @staticmethod
    def _stored_until(now: str, retention_hours: int | None) -> str | None:
        if not retention_hours:
            return None
        parsed = from_storage(now)
        return to_storage(parsed + timedelta(hours=int(retention_hours)))

    def _update_container(self, connection: sqlite3.Connection, container_id: int, **values: Any) -> None:
        values = {"current_batch_id" if key == "batch_id" else
                  "current_weight_grams" if key == "weight" else key: value for key, value in values.items()}
        fields = ["%s=?" % key for key in values]
        params = list(values.values())
        connection.execute(
            "UPDATE ash_containers SET " + ",".join(fields) + ",version=version+1,updated_at=? WHERE id=?",
            (*params, to_storage(self.clock.now()), container_id),
        )

    @staticmethod
    def _event(connection: sqlite3.Connection, temple_id: int, container_id: int, event_type: str, actor: str,
               counterparty: str, from_status: str, to_status: str, weight_grams: int, detail: dict[str, Any], now: str) -> sqlite3.Row:
        cursor = connection.execute(
            "INSERT INTO ash_custody_events(temple_id,event_type,container_id,actor,counterparty,from_status,to_status,weight_grams,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (temple_id, event_type, container_id, actor, counterparty, from_status, to_status, weight_grams,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )
        return connection.execute("SELECT * FROM ash_custody_events WHERE id=?", (cursor.lastrowid,)).fetchone()

    @staticmethod
    def _last_event_id(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    @staticmethod
    def _ledger_row(connection: sqlite3.Connection, temple_id: int, node_event_id: int, container_id: int, batch_id: int | None,
                    direction: str, signed_weight_grams: int, balance_after_grams: int, reason: str, actor: str, now: str) -> None:
        connection.execute(
            "INSERT INTO ash_weight_ledger(temple_id,node_event_id,container_id,batch_id,direction,signed_weight_grams,balance_after_grams,reason,actor,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (temple_id, node_event_id, container_id, batch_id, direction, signed_weight_grams, balance_after_grams, reason, actor, now),
        )

    def _next_business_code(self, connection: sqlite3.Connection, temple_id: int, prefix: str, table: str, column: str) -> str:
        sequence = int(connection.execute(
            "SELECT COALESCE(MAX(id),0)+1 FROM " + table + " WHERE temple_id=?", (temple_id,),
        ).fetchone()[0])
        return f"{prefix}{self.clock.now().strftime('%Y%m%d')}-{sequence:06d}"

    def _open_anomaly(self, connection: sqlite3.Connection, temple_id: int, anomaly_type: str, severity: str, now: str,
                      *, container_id: int | None = None, handover_id: int | None = None, detail: dict[str, Any] | None = None,
                      reuse: bool = False) -> dict[str, Any] | None:
        if reuse:
            existing = connection.execute(
                "SELECT id FROM ash_anomalies WHERE temple_id=? AND anomaly_type=? AND state IN ('open','acknowledged') "
                "AND COALESCE(container_id,-1)=COALESCE(?,-1) AND COALESCE(handover_id,-1)=COALESCE(?,-1)",
                (temple_id, anomaly_type, container_id, handover_id),
            ).fetchone()
            if existing is not None:
                return None
        code = f"ANM-{self.clock.now().strftime('%Y%m%d')}-{int(connection.execute('SELECT COALESCE(MAX(id),0)+1 FROM ash_anomalies').fetchone()[0]):06d}"
        cursor = connection.execute(
            "INSERT INTO ash_anomalies(code,temple_id,anomaly_type,severity,container_id,handover_id,detail_json,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (code, temple_id, anomaly_type, severity, container_id, handover_id,
             json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), now),
        )
        return self._anomaly_dict(connection.execute("SELECT * FROM ash_anomalies WHERE id=?", (cursor.lastrowid,)).fetchone())

    @staticmethod
    def _anomaly_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["detail"] = json.loads(result.pop("detail_json"))
        return result

    def _events(self, connection: sqlite3.Connection, container_id: int) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM ash_custody_events WHERE container_id=? ORDER BY id", (container_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def _ledger(self, connection: sqlite3.Connection, container_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM ash_weight_ledger WHERE container_id=? ORDER BY id", (container_id,),
        ).fetchall()]

    @staticmethod
    def _chain_continuous(events: list[dict[str, Any]]) -> bool:
        current = ""
        for event in events:
            pair = (event["from_status"], event["to_status"])
            if event["event_type"] not in ALLOWED_STATUS_FLOW or pair not in ALLOWED_STATUS_FLOW[event["event_type"]]:
                return False
            if event["from_status"] != current:
                return False
            current = event["to_status"]
        return True

    def _batch_tree(self, connection: sqlite3.Connection, root_batch_id: int) -> dict[str, Any]:
        rows = connection.execute(
            "WITH RECURSIVE tree(id) AS ("
            "SELECT ? UNION ALL SELECT bc.component_batch_id FROM ash_batch_components bc JOIN tree t ON t.id=bc.merge_batch_id"
            ") SELECT id FROM tree",
            (root_batch_id,),
        ).fetchall()
        all_ids = [int(row["id"]) for row in rows]
        return {"root": root_batch_id, "all": all_ids}

    def _downstream_batches(self, connection: sqlite3.Connection, batch_id: int) -> list[int]:
        rows = connection.execute(
            "WITH RECURSIVE downstream(id) AS ("
            "SELECT ? UNION ALL SELECT bc.merge_batch_id FROM ash_batch_components bc JOIN downstream d ON d.id=bc.component_batch_id"
            ") SELECT id FROM downstream",
            (batch_id,),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def _node_verifications(self, connection: sqlite3.Connection, tree: dict[str, Any]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(tree["all"]))
        batches = connection.execute(
            "SELECT b.*,h.code AS hall_code,h.name AS hall_name FROM ash_batches b LEFT JOIN worship_halls h ON h.id=b.hall_id "
            "WHERE b.id IN (%s)" % placeholders, tree["all"],
        ).fetchall()
        nodes = []
        for batch in batches:
            item = dict(batch)
            components = [dict(row) for row in connection.execute(
                "SELECT bc.*,child.batch_code AS component_batch_code FROM ash_batch_components bc "
                "JOIN ash_batches child ON child.id=bc.component_batch_id WHERE bc.merge_batch_id=? ORDER BY bc.rowid",
                (batch["id"],),
            ).fetchall()]
            component_sum = sum(row["weight_grams"] for row in components)
            adjustment = int(connection.execute(
                "SELECT COALESCE(SUM(signed_weight_grams),0) FROM ash_weight_ledger WHERE batch_id=? AND direction='adjust'",
                (batch["id"],),
            ).fetchone()[0])
            item["components"] = components
            item["component_sum_grams"] = component_sum
            item["adjustment_grams"] = adjustment
            if item["origin_type"] == "collection":
                item["balanced"] = item["weight_grams"] == item["declared_weight_grams"] + adjustment
            else:
                item["balanced"] = item["weight_grams"] == component_sum + adjustment
            nodes.append(item)
        nodes.sort(key=lambda item: item["id"])
        return nodes

    def _source_aggregation(self, connection: sqlite3.Connection, tree: dict[str, Any]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(tree["all"]))
        rows = connection.execute(
            "SELECT b.hall_id,h.code AS hall_code,h.name AS hall_name,b.batch_code,b.weight_grams "
            "FROM ash_batches b LEFT JOIN worship_halls h ON h.id=b.hall_id "
            "WHERE b.id IN (%s) AND b.origin_type='collection' ORDER BY b.id" % placeholders, tree["all"],
        ).fetchall()
        by_hall: dict[int | None, dict[str, Any]] = {}
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            items.append(item)
            key = row["hall_id"]
            agg = by_hall.setdefault(key, {"hall_id": key, "hall_code": row["hall_code"], "hall_name": row["hall_name"], "total_grams": 0, "batches": []})
            agg["total_grams"] += row["weight_grams"]
            agg["batches"].append(row["batch_code"])
        return {"items": items, "by_hall": list(by_hall.values())}
