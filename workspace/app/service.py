"""领域服务层：版本化执行、历史回放、发布冻结、异常豁免、质量凭据。

并发模型：数据修复(repair)、规则回放(replay)、发布冻结(freeze)、异常豁免(grant)
四类操作全部经过 db.transaction() 的同一把串行锁，裁决顺序确定、互不覆盖。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config, engine
from .db import get_conn, transaction


class DomainError(Exception):
    """业务规则冲突（快照已冻结、豁免重复、版本不存在等）。

    status 为建议的 HTTP 状态码；缺省由路由按上下文决定。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- 基础对象

def create_product(name: str, description: str = "") -> dict[str, Any]:
    with transaction() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO data_products(name, description) VALUES (?, ?)",
                (name, description),
            )
        except sqlite3.IntegrityError:
            raise DomainError(f"product name {name!r} already exists",
                              status=409) from None
        return _product(conn, cur.lastrowid)


def get_product(conn: sqlite3.Connection, product_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM data_products WHERE id = ?", (product_id,)
    ).fetchone()
    if not row:
        raise DomainError(f"product {product_id} not found")
    return dict(row)


def fetch_product(product_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        return get_product(conn, product_id)


def list_products() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM data_products ORDER BY id"
        )]


def _product(conn: sqlite3.Connection, pid: int) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM data_products WHERE id=?", (pid,)).fetchone())


