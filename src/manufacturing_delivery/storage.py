"""制造交付编排服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS mfg_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','quality','coordinator','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment_models (
    model_id TEXT PRIMARY KEY,
    equipment_type TEXT NOT NULL,
    name TEXT NOT NULL,
    preferred_components_json TEXT NOT NULL,
    bom_json TEXT NOT NULL,
    routing_hours_json TEXT NOT NULL,
    gates_json TEXT NOT NULL,
    accepts_substitutes INTEGER NOT NULL DEFAULT 1 CHECK(accepts_substitutes IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_lots (
    lot_id TEXT PRIMARY KEY,
    component_model TEXT NOT NULL,
    category TEXT NOT NULL,
    quality_grade TEXT NOT NULL,
    grade_rank INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    held_qty INTEGER NOT NULL DEFAULT 0,
    consumed_qty INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL,
    quality_state TEXT NOT NULL DEFAULT 'quarantined'
        CHECK(quality_state IN ('quarantined','released','rejected')),
    released_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES mfg_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lots_model_state
ON component_lots(component_model, quality_state);

CREATE TABLE IF NOT EXISTS substitute_rules (
    rule_id TEXT PRIMARY KEY,
    equipment_type TEXT NOT NULL,
    category TEXT NOT NULL,
    preferred_model TEXT NOT NULL,
    substitute_model TEXT NOT NULL,
    allow_customer_override INTEGER NOT NULL DEFAULT 1 CHECK(allow_customer_override IN (0,1)),
    minimum_grade_rank INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(equipment_type, category, preferred_model, substitute_model)
);

CREATE TABLE IF NOT EXISTS line_capacities (
    cap_id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id TEXT NOT NULL,
    station TEXT NOT NULL,
    service_date TEXT NOT NULL,
    available_hours TEXT NOT NULL,
    booked_hours TEXT NOT NULL DEFAULT '0',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(line_id, station, service_date)
);

CREATE INDEX IF NOT EXISTS idx_capacity_station_date
ON line_capacities(station, service_date);

CREATE TABLE IF NOT EXISTS shipping_windows (
    window_id TEXT PRIMARY KEY,
    destination TEXT NOT NULL,
    opens_on TEXT NOT NULL,
    closes_on TEXT NOT NULL,
    capacity_units INTEGER NOT NULL,
    booked_units INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed','departed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    CHECK(closes_on >= opens_on)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    due_date TEXT NOT NULL,
    window_id TEXT NOT NULL REFERENCES shipping_windows(window_id),
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','confirmed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES mfg_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_state_due
ON orders(state, due_date);

CREATE TABLE IF NOT EXISTS order_items (
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    model_id TEXT NOT NULL REFERENCES equipment_models(model_id),
    quantity INTEGER NOT NULL,
    allow_substitutes INTEGER NOT NULL DEFAULT 0 CHECK(allow_substitutes IN (0,1)),
    required_grades_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(order_id, model_id)
);

CREATE TABLE IF NOT EXISTS plan_snapshots (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    order_revision INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    conflict_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL REFERENCES mfg_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_units (
    unit_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    model_id TEXT NOT NULL REFERENCES equipment_models(model_id),
    unit_seq INTEGER NOT NULL,
    state TEXT NOT NULL
        CHECK(state IN ('scheduled','in_production','awaiting_inspection','inspected','shipped','blocked')),
    design_revision TEXT NOT NULL,
    design_json TEXT NOT NULL,
    planned_complete_on TEXT NOT NULL,
    started_at TEXT,
    production_completed_at TEXT,
    inspected_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(order_id, unit_seq)
);

CREATE INDEX IF NOT EXISTS idx_units_state ON production_units(state);

CREATE TABLE IF NOT EXISTS unit_components (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id TEXT NOT NULL REFERENCES production_units(unit_id),
    lot_id TEXT NOT NULL REFERENCES component_lots(lot_id),
    category TEXT NOT NULL,
    component_model TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    is_substitute INTEGER NOT NULL DEFAULT 0 CHECK(is_substitute IN (0,1)),
    rule_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('held','consumed','released')),
    created_at TEXT NOT NULL,
    UNIQUE(unit_id, category)
);

CREATE TABLE IF NOT EXISTS unit_schedule (
    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id TEXT NOT NULL REFERENCES production_units(unit_id),
    line_id TEXT NOT NULL,
    station TEXT NOT NULL,
    seq_no INTEGER NOT NULL,
    service_date TEXT NOT NULL,
    hours TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('scheduled','done','released')),
    UNIQUE(unit_id, station)
);

CREATE INDEX IF NOT EXISTS idx_schedule_date_station
ON unit_schedule(service_date, station);

CREATE TABLE IF NOT EXISTS inspection_records (
    inspection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id TEXT NOT NULL REFERENCES production_units(unit_id),
    gate TEXT NOT NULL,
    seq_no INTEGER NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('pending','passed','failed','waived')),
    inspector_id TEXT REFERENCES mfg_users(user_id),
    note TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    UNIQUE(unit_id, gate)
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL UNIQUE REFERENCES production_units(unit_id),
    window_id TEXT NOT NULL REFERENCES shipping_windows(window_id),
    shipped_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES mfg_users(user_id)
);

CREATE TABLE IF NOT EXISTS engineering_changes (
    change_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES equipment_models(model_id),
    revision_label TEXT NOT NULL,
    effective_on TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('none','unstarted_only','all')),
    overrides_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded')),
    created_by TEXT NOT NULL REFERENCES mfg_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL REFERENCES engineering_changes(change_id),
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    conflict_json TEXT NOT NULL DEFAULT '[]',
    applied INTEGER NOT NULL DEFAULT 1 CHECK(applied IN (0,1)),
    undone INTEGER NOT NULL DEFAULT 0 CHECK(undone IN (0,1)),
    created_by TEXT NOT NULL REFERENCES mfg_users(user_id),
    created_at TEXT NOT NULL,
    undone_at TEXT,
    UNIQUE(change_id, order_id)
);

CREATE TABLE IF NOT EXISTS mfg_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS mfg_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mfg_audit_entity
ON mfg_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False：ThreadingHTTPServer 会把请求分派到工作线程；
    # 所有写操作都经 BEGIN IMMEDIATE 串行化，配合 busy_timeout 保证跨线程安全。
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
