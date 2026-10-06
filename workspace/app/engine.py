"""规则引擎：规则摘要计算与在内存中对不可变快照执行校验。

设计要点：
- 规则摘要 rule_digest 覆盖规则语义（code/type/table/规范化参数），
  豁免绑定该摘要；规则升级（参数变化）会得到不同摘要，旧豁免不会误覆盖新规则。
- 执行结果只由「数据快照 + 一组确定的规则版本」决定，天然可回放。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SUPPORTED_CODES = {
    "NOT_NULL",
    "REGEX",
    "RANGE",
    "ENUM",
    "UNIQUE",
    "REFERENTIAL_INTEGRITY",
    "SQL_ASSERTION",
}


class RuleDefinitionError(ValueError):
    """规则参数不合法。"""


class RuleExecutionError(RuntimeError):
    """规则在快照上执行失败（如 SQL 断言语法错误）。"""


def canonical_json(obj: Any) -> str:
    """稳定序列化：键排序、无空白、保留 Unicode，用于哈希与凭据。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _semantic_payload(definition: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule_code": definition["rule_code"],
        "engine_code": definition["engine_code"],
        "rule_type": definition["rule_type"],
        "table_name": definition["table_name"],
        "params": definition["params"],
    }


def rule_digest(definition: dict[str, Any]) -> str:
    """规则语义摘要（豁免绑定对象）。"""
    return hashlib.sha256(
        canonical_json(_semantic_payload(definition)).encode("utf-8")
    ).hexdigest()