def publish_snapshot(
    product_id: int,
    snapshot_ref: str,
    tables: dict[str, list[dict[str, Any]]],
    key_columns: dict[str, list[str]] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """登记一份不可变数据快照（首次发布）。tables: {表名: [行 JSON]}，
    key_columns 指定每张表的业务主键列（缺省用内置 _row_key 或单列 id）。"""
    key_columns = key_columns or {}
    _validate_tables(tables, key_columns)
    with transaction() as conn:
        get_product(conn, product_id)
        dup = conn.execute(
            "SELECT 1 FROM data_snapshots WHERE product_id=? AND snapshot_ref=?",
            (product_id, snapshot_ref),
        ).fetchone()
        if dup:
            raise DomainError(f"snapshot_ref {snapshot_ref!r} already exists")
        cur = conn.execute(
            "INSERT INTO data_snapshots(product_id, snapshot_ref, parent_id, origin,"
            " repair_note, row_count) VALUES (?,?,NULL,'publish',?,?)",
            (product_id, snapshot_ref, note, sum(len(v) for v in tables.values())),
        )
        snap_id = cur.lastrowid
        _insert_rows(conn, snap_id, tables, key_columns)
        return dict(conn.execute(
            "SELECT * FROM data_snapshots WHERE id=?", (snap_id,)
        ).fetchone())


def repair_snapshot(
    product_id: int,
    source_snapshot_id: int,
    new_snapshot_ref: str,
    edits: dict[str, list[dict[str, Any]]] | None = None,
    deletes: dict[str, list[Any]] | None = None,
    key_columns: dict[str, list[str]] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """数据修复：从旧快照派生新的不可变快照（upsert/delete 行），旧快照原样保留。"""
    edits = edits or {}
    deletes = deletes or {}
    merged_keys = dict(key_columns or {})
    with transaction() as conn:
        get_product(conn, product_id)
        src = conn.execute(
            "SELECT * FROM data_snapshots WHERE id=? AND product_id=?",
            (source_snapshot_id, product_id),
        ).fetchone()
        if not src:
            raise DomainError("source snapshot not found")
        dup = conn.execute(
            "SELECT 1 FROM data_snapshots WHERE product_id=? AND snapshot_ref=?",
            (product_id, new_snapshot_ref),
        ).fetchone()
        if dup:
            raise DomainError(f"snapshot_ref {new_snapshot_ref!r} already exists")

        # 以旧快照为基线复制全部行，再叠加修复
        rows_by_table: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for r in conn.execute(
            "SELECT table_name, row_key, payload FROM snapshot_rows WHERE snapshot_id=?",
            (source_snapshot_id,),
        ):
            rows_by_table.setdefault(r["table_name"], []).append(
                (r["row_key"], json.loads(r["payload"]))
            )
        cur = conn.execute(
            "INSERT INTO data_snapshots(product_id, snapshot_ref, parent_id, origin,"
            " repair_note, row_count) VALUES (?,?,?,'repair',?,0)",
            (product_id, new_snapshot_ref, source_snapshot_id, note),
        )
        new_id = cur.lastrowid

        # 叠加 edits（按 row_key upsert）
        for table, rows in edits.items():
            if table not in rows_by_table:
                rows_by_table[table] = []
            kc = (merged_keys.get(table)
                  or _derive_key_columns(table, rows_by_table[table])
                  or _infer_key_columns(table, rows))
            index = {rk: i for i, (rk, _) in enumerate(rows_by_table[table])}
            for payload in rows:
                rk = _row_key_for(table, payload, kc)
                if rk in index:
                    rows_by_table[table][index[rk]] = (rk, payload)
                else:
                    rows_by_table[table].append((rk, payload))
                    index[rk] = len(rows_by_table[table]) - 1
        # 叠加 deletes（未显式给主键列时，从旧快照既有行键推导）
        for table, keys in deletes.items():
            if table not in rows_by_table:
                continue
            kc = merged_keys.get(table) or _derive_key_columns(table, rows_by_table[table])
            wanted = {_row_key_for(table, k, kc) for k in keys}
            rows_by_table[table] = [
                (rk, p) for rk, p in rows_by_table[table] if rk not in wanted
            ]

        total = 0
        for table, rows in rows_by_table.items():
            for rk, payload in rows:
                conn.execute(
                    "INSERT INTO snapshot_rows(snapshot_id, table_name, row_key, payload)"
                    " VALUES (?,?,?,?)",
                    (new_id, table, rk, engine.canonical_json(payload)),
                )
                total += 1
        conn.execute("UPDATE data_snapshots SET row_count=? WHERE id=?", (total, new_id))
        return dict(conn.execute(
            "SELECT * FROM data_snapshots WHERE id=?", (new_id,)
        ).fetchone())


def list_snapshots(product_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        get_product(conn, product_id)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM data_snapshots WHERE product_id=? ORDER BY id", (product_id,)
        )]


def get_snapshot_rows(
    snapshot_id: int, table: str | None = None
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    with get_conn() as conn:
        snap = conn.execute(
            "SELECT * FROM data_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if not snap:
            raise DomainError("snapshot not found")
        q = "SELECT table_name, row_key, payload FROM snapshot_rows WHERE snapshot_id=?"
        args: list[Any] = [snapshot_id]
        if table:
            q += " AND table_name=?"
            args.append(table)
        out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for r in conn.execute(q + " ORDER BY id", args):
            out.setdefault(r["table_name"], []).append(
                (r["row_key"], json.loads(r["payload"]))
            )
        return out


# ---------------------------------------------------------------- 规则 API

def create_rule_version(
    product_id: int,
    rule_code: str,
    name: str,
    rule_type: str,
    table_name: str,
    params: dict[str, Any],
    engine_code: str | None = None,
    severity: str = "error",
    created_by: str = "system",
) -> dict[str, Any]:
    """登记规则的下一个不可变版本（同 rule_code 自增 version）。"""
    definition = {
        "rule_code": rule_code,
        "engine_code": engine_code or rule_code,
        "name": name,
        "rule_type": rule_type,
        "table_name": table_name,
        "params": params,
        "severity": severity,
    }
    engine.validate_definition(definition)
    digest = engine.rule_digest(definition)
    bsha = engine.body_sha256(definition)
    with transaction() as conn:
        get_product(conn, product_id)
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM rule_versions"
            " WHERE product_id=? AND rule_code=?",
            (product_id, rule_code),
        ).fetchone()
        version = row["v"] + 1
        cur = conn.execute(
            "INSERT INTO rule_versions(product_id, rule_code, engine_code, version,"
            " name, rule_type, table_name, params, severity, rule_digest, body_sha256,"
            " created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (product_id, rule_code, definition["engine_code"], version, name,
             rule_type, table_name, engine.canonical_json(params), severity,
             digest, bsha, created_by),
        )
        # 新版本自动取代同编码的旧生效版本；旧版本行永久保留供历史结果追溯
        conn.execute(
            "UPDATE rule_versions SET status='superseded'"
            " WHERE product_id=? AND rule_code=? AND version<>? AND status='active'",
            (product_id, rule_code, version),
        )
        return _rule_version(conn, cur.lastrowid)


def deprecate_rule(product_id: int, rule_code: str) -> dict[str, Any]:
    with transaction() as conn:
        get_product(conn, product_id)
        row = conn.execute(
            "SELECT id FROM rule_versions WHERE product_id=? AND rule_code=?"
            " ORDER BY version DESC LIMIT 1",
            (product_id, rule_code),
        ).fetchone()
        if not row:
            raise DomainError(f"rule {rule_code!r} not found")
        conn.execute("UPDATE rule_versions SET status='deprecated' WHERE id=?", (row["id"],))
        return _rule_version(conn, row["id"])


def list_rule_versions(product_id: int, rule_code: str | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        get_product(conn, product_id)
        q = "SELECT * FROM rule_versions WHERE product_id=?"
        args: list[Any] = [product_id]
        if rule_code:
            q += " AND rule_code=?"
            args.append(rule_code)
        q += " ORDER BY rule_code, version"
        return [_rule_version(conn, r["id"]) for r in conn.execute(q, args)]


def _rule_version(conn: sqlite3.Connection, rid: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM rule_versions WHERE id=?", (rid,)).fetchone()
    d = dict(r)
    d["params"] = json.loads(d["params"])
    return d


def _active_rule_rows(conn: sqlite3.Connection, product_id: int,
                      rule_codes: list[str] | None) -> list[sqlite3.Row]:
    q = ("SELECT * FROM rule_versions WHERE product_id=? AND status='active'")
    args: list[Any] = [product_id]
    if rule_codes is not None:
        if not rule_codes:
            return []
        q += " AND rule_code IN (%s)" % ",".join("?" * len(rule_codes))
        args.extend(rule_codes)
    q += " ORDER BY id"
    return list(conn.execute(q, args))


def _load_rules(rows: Iterable[sqlite3.Row]) -> list[engine.LoadedRule]:
    return [
        engine.LoadedRule(
            id=r["id"], rule_code=r["rule_code"], engine_code=r["engine_code"],
            version=r["version"],
            name=r["name"], rule_type=r["rule_type"], table_name=r["table_name"],
            params=json.loads(r["params"]), severity=r["severity"],
            rule_digest=r["rule_digest"],
        )
        for r in rows
    ]


def rule_set_fingerprint(rows: Iterable[sqlite3.Row]) -> str:
    """本次执行所用规则集的指纹：各版本 id + 摘要的有序哈希。"""
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['id']}:{r['rule_code']}:v{r['version']}:{r['rule_digest']}\n"
                 .encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------- 执行 / 回放

def _load_exemptions(conn: sqlite3.Connection, data_snapshot_id: int
                     ) -> set[tuple[str, str, str, str]]:
    return {
        (r["table_name"], r["row_key"], r["column_name"], r["rule_digest"])
        for r in conn.execute(
            "SELECT table_name, row_key, column_name, rule_digest FROM exemptions"
            " WHERE data_snapshot_id=? AND status='granted'",
            (data_snapshot_id,),
        )
    }


def _execute_in_tx(
    conn: sqlite3.Connection,
    product_id: int,
    data_snapshot_id: int,
    kind: str,
    rule_codes: list[str] | None,
    supersedes_id: int | None,
    executor: str,
    note: str,
    auto_freeze: bool,
) -> dict[str, Any]:
    """事务内执行并落一份不可变结果快照。调用方必须已持有 transaction()。"""
    rows_by_table = {}
    snap = conn.execute(
        "SELECT * FROM data_snapshots WHERE id=? AND product_id=?",
        (data_snapshot_id, product_id),
    ).fetchone()
    if not snap:
        raise DomainError("data snapshot not found")
    for r in conn.execute(
        "SELECT table_name, row_key, payload FROM snapshot_rows"
        " WHERE snapshot_id=? ORDER BY id",
        (data_snapshot_id,),
    ):
        rows_by_table.setdefault(r["table_name"], []).append(
            (r["row_key"], json.loads(r["payload"]))
        )

    rule_rows = _active_rule_rows(conn, product_id, rule_codes)
    rules = _load_rules(rule_rows)
    fingerprint = rule_set_fingerprint(rule_rows)

    if kind == "publish":
        # 发布判断唯一：同一数据快照至多一条 publish 结果，且冻结后不可重判
        existing = conn.execute(
            "SELECT id, frozen FROM result_snapshots"
            " WHERE data_snapshot_id=? AND kind='publish'",
            (data_snapshot_id,),
        ).fetchone()
        if existing:
            if existing["frozen"]:
                raise DomainError(
                    "publish judgment is frozen; replay upgraded rules instead"
                )
            # 未冻结的理论上不会出现（发布即冻结），防御性拒绝重复发布
            raise DomainError("publish result already exists; use replay for revisions")
    elif kind == "replay":
        if supersedes_id is None:
            raise DomainError("replay requires supersedes_id")
        base = conn.execute(
            "SELECT id, kind FROM result_snapshots WHERE id=? AND product_id=?",
            (supersedes_id, product_id),
        ).fetchone()
        if not base:
            raise DomainError("superseded result snapshot not found")
    else:
        raise DomainError("kind must be publish|replay")

    failures = engine.evaluate(rules, rows_by_table)
    exemptions = _load_exemptions(conn, data_snapshot_id)
    total_rows = sum(len(v) for v in rows_by_table.values())

    cur = conn.execute(
        "INSERT INTO result_snapshots(product_id, data_snapshot_id, kind,"
        " supersedes_id, frozen, rule_set_sha256, total_rules, total_rows,"
        " failure_count, verdict, executor, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (product_id, data_snapshot_id, kind, supersedes_id,
         1 if (auto_freeze and kind == "publish") else 0,
         fingerprint, len(rules), total_rows, len(failures),
         # error 级失败才否决发布；warning 仅记录
         "fail" if any(f.severity == "error" for f in failures) else "pass",
         executor, note),
    )
    result_id = cur.lastrowid
    for seq, f in enumerate(sorted(failures, key=lambda x: (
            x.table_name, x.row_key, x.rule_version_id, x.column_name))):
        exempt = (f.table_name, f.row_key, f.column_name, f.rule_digest) in exemptions
        conn.execute(
            "INSERT INTO findings(result_snapshot_id, rule_version_id, rule_code,"
            " engine_code, rule_version, rule_digest, table_name, row_key, column_name,"
            " observed_value, message, seq, exempt_at_execution)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (result_id, f.rule_version_id, f.rule_code, f.engine_code,
             f.rule_version, f.rule_digest, f.table_name, f.row_key, f.column_name,
             f.observed_value, f.message, seq, 1 if exempt else 0),
        )
    return _result_snapshot(conn, result_id)


