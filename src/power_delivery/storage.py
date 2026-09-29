"""制造交付编排服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


# 事务互斥锁：HTTP 服务每个请求运行在独立工作线程、共享同一 SQLite
# 连接，需要防止 BEGIN ... COMMIT 在连接上交错。部署形态为单容器
# 单连接，进程级锁即足够；测试中的内存连接均为单线程使用。
_TRANSACTION_LOCK = threading.RLock()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pd_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('sales','coordinator','engineer','warehouse','quality','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_constraints (
    customer_id TEXT PRIMARY KEY,
    approved_grades_json TEXT NOT NULL,
    allowed_substitutes_json TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_parts (
    part_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    family TEXT NOT NULL CHECK(family IN ('transformer','switchgear')),
    unit TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS substitution_rules (
    rule_id TEXT PRIMARY KEY,
    original_part_id TEXT NOT NULL REFERENCES component_parts(part_id),
    substitute_part_id TEXT NOT NULL REFERENCES component_parts(part_id),
    minimum_grade TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(original_part_id <> substitute_part_id)
);

CREATE TABLE IF NOT EXISTS component_batches (
    batch_id TEXT PRIMARY KEY,
    part_id TEXT NOT NULL REFERENCES component_parts(part_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    grade TEXT NOT NULL,
    heat_number TEXT NOT NULL,
    certified INTEGER NOT NULL CHECK(certified IN (0,1)),
    received_on TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_part
ON component_batches(part_id, received_on);

CREATE TABLE IF NOT EXISTS production_lines (
    line_id TEXT PRIMARY KEY,
    family TEXT NOT NULL CHECK(family IN ('transformer','switchgear')),
    name TEXT NOT NULL,
    daily_capacity INTEGER NOT NULL CHECK(daily_capacity > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inspection_gates (
    gate_id TEXT PRIMARY KEY,
    family TEXT NOT NULL CHECK(family IN ('transformer','switchgear')),
    name TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    duration_days INTEGER NOT NULL DEFAULT 0 CHECK(duration_days >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipping_windows (
    window_id TEXT PRIMARY KEY,
    destination TEXT NOT NULL,
    opens_on TEXT NOT NULL,
    closes_on TEXT NOT NULL,
    slots INTEGER NOT NULL CHECK(slots > 0),
    created_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(closes_on >= opens_on)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    product_model TEXT NOT NULL,
    family TEXT NOT NULL CHECK(family IN ('transformer','switchgear')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    requested_date TEXT NOT NULL,
    window_id TEXT NOT NULL REFERENCES shipping_windows(window_id),
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','confirmed','in_production','change_pending','completed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 0,
    current_plan_revision INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_window
ON orders(window_id, state);

CREATE TABLE IF NOT EXISTS order_bom (
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    part_id TEXT NOT NULL REFERENCES component_parts(part_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    PRIMARY KEY(order_id, part_id)
);

CREATE TABLE IF NOT EXISTS order_plan_revisions (
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    revision INTEGER NOT NULL CHECK(revision > 0),
    plan_json TEXT NOT NULL,
    issues_json TEXT NOT NULL,
    feasible INTEGER NOT NULL CHECK(feasible IN (0,1)),
    change_id TEXT,
    restored_from INTEGER,
    created_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(order_id, revision)
);

CREATE TABLE IF NOT EXISTS component_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    plan_revision INTEGER NOT NULL,
    unit_no INTEGER NOT NULL CHECK(unit_no > 0),
    requirement_part_id TEXT NOT NULL,
    batch_id TEXT NOT NULL REFERENCES component_batches(batch_id),
    part_id TEXT NOT NULL,
    rule_id TEXT,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_batch_state
ON component_reservations(batch_id, state);

CREATE INDEX IF NOT EXISTS idx_reservations_order
ON component_reservations(order_id, plan_revision, unit_no);

CREATE TABLE IF NOT EXISTS unit_states (
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    unit_no INTEGER NOT NULL CHECK(unit_no > 0),
    revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('planned','production','inspection','awaiting_shipment','shipped','completed','scrapped')),
    product_model TEXT NOT NULL,
    line_id TEXT,
    production_date TEXT,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(order_id, unit_no)
);

CREATE TABLE IF NOT EXISTS gate_results (
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    unit_no INTEGER NOT NULL,
    gate_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('passed','failed')),
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL REFERENCES pd_users(user_id),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(order_id, unit_no, gate_id)
);

CREATE TABLE IF NOT EXISTS engineering_changes (
    change_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','applied','rejected','cancelled')),
    revision_from INTEGER NOT NULL,
    revision_to INTEGER,
    created_by TEXT NOT NULL REFERENCES pd_users(user_id),
    created_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS pd_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS pd_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_pd_audit_entity
ON pd_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # HTTP 服务为每个连接分配工作线程；写入已统一使用 BEGIN IMMEDIATE
    # 配合 busy_timeout 串行化，因此允许跨线程复用同一连接。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
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
    with _TRANSACTION_LOCK:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
