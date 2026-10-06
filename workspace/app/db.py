"""SQLite 连接管理与 schema 初始化。

所有写操作通过显式事务串行化（BEGIN IMMEDIATE），保证数据修复、规则回放、
发布冻结、异常豁免四类并发操作之间的一致性。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from . import config

# 显式串行点：跨多句的写事务用这把锁先在应用层排队，
# 再配合 SQLite 的 BEGIN IMMEDIATE 防止数据库层死锁。
_tx_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=30.0,
        isolation_level=None,  # 自己管理事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """只读/单语句连接（自动提交模式，纯查询不开启事务）。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """立即型写事务：进入即拿取库级写锁，应用层锁保证四类并发操作串行裁决。"""
    with _tx_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


SCHEMA = """
-- 数据产品：字段、约束、跨表规则所附着的固定发布单元
CREATE TABLE IF NOT EXISTS data_products (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 数据快照：产品的一个不可变数据版本（修复会产生新快照，旧快照永不被改写）
CREATE TABLE IF NOT EXISTS data_snapshots (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES data_products(id),
    snapshot_ref    TEXT NOT NULL,          -- 业务引用，如 2026Q3-daily-042
    parent_id       INTEGER REFERENCES data_snapshots(id),  -- 修复链
    origin          TEXT NOT NULL,          -- publish | repair
    repair_note     TEXT NOT NULL DEFAULT '',
    row_count       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(product_id, snapshot_ref)
);

-- 快照内的数据表（行内容以 JSON 存放，key 为列名）
CREATE TABLE IF NOT EXISTS snapshot_rows (
    id              INTEGER PRIMARY KEY,
    snapshot_id     INTEGER NOT NULL REFERENCES data_snapshots(id),
    table_name      TEXT NOT NULL,
    -- 稳定行标识：业务主键 JSON，如 {"order_id":"O-1"}；回放时靠它定位同一条样本
    row_key         TEXT NOT NULL,
    payload         TEXT NOT NULL,          -- JSON 对象
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(snapshot_id, table_name, row_key)
);

-- 规则版本链
CREATE TABLE IF NOT EXISTS rule_versions (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES data_products(id),
    rule_code       TEXT NOT NULL,          -- 稳定业务规则标识，如 ORDER_AMOUNT_RANGE
    engine_code     TEXT NOT NULL,          -- 引擎算子：NOT_NULL/REGEX/RANGE/ENUM/UNIQUE/REFERENTIAL_INTEGRITY/SQL_ASSERTION
    version         INTEGER NOT NULL,
    name            TEXT NOT NULL,
    rule_type       TEXT NOT NULL,          -- field | constraint | cross_table
    table_name      TEXT NOT NULL,
    -- field: 列名; constraint: 表达式 JSON; cross_table: 关联配置 JSON
    params          TEXT NOT NULL,          -- JSON
    severity        TEXT NOT NULL DEFAULT 'error',  -- error | warning
    -- 规则摘要：code+version+规范化参数的哈希，豁免绑定它而不是绑定可变行
    rule_digest     TEXT NOT NULL,
    body_sha256     TEXT NOT NULL,          -- 规则定义体的 SHA-256（凭据用）
    -- active=当前生效；superseded=被同编码新版本取代（行永久保留以追溯历史结果）；
    -- deprecated=人工弃用
    status          TEXT NOT NULL DEFAULT 'active',
    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(product_id, rule_code, version)
);

-- 结果快照（执行批次）：对某数据快照、按某规则集版本执行后的不可变裁决
CREATE TABLE IF NOT EXISTS result_snapshots (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES data_products(id),
    data_snapshot_id INTEGER NOT NULL REFERENCES data_snapshots(id),
    kind            TEXT NOT NULL,          -- publish（发布判断）| replay（回放修订）
    -- 发布判断链：同产品下每一份发布结果指向上一份；replay 指向被修订的发布结果
    supersedes_id   INTEGER REFERENCES result_snapshots(id),
    frozen          INTEGER NOT NULL DEFAULT 0,  -- 发布冻结后不可再被重判
    rule_set_sha256 TEXT NOT NULL,          -- 本次执行所用全部规则版本摘要的哈希
    total_rules     INTEGER NOT NULL,
    total_rows      INTEGER NOT NULL,
    failure_count   INTEGER NOT NULL,
    verdict         TEXT NOT NULL,          -- pass | fail
    executor        TEXT NOT NULL DEFAULT 'scheduler',
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- 每份数据快照至多一份发布判断（回放只能形成 replay 修订版）
CREATE UNIQUE INDEX IF NOT EXISTS idx_result_publish_once
    ON result_snapshots(data_snapshot_id) WHERE kind = 'publish';

-- 结果快照内逐条失败样本的定位与规则版本
CREATE TABLE IF NOT EXISTS findings (
    id                  INTEGER PRIMARY KEY,
    result_snapshot_id  INTEGER NOT NULL REFERENCES result_snapshots(id),
    rule_version_id     INTEGER NOT NULL REFERENCES rule_versions(id),
    rule_code           TEXT NOT NULL,
    engine_code         TEXT NOT NULL DEFAULT '',
    rule_version        INTEGER NOT NULL,
    rule_digest         TEXT NOT NULL,      -- 冗余存档：即便规则版本被废弃也不丢摘要
    table_name          TEXT NOT NULL,
    row_key             TEXT NOT NULL,      -- 精确样本定位（稳定主键 JSON）
    column_name         TEXT NOT NULL DEFAULT '',
    observed_value      TEXT NOT NULL DEFAULT '',
    message             TEXT NOT NULL DEFAULT '',
    seq                 INTEGER NOT NULL,   -- 结果快照内的稳定序号（分页用）
    -- 执行时刻该失败是否已被有效豁免：随结果快照冻结，之后豁免表变化不改写它
    exempt_at_execution INTEGER NOT NULL DEFAULT 0,
    UNIQUE(result_snapshot_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_findings_rs ON findings(result_snapshot_id);

-- 异常豁免：必须同时绑定精确样本（快照+表+行键）与规则摘要
CREATE TABLE IF NOT EXISTS exemptions (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES data_products(id),
    -- 绑定的数据范围：精确到某数据快照的某张表某条行键
    data_snapshot_id INTEGER NOT NULL REFERENCES data_snapshots(id),
    table_name      TEXT NOT NULL,
    row_key         TEXT NOT NULL,
    column_name     TEXT NOT NULL DEFAULT '',
    -- 绑定的规则：规则编码 + 精确版本 + 规则摘要（防规则升级后误豁免）
    rule_code       TEXT NOT NULL,
    rule_version    INTEGER NOT NULL,
    rule_digest     TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'granted', -- granted | revoked
    created_by      TEXT NOT NULL DEFAULT 'steward',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    revoked_at      TEXT,
    -- 同一精确样本 × 同一规则摘要只允许一条有效豁免
    UNIQUE(data_snapshot_id, table_name, row_key, column_name, rule_digest)
);
CREATE INDEX IF NOT EXISTS idx_exempt_lookup
    ON exemptions(data_snapshot_id, table_name, row_key, rule_digest);

-- 结果快照级别的裁决凭证（可下载质量凭据）
CREATE TABLE IF NOT EXISTS credentials (
    id                  INTEGER PRIMARY KEY,
    result_snapshot_id  INTEGER NOT NULL UNIQUE REFERENCES result_snapshots(id),
    credential_ref      TEXT NOT NULL UNIQUE,   -- 如 QC-P1-000007
    file_path           TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    issued_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def init_db() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