def execute_publish(
    product_id: int,
    data_snapshot_id: int,
    rule_codes: list[str] | None = None,
    executor: str = "scheduler",
    note: str = "",
) -> dict[str, Any]:
    """发布执行：形成当时发布判断并立即冻结，此后只能回放产生修订版。"""
    with transaction() as conn:
        return _execute_in_tx(
            conn, product_id, data_snapshot_id, "publish", rule_codes,
            None, executor, note, auto_freeze=True,
        )


def replay(
    product_id: int,
    data_snapshot_id: int,
    supersedes_id: int,
    rule_codes: list[str] | None = None,
    executor: str = "scheduler",
    note: str = "",
) -> dict[str, Any]:
    """规则升级后对历史快照回放：结果只能形成修订版（kind=replay），
    绝不改写被回放结果当时的发布判断。"""
    with transaction() as conn:
        return _execute_in_tx(
            conn, product_id, data_snapshot_id, "replay", rule_codes,
            supersedes_id, executor, note, auto_freeze=False,
        )


def freeze_result(product_id: int, result_snapshot_id: int) -> dict[str, Any]:
    """发布冻结（幂等）。replay 修订版允许引用冻结结果但不改变它。"""
    with transaction() as conn:
        r = conn.execute(
            "SELECT * FROM result_snapshots WHERE id=? AND product_id=?",
            (result_snapshot_id, product_id),
        ).fetchone()
        if not r:
            raise DomainError("result snapshot not found")
        if r["kind"] != "publish":
            raise DomainError("only publish judgments can be frozen")
        conn.execute("UPDATE result_snapshots SET frozen=1 WHERE id=?", (result_snapshot_id,))
        return _result_snapshot(conn, result_snapshot_id)