def body_sha256(definition: dict[str, Any]) -> str:
    """完整规则定义体指纹（含名称、严重级，用于质量凭据）。"""
    body = {
        "rule_code": definition["rule_code"],
        "engine_code": definition["engine_code"],
        "name": definition["name"],
        "rule_type": definition["rule_type"],
        "table_name": definition["table_name"],
        "params": definition["params"],
        "severity": definition["severity"],
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def validate_definition(definition: dict[str, Any]) -> None:
    code = definition.get("engine_code")
    rtype = definition.get("rule_type")
    table = definition.get("table_name")
    params = definition.get("params") or {}
    if not isinstance(definition.get("rule_code"), str) or not definition["rule_code"]:
        raise RuleDefinitionError("rule_code (stable business id) is required")
    if code not in SUPPORTED_CODES:
        raise RuleDefinitionError(f"unsupported rule_code: {code!r}")
    if rtype not in {"field", "constraint", "cross_table"}:
        raise RuleDefinitionError(f"unsupported rule_type: {rtype!r}")
    if not isinstance(table, str) or not IDENT_RE.match(table):
        raise RuleDefinitionError("table_name must be a legal SQL identifier")
    if not isinstance(params, dict):
        raise RuleDefinitionError("params must be an object")
    if definition.get("severity", "error") not in {"error", "warning"}:
        raise RuleDefinitionError("severity must be error|warning")

    if code == "NOT_NULL":
        _need_column(params)
    elif code == "REGEX":
        _need_column(params)
        pattern = params.get("pattern")
        if not isinstance(pattern, str):
            raise RuleDefinitionError("REGEX requires string params.pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RuleDefinitionError(f"invalid regex: {exc}") from None
    elif code == "RANGE":
        _need_column(params)
        if "min" not in params and "max" not in params:
            raise RuleDefinitionError("RANGE requires params.min and/or params.max")
    elif code == "ENUM":
        _need_column(params)
        values = params.get("values")
        if not isinstance(values, list) or not values:
            raise RuleDefinitionError("ENUM requires non-empty params.values list")
    elif code == "UNIQUE":
        cols = params.get("columns")
        if not isinstance(cols, list) or not cols or not all(
            isinstance(c, str) for c in cols
        ):
            raise RuleDefinitionError("UNIQUE requires params.columns string list")
    elif code == "REFERENTIAL_INTEGRITY":
        for key in ("child_columns", "parent_table", "parent_columns"):
            if not params.get(key):
                raise RuleDefinitionError(
                    f"REFERENTIAL_INTEGRITY requires params.{key}"
                )
        if not IDENT_RE.match(params["parent_table"]):
            raise RuleDefinitionError("parent_table must be an identifier")
        if len(params["child_columns"]) != len(params["parent_columns"]):
            raise RuleDefinitionError("child/parent column counts must match")
    elif code == "SQL_ASSERTION":
        sql = params.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise RuleDefinitionError("SQL_ASSERTION requires params.sql")


def _need_column(params: dict[str, Any]) -> None:
    col = params.get("column")
    if not isinstance(col, str) or not col:
        raise RuleDefinitionError("params.column is required")


@dataclass(frozen=True)
class Failure:
    rule_version_id: int
    rule_code: str
    engine_code: str
    rule_version: int
    rule_digest: str
    severity: str
    table_name: str
    row_key: str  # 规范化后的 JSON 主键
    column_name: str
    observed_value: str
    message: str


@dataclass
class LoadedRule:
    id: int
    rule_code: str
    engine_code: str
    version: int
    name: str
    rule_type: str
    table_name: str
    params: dict[str, Any]
    severity: str
    rule_digest: str


def _normalize_row_key(row_key: Any) -> str:
    if isinstance(row_key, str):
        # 接受客户端传入的 JSON 字符串，统一成规范形式
        try:
            parsed = json.loads(row_key)
            return canonical_json(parsed)
        except json.JSONDecodeError:
            return canonical_json({"_key": row_key})
    return canonical_json(row_key)


def _store_value(value: Any) -> Any:
    """JSON 值 -> SQLite 存储值。"""
    if value is None or isinstance(value, (int, float, str)):
        return value
    return canonical_json(value)


def build_inmemory_db(tables: dict[str, list[tuple[str, dict[str, Any]]]]) -> sqlite3.Connection:
    """把快照全部行装入内存库；每张表附带 __row_key 列供断言 SQL 精确定位样本。"""
    mem = sqlite3.connect(":memory:")
    for table, rows in tables.items():
        columns: set[str] = set()
        for _, payload in rows:
            columns.update(payload.keys())
        ordered = sorted(columns)
        quoted = ", ".join(f'"{c}" TEXT' for c in ordered)
        mem.execute(
            f'CREATE TABLE "{table}" (__row_key TEXT NOT NULL, {quoted})'
        )
        for raw_key, payload in rows:
            placeholders = ", ".join(["?"] * (len(ordered) + 1))
            mem.execute(
                f'INSERT INTO "{table}" (__row_key, {", ".join(chr(34) + c + chr(34) for c in ordered)}) '
                f"VALUES ({placeholders})",
                [raw_key] + [_store_value(payload.get(c)) for c in ordered],
            )
    return mem


def evaluate(
    rules: list[LoadedRule],
    rows_by_table: dict[str, list[tuple[str, dict[str, Any]]]],
) -> list[Failure]:
    """对规则集执行，返回原始失败列表（豁免由上层裁决时叠加）。"""
    failures: list[Failure] = []
    mem: sqlite3.Connection | None = None
    try:
        for rule in rules:
            rows = rows_by_table.get(rule.table_name, [])
            p = rule.params
            if rule.engine_code == "NOT_NULL":
                col = p["column"]
                for rk, payload in rows:
                    if payload.get(col) is None:
                        failures.append(_f(rule, rule.table_name, rk, col, None,
                                           f"column {col!r} is null"))
            elif rule.engine_code == "REGEX":
                col, pattern = p["column"], p["pattern"]
                rx = re.compile(pattern)
                for rk, payload in rows:
                    v = payload.get(col)
                    if v is not None and not rx.match(str(v)):
                        failures.append(_f(rule, rule.table_name, rk, col, v,
                                           f"value {v!r} does not match {pattern!r}"))
            elif rule.engine_code == "RANGE":
                col = p["column"]
                lo, hi = p.get("min"), p.get("max")
                for rk, payload in rows:
                    v = payload.get(col)
                    if v is None:
                        continue
                    try:
                        num = float(v)
                    except (TypeError, ValueError):
                        failures.append(_f(rule, rule.table_name, rk, col, v,
                                           f"value {v!r} is not numeric"))
                        continue
                    if lo is not None and num < float(lo):
                        failures.append(_f(rule, rule.table_name, rk, col, v,
                                           f"value {v} < min {lo}"))
                    elif hi is not None and num > float(hi):
                        failures.append(_f(rule, rule.table_name, rk, col, v,
                                           f"value {v} > max {hi}"))
            elif rule.engine_code == "ENUM":
                col, allowed = p["column"], set(p["values"])
                for rk, payload in rows:
                    v = payload.get(col)
                    if v is not None and v not in allowed:
                        failures.append(_f(rule, rule.table_name, rk, col, v,
                                           f"value {v!r} not in {sorted(allowed)}"))
            elif rule.engine_code == "UNIQUE":
                cols = p["columns"]
                seen: dict[tuple, str] = {}
                dup_keys: set[str] = set()
                for rk, payload in rows:
                    sig = tuple(canonical_json(payload.get(c)) for c in cols)
                    if sig in seen:
                        dup_keys.add(rk)
                        dup_keys.add(seen[sig])
                    else:
                        seen[sig] = rk
                for rk in sorted(dup_keys):
                    failures.append(_f(rule, rule.table_name, rk, ",".join(cols), "",
                                       f"duplicate key on columns {cols}"))
            elif rule.engine_code == "REFERENTIAL_INTEGRITY":
                child_cols = p["child_columns"]
                parent_table = p["parent_table"]
                parent_cols = p["parent_columns"]
                parent_rows = rows_by_table.get(parent_table, [])
                parent_index = {
                    tuple(payload.get(c) for c in parent_cols)
                    for _, payload in parent_rows
                }
                for rk, payload in rows:
                    sig = tuple(payload.get(c) for c in child_cols)
                    if sig not in parent_index:
                        failures.append(_f(
                            rule, rule.table_name, rk, ",".join(child_cols),
                            canonical_json(sig),
                            f"no matching parent {parent_table}({parent_cols})",
                        ))
            elif rule.engine_code == "SQL_ASSERTION":
                if mem is None:
                    mem = build_inmemory_db(rows_by_table)
                failures.extend(_run_sql_assertion(mem, rule))
    finally:
        if mem is not None:
            mem.close()

    failures.sort(key=lambda f: (f.rule_version_id, f.table_name, f.row_key,
                                 f.column_name))
    return failures


def _run_sql_assertion(mem: sqlite3.Connection, rule: LoadedRule) -> list[Failure]:
    try:
        cur = mem.execute(rule.params["sql"])
        cols = [d[0] for d in cur.description] if cur.description else []
        result_rows = cur.fetchall()
    except sqlite3.Error as exc:
        raise RuleExecutionError(
            f"SQL_ASSERTION {rule.rule_code} v{rule.version} failed: {exc}"
        ) from None
    if "row_key" not in cols:
        raise RuleExecutionError(
            "SQL_ASSERTION must return a column named row_key "
            "(tip: select __row_key AS row_key)"
        )
    out: list[Failure] = []
    ki = cols.index("row_key")
    ti = cols.index("table_name") if "table_name" in cols else None
    ci = cols.index("column_name") if "column_name" in cols else None
    mi = cols.index("message") if "message" in cols else None
    vi = cols.index("observed_value") if "observed_value" in cols else None
    for r in result_rows:
        rk = _normalize_row_key(r[ki])
        table = str(r[ti]) if ti is not None and r[ti] is not None else rule.table_name
        column = str(r[ci]) if ci is not None and r[ci] is not None else ""
        message = str(r[mi]) if mi is not None and r[mi] is not None else "SQL assertion failed"
        observed = "" if r[vi] is None or vi is None else str(r[vi])
        out.append(_f(rule, table, rk, column, observed, message))
    return out


def _f(rule: LoadedRule, table: str, row_key: str, column: str,
       observed: Any, message: str) -> Failure:
    return Failure(
        rule_version_id=rule.id,
        rule_code=rule.rule_code,
        engine_code=rule.engine_code,
        rule_version=rule.version,
        rule_digest=rule.rule_digest,
        severity=rule.severity,
        table_name=table,
        row_key=row_key if isinstance(row_key, str) else _normalize_row_key(row_key),
        column_name=column,
        observed_value="" if observed is None else str(observed),
        message=message,
    )
