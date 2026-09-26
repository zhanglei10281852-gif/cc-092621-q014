from __future__ import annotations

import sqlite3

ASH_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS ash_containers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    capacity_grams INTEGER NOT NULL CHECK(capacity_grams > 0),
    status TEXT NOT NULL DEFAULT 'empty' CHECK(status IN ('empty','sealed','in_transit','stored','disposed')),
    current_batch_id INTEGER REFERENCES ash_batches(id),
    current_weight_grams INTEGER NOT NULL DEFAULT 0 CHECK(current_weight_grams >= 0),
    seal_code TEXT NOT NULL DEFAULT '',
    custodian TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    stored_at TEXT,
    stored_until TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(temple_id, code)
);
CREATE INDEX IF NOT EXISTS idx_ash_containers_status ON ash_containers(temple_id,status,stored_until);
CREATE TABLE IF NOT EXISTS ash_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL UNIQUE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    origin_type TEXT NOT NULL CHECK(origin_type IN ('collection','merge')),
    declared_weight_grams INTEGER NOT NULL CHECK(declared_weight_grams >= 0),
    weight_grams INTEGER NOT NULL CHECK(weight_grams >= 0),
    merge_node_event_id INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ash_batches_temple ON ash_batches(temple_id,id);
CREATE TABLE IF NOT EXISTS ash_batch_components (
    merge_batch_id INTEGER NOT NULL REFERENCES ash_batches(id) ON DELETE CASCADE,
    component_batch_id INTEGER NOT NULL REFERENCES ash_batches(id),
    weight_grams INTEGER NOT NULL CHECK(weight_grams >= 0),
    PRIMARY KEY(merge_batch_id, component_batch_id)
);
CREATE TABLE IF NOT EXISTS ash_handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_code TEXT NOT NULL UNIQUE,
    idempotency_key TEXT UNIQUE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    container_id INTEGER NOT NULL REFERENCES ash_containers(id),
    from_party TEXT NOT NULL,
    to_party TEXT NOT NULL,
    to_location TEXT NOT NULL,
    retention_hours INTEGER,
    prior_status TEXT NOT NULL,
    weight_snapshot_grams INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','cancelled')),
    initiated_by TEXT NOT NULL,
    initiated_at TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    cancel_reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_ash_handovers_status ON ash_handovers(status,initiated_at);
CREATE TABLE IF NOT EXISTS ash_disposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disposal_code TEXT NOT NULL UNIQUE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    container_id INTEGER NOT NULL REFERENCES ash_containers(id),
    batch_id INTEGER NOT NULL REFERENCES ash_batches(id),
    weight_grams INTEGER NOT NULL CHECK(weight_grams >= 0),
    method TEXT NOT NULL,
    actor TEXT NOT NULL,
    witness TEXT NOT NULL DEFAULT '',
    node_event_id INTEGER NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    disposed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ash_custody_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'register','pack','transfer_initiate','transfer_confirm','transfer_cancel',
        'merge_split','merge_pack','reweigh','dispose'
    )),
    container_id INTEGER NOT NULL REFERENCES ash_containers(id),
    actor TEXT NOT NULL,
    counterparty TEXT NOT NULL DEFAULT '',
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL DEFAULT '',
    weight_grams INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ash_custody_events_container ON ash_custody_events(container_id,id);
CREATE INDEX IF NOT EXISTS idx_ash_custody_events_temple ON ash_custody_events(temple_id,id);
CREATE TABLE IF NOT EXISTS ash_weight_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    node_event_id INTEGER NOT NULL REFERENCES ash_custody_events(id),
    container_id INTEGER NOT NULL REFERENCES ash_containers(id),
    batch_id INTEGER REFERENCES ash_batches(id),
    direction TEXT NOT NULL CHECK(direction IN ('in','out','adjust')),
    signed_weight_grams INTEGER NOT NULL,
    balance_after_grams INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ash_ledger_container ON ash_weight_ledger(container_id,id);
CREATE INDEX IF NOT EXISTS idx_ash_ledger_node ON ash_weight_ledger(node_event_id);
CREATE TABLE IF NOT EXISTS ash_anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    anomaly_type TEXT NOT NULL CHECK(anomaly_type IN (
        'storage_overdue','handover_stalled','receipt_weight_mismatch','weight_verification'
    )),
    severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
    container_id INTEGER REFERENCES ash_containers(id),
    handover_id INTEGER REFERENCES ash_handovers(id),
    detail_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','acknowledged','resolved')),
    opened_at TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at TEXT,
    resolved_by TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ash_anomalies_state ON ash_anomalies(state,anomaly_type,opened_at);
'''


def ensure_ash_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(ASH_SCHEMA)