def get_result(result_snapshot_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        return _result_snapshot(conn, result_snapshot_id)


def _result_snapshot(conn: sqlite3.Connection, rid: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM result_snapshots WHERE id=?", (rid,)).fetchone()
    if not r:
        raise DomainError("result snapshot not found")
    d = dict(r)
    d["frozen"] = bool(d["frozen"])
    return d


def list_results(product_id: int, data_snapshot_id: int | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        get_product(conn, product_id)
        q = "SELECT * FROM result_snapshots WHERE product_id=?"
        args: list[Any] = [product_id]
        if data_snapshot_id is not None:
            q += " AND data_snapshot_id=?"
            args.append(data_snapshot_id)
        q += " ORDER BY id"
        out = [dict(r) for r in conn.execute(q, args)]
        for d in out:
            d["frozen"] = bool(d["frozen"])
        return out


def revision_chain(product_id: int, result_snapshot_id: int) -> list[dict[str, Any]]:
    """裁决链：发布判断 -> 各修订版（沿链完整回溯/展开，并校验产品归属）。"""
    with get_conn() as conn:
        node = conn.execute(
            "SELECT * FROM result_snapshots WHERE id=? AND product_id=?",
            (result_snapshot_id, product_id),
        ).fetchone()
        if not node:
            raise DomainError("result snapshot not found")
        # 向上找到发布根
        root = node
        seen: set[int] = set()
        while root["kind"] == "replay" and root["supersedes_id"] and root["id"] not in seen:
            seen.add(root["id"])
            nxt = conn.execute(
                "SELECT * FROM result_snapshots WHERE id=? AND product_id=?",
                (root["supersedes_id"], product_id),
            ).fetchone()
            if not nxt:
                break
            root = nxt
        chain = [dict(root)]
        # 向下按 id 顺序展开全部修订版
        for r in conn.execute(
            "SELECT * FROM result_snapshots"
            " WHERE supersedes_id=? AND product_id=? ORDER BY id",
            (root["id"], product_id),
        ):
            chain.append(dict(r))
        for d in chain:
            d["frozen"] = bool(d["frozen"])
        return chain


# ---------------------------------------------------------------- 固定快照分页

def list_findings_page(
    result_snapshot_id: int,
    offset: int = 0,
    limit: int = config.DEFAULT_PAGE_SIZE,
    rule_code: str | None = None,
    exempt_filter: str | None = None,  # None=全部 | exempted | outstanding
) -> dict[str, Any]:
    """分页只读一个固定结果快照：数据来自 findings，行序由 seq 固定，
    与随后新增的豁免/回放互不干扰。"""
    limit = min(max(1, limit), config.MAX_PAGE_SIZE)
    offset = max(0, offset)
    with get_conn() as conn:
        rs = conn.execute(
            "SELECT id FROM result_snapshots WHERE id=?", (result_snapshot_id,)
        ).fetchone()
        if not rs:
            raise DomainError("result snapshot not found")
        where = ["result_snapshot_id=?"]
        args: list[Any] = [result_snapshot_id]
        if rule_code:
            where.append("rule_code=?")
            args.append(rule_code)
        if exempt_filter == "exempted":
            where.append("exempt_at_execution=1")
        elif exempt_filter == "outstanding":
            where.append("exempt_at_execution=0")
        elif exempt_filter is not None:
            raise DomainError("exempt_filter must be exempted|outstanding")
        wsql = " AND ".join(where)
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM findings WHERE {wsql}", args
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM findings WHERE {wsql} ORDER BY seq LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
        return {
            "result_snapshot_id": result_snapshot_id,
            "snapshot_fixed": True,
            "offset": offset,
            "limit": limit,
            "total": total,
            "items": [_finding_dict(r) for r in rows],
        }


def get_finding(finding_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        if not r:
            raise DomainError("finding not found")
        return _finding_dict(r)


def _finding_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["exempt_at_execution"] = bool(d["exempt_at_execution"])
    return d


# ---------------------------------------------------------------- 异常豁免

def grant_exemption(
    product_id: int,
    data_snapshot_id: int,
    rule_code: str,
    rule_version: int,
    reason: str,
    table_name: str | None = None,
    row_key: Any = None,
    column_name: str = "",
    finding_id: int | None = None,
    created_by: str = "steward",
) -> dict[str, Any]:
    """豁免必须绑定精确样本（快照+表+行键）与规则摘要。

    可用 finding_id 定位，也可显式给出坐标；规则摘要从指定的规则版本读取，
    规则升级后摘要变化，旧豁免不会自动覆盖新规则的失败。"""
    with transaction() as conn:
        get_product(conn, product_id)
        if finding_id is not None:
            fr = conn.execute(
                "SELECT f.*, f2.data_snapshot_id AS dsid FROM findings f"
                " JOIN result_snapshots f2 ON f2.id=f.result_snapshot_id"
                " WHERE f.id=?",
                (finding_id,),
            ).fetchone()
            if not fr:
                raise DomainError("finding not found", status=404)
            data_snapshot_id = fr["dsid"]
            table_name = fr["table_name"]
            row_key = fr["row_key"]
            column_name = fr["column_name"]
            rule_code = fr["rule_code"]
            rule_version = fr["rule_version"]
        snap = conn.execute(
            "SELECT 1 FROM data_snapshots WHERE id=? AND product_id=?",
            (data_snapshot_id, product_id),
        ).fetchone()
        if not snap:
            raise DomainError("data snapshot not found in product", status=404)
        if not table_name or row_key is None:
            raise DomainError(
                "exemption requires precise sample: table_name + row_key",
                status=400)
        norm_key = engine._normalize_row_key(row_key)
        rv = conn.execute(
            "SELECT * FROM rule_versions WHERE product_id=? AND rule_code=? AND version=?",
            (product_id, rule_code, rule_version),
        ).fetchone()
        if not rv:
            raise DomainError(
                f"rule {rule_code} v{rule_version} not found", status=404)

        # 样本必须真实存在于该快照（防止豁免到一个不存在的坐标）
        exists = conn.execute(
            "SELECT 1 FROM snapshot_rows WHERE snapshot_id=? AND table_name=? AND row_key=?",
            (data_snapshot_id, table_name, norm_key),
        ).fetchone()
        if not exists:
            raise DomainError("sample row_key not found in snapshot", status=422)

        try:
            cur = conn.execute(
                "INSERT INTO exemptions(product_id, data_snapshot_id, table_name,"
                " row_key, column_name, rule_code, rule_version, rule_digest,"
                " reason, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (product_id, data_snapshot_id, table_name, norm_key, column_name,
                 rule_code, rule_version, rv["rule_digest"], reason, created_by),
            )
        except sqlite3.IntegrityError:
            raise DomainError(
                "exemption already exists for this exact sample + rule digest",
                status=409,
            ) from None
        return _exemption(conn, cur.lastrowid)


def revoke_exemption(product_id: int, exemption_id: int) -> dict[str, Any]:
    with transaction() as conn:
        r = conn.execute(
            "SELECT * FROM exemptions WHERE id=? AND product_id=?",
            (exemption_id, product_id),
        ).fetchone()
        if not r:
            raise DomainError("exemption not found")
        if r["status"] != "granted":
            raise DomainError("exemption already revoked")
        conn.execute(
            "UPDATE exemptions SET status='revoked', revoked_at=? WHERE id=?",
            (_now(), exemption_id),
        )
        return _exemption(conn, exemption_id)


def list_exemptions(
    product_id: int,
    data_snapshot_id: int | None = None,
    status: str = "granted",
    offset: int = 0,
    limit: int = config.DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    limit = min(max(1, limit), config.MAX_PAGE_SIZE)
    with get_conn() as conn:
        get_product(conn, product_id)
        where = ["product_id=?"]
        args: list[Any] = [product_id]
        if data_snapshot_id is not None:
            where.append("data_snapshot_id=?")
            args.append(data_snapshot_id)
        if status != "all":
            where.append("status=?")
            args.append(status)
        wsql = " AND ".join(where)
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM exemptions WHERE {wsql}", args
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM exemptions WHERE {wsql} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [limit, max(0, offset)],
        ).fetchall()
        return {
            "offset": offset, "limit": limit, "total": total,
            "items": [_exemption_conn(r) for r in rows],
        }


def _exemption(conn: sqlite3.Connection, eid: int) -> dict[str, Any]:
    return _exemption_conn(conn.execute(
        "SELECT * FROM exemptions WHERE id=?", (eid,)
    ).fetchone())


def _exemption_conn(r: sqlite3.Row) -> dict[str, Any]:
    return dict(r)


# ---------------------------------------------------------------- 异常工作台

def workbench(
    product_id: int,
    result_snapshot_id: int,
    offset: int = 0,
    limit: int = config.DEFAULT_PAGE_SIZE,
    status_filter: str | None = None,
) -> dict[str, Any]:
    """异常工作台：在固定结果快照上逐条裁决。

    分页行严格来自该结果快照；exempt_at_execution 是执行时刻冻结的状态，
    current_exempt 是豁免表当前状态（仅注释用），不改变分页集合。"""
    page = list_findings_page(
        result_snapshot_id, offset=offset, limit=limit,
        exempt_filter=("exempted" if status_filter == "exempted"
                       else "outstanding" if status_filter == "outstanding" else None),
    )
    with get_conn() as conn:
        prod = conn.execute("SELECT 1 FROM data_products WHERE id=?", (product_id,)).fetchone()
        if not prod:
            raise DomainError("product not found")
        rs = conn.execute(
            "SELECT * FROM result_snapshots WHERE id=? AND product_id=?",
            (result_snapshot_id, product_id),
        ).fetchone()
        if not rs:
            raise DomainError("result snapshot not found in product")
        current = {
            (r["table_name"], r["row_key"], r["column_name"], r["rule_digest"]): r["id"]
            for r in conn.execute(
                "SELECT id, table_name, row_key, column_name, rule_digest"
                " FROM exemptions WHERE data_snapshot_id=? AND status='granted'",
                (rs["data_snapshot_id"],),
            )
        }
    items = []
    for it in page["items"]:
        key = (it["table_name"], it["row_key"], it["column_name"], it["rule_digest"])
        it["current_exemption_id"] = current.get(key)
        items.append(it)
    page["items"] = items
    rs_dict = dict(rs)
    rs_dict["frozen"] = bool(rs_dict["frozen"])
    page["result"] = rs_dict
    return page


# ---------------------------------------------------------------- 质量凭据

def issue_credential(product_id: int, result_snapshot_id: int) -> dict[str, Any]:
    """为结果快照签发不可变质量凭据文件（自描述、带 SHA-256，可离线验真）。

    回放产生的修订版凭据 kind=replay，明确标注 supersedes，不冒充当时发布判断。"""
    with transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM credentials WHERE result_snapshot_id=?",
            (result_snapshot_id,),
        ).fetchone()
        if existing:
            return dict(existing)
        text = _render_credential(conn, product_id, result_snapshot_id)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        seq = conn.execute(
            "SELECT COUNT(*) AS c FROM credentials"
        ).fetchone()["c"] + 1
        ref = f"QC-P{product_id}-{seq:06d}-R{result_snapshot_id}"
        path = config.CREDENTIAL_DIR / f"{ref}.txt"
        path.write_text(text, encoding="utf-8")
        cur = conn.execute(
            "INSERT INTO credentials(result_snapshot_id, credential_ref, file_path, sha256)"
            " VALUES (?,?,?,?)",
            (result_snapshot_id, ref, str(path), sha),
        )
        return dict(conn.execute(
            "SELECT * FROM credentials WHERE id=?", (cur.lastrowid,)
        ).fetchone())


def _render_credential(conn: sqlite3.Connection, product_id: int,
                       result_snapshot_id: int) -> str:
    rs = conn.execute(
        "SELECT * FROM result_snapshots WHERE id=? AND product_id=?",
        (result_snapshot_id, product_id),
    ).fetchone()
    if not rs:
        raise DomainError("result snapshot not found in product")
    snap = conn.execute(
        "SELECT * FROM data_snapshots WHERE id=?", (rs["data_snapshot_id"],)
    ).fetchone()
    product = conn.execute(
        "SELECT * FROM data_products WHERE id=?", (product_id,)
    ).fetchone()
    lines: list[str] = []
    lines.append("DATA QUALITY CREDENTIAL")
    lines.append("=" * 64)
    lines.append(f"Product:          {product['name']} (id={product_id})")
    lines.append(f"Data Snapshot:    {snap['snapshot_ref']} (id={snap['id']}, origin={snap['origin']})")
    lines.append(f"Result Snapshot:  id={rs['id']} kind={rs['kind']} frozen={bool(rs['frozen'])}")
    if rs["supersedes_id"]:
        lines.append(f"Supersedes:       result snapshot id={rs['supersedes_id']}"
                     "  (revision; original judgment unchanged)")
    lines.append(f"Verdict:          {rs['verdict'].upper()}")
    lines.append(f"Rules Executed:   {rs['total_rules']}  (rule-set sha256={rs['rule_set_sha256'][:16]}…)")
    lines.append(f"Rows Scanned:     {rs['total_rows']}")
    lines.append(f"Failures:         {rs['failure_count']}")
    lines.append(f"Executor:         {rs['executor']}")
    lines.append(f"Note:             {rs['note']}")
    lines.append(f"Issued At (UTC):  {_now()}")
    lines.append("")
    lines.append("Rule Versions Used")
    lines.append("-" * 64)
    digests = {
        r["rule_digest"]
        for r in conn.execute(
            "SELECT DISTINCT rule_digest FROM findings WHERE result_snapshot_id=?",
            (result_snapshot_id,),
        )
    }
    used_ids = [r["rule_version_id"] for r in conn.execute(
        "SELECT DISTINCT rule_version_id FROM findings WHERE result_snapshot_id=?",
        (result_snapshot_id,),
    )]
    if used_ids:
        q = f"SELECT * FROM rule_versions WHERE id IN ({','.join('?' * len(used_ids))}) ORDER BY rule_code, version"
        for r in conn.execute(q, used_ids):
            lines.append(
                f"  {r['rule_code']} v{r['version']} [{r['rule_type']}/{r['table_name']}]"
                f" {r['name']} digest={r['rule_digest'][:16]}…"
            )
    else:
        lines.append("  (no rule produced a failure; full rule-set fingerprint above attests execution)")
    lines.append("")
    lines.append("Findings (fixed sample locations)")
    lines.append("-" * 64)
    for r in conn.execute(
        "SELECT * FROM findings WHERE result_snapshot_id=? ORDER BY seq LIMIT 1000",
        (result_snapshot_id,),
    ):
        tag = "EXEMPT@EXEC" if r["exempt_at_execution"] else "FAILED"
        lines.append(
            f"  #{r['seq']:04d} [{tag}] {r['rule_code']} v{r['rule_version']}"
            f" {r['table_name']}:{r['row_key']}"
            f"{('.' + r['column_name']) if r['column_name'] else ''} :: {r['message']}"
        )
    if rs["failure_count"] > 1000:
        lines.append(f"  … {rs['failure_count'] - 1000} more findings omitted, see API")
    body = "\n".join(lines) + "\n"
    seal = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return body + "-" * 64 + f"\nSeal SHA-256: {seal}\n"


def verify_credential_file(path: str) -> dict[str, Any]:
    """离线验真：
    - 内嵌封条必须等于正文哈希（文件自洽）；
    - 整文件哈希必须等于签发时登记的 sha256（与库存一致，可检出篡改）。"""
    text = open(path, encoding="utf-8").read()
    marker = "Seal SHA-256: "
    idx = text.rfind(marker)
    if idx < 0:
        raise DomainError("not a credential file: missing seal")
    seal = text[idx + len(marker):].strip()
    body_end = text.rfind("-" * 64)
    body = text[: body_end]
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM credentials WHERE file_path=?", (path,)
        ).fetchone()
    registered_sha = row["sha256"] if row else None
    return {
        "seal": seal,
        "body_sha256": body_hash,
        "file_sha256": file_hash,
        "seal_self_consistent": seal == body_hash,
        "registered": dict(row) if row else None,
        "valid": seal == body_hash and registered_sha == file_hash,
    }


# ---------------------------------------------------------------- 入库辅助

def _validate_tables(tables: dict[str, list[dict[str, Any]]],
                     key_columns: dict[str, list[str]]) -> None:
    if not isinstance(tables, dict) or not tables:
        raise DomainError("tables must be a non-empty object {table: [rows]}")
    for table, rows in tables.items():
        if not engine.IDENT_RE.match(table):
            raise DomainError(f"illegal table name: {table!r}")
        if not isinstance(rows, list):
            raise DomainError(f"rows of {table} must be a list")
        kc = key_columns.get(table)
        for row in rows:
            if not isinstance(row, dict):
                raise DomainError(f"each row in {table} must be an object")
        if kc is not None:
            if not kc or not all(isinstance(c, str) for c in kc):
                raise DomainError(f"key_columns[{table}] must be string list")


def _infer_key_columns(table: str, rows: list[dict[str, Any]]) -> list[str]:
    if rows and isinstance(rows[0], dict) and "id" in rows[0]:
        return ["id"]
    raise DomainError(
        f"cannot infer business key for table {table!r}; "
        "provide key_columns or include an 'id' field"
    )


def _derive_key_columns(
    table: str, existing: list[tuple[str, dict[str, Any]]]
) -> list[str] | None:
    """从旧快照既有 row_key（规范 JSON）反推主键列名。"""
    for rk, _ in existing:
        try:
            parsed = json.loads(rk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed and all(
            isinstance(k, str) for k in parsed
        ):
            return sorted(parsed.keys())
    return None


def _row_key_for(table: str, row: Any, key_columns: list[str]) -> str:
    if isinstance(row, dict):
        if "_row_key" in row:
            return engine._normalize_row_key(row["_row_key"])
        missing = [c for c in key_columns if c not in row]
        if missing:
            raise DomainError(
                f"row in {table} missing key columns {missing}"
            )
        key = {c: row[c] for c in key_columns}
        return engine.canonical_json(key)
    # 直接给出的键值
    if len(key_columns) == 1:
        return engine.canonical_json({key_columns[0]: row})
    if isinstance(row, dict):
        return engine.canonical_json(row)
    raise DomainError(f"cannot build row key for {table}: {row!r}")


def _insert_rows(conn: sqlite3.Connection, snapshot_id: int,
                 tables: dict[str, list[dict[str, Any]]],
                 key_columns: dict[str, list[str]]) -> None:
    for table, rows in tables.items():
        kc = key_columns.get(table) or _infer_key_columns(table, rows)
        for payload in rows:
            rk = _row_key_for(table, payload, kc)
            conn.execute(
                "INSERT INTO snapshot_rows(snapshot_id, table_name, row_key, payload)"
                " VALUES (?,?,?,?)",
                (snapshot_id, table, rk, engine.canonical_json(payload)),
            )
