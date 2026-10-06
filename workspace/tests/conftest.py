"""pytest 公共夹具：每个测试使用独立临时数据库。"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    cred_dir = tmp_path / "credentials"
    monkeypatch.setenv("DQDB_PATH", str(db_file))
    monkeypatch.setenv("DQCRED_DIR", str(cred_dir))

    from app import config, db as db_mod, main as main_mod
    importlib.reload(config)
    importlib.reload(db_mod)
    # service / engine 通过模块属性延迟引用 config/db，reload 后重新绑定
    importlib.reload(importlib.import_module("app.service"))
    importlib.reload(main_mod)
    db_mod.init_db()

    with TestClient(main_mod.app) as c:
        yield c


# ---------------------------------------------------------- 构造辅助

ORDERS = [
    {"id": "O-1", "customer_id": "C-1", "amount": 100, "status": "paid", "email": "a@x.com"},
    {"id": "O-2", "customer_id": "C-2", "amount": -5, "status": "paid", "email": "bad-email"},
    {"id": "O-3", "customer_id": None, "amount": 9999, "status": "weird", "email": "c@x.com"},
]
CUSTOMERS = [
    {"id": "C-1", "name": "Alice", "tier": "gold"},
    {"id": "C-2", "name": "Bob", "tier": "silver"},
]


def make_product(client, name="orders-product"):
    r = client.post("/api/v1/products", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_snapshot(client, pid, ref="snap-1", orders=None, customers=None):
    body = {
        "snapshot_ref": ref,
        "tables": {
            "orders": orders if orders is not None else ORDERS,
            "customers": customers if customers is not None else CUSTOMERS,
        },
        "key_columns": {"orders": ["id"], "customers": ["id"]},
    }
    r = client.post(f"/api/v1/products/{pid}/snapshots", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_rule(client, pid, code, rtype, table, params, name=None,
             severity="error", engine_code=None):
    body = {
        "rule_code": code,
        "engine_code": engine_code or _ENGINE_FOR.get(code, code),
        "name": name or code.title(),
        "rule_type": rtype,
        "table_name": table,
        "params": params,
        "severity": severity,
    }
    r = client.post(f"/api/v1/products/{pid}/rules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# 业务规则编码 -> 引擎算子
_ENGINE_FOR = {
    "ORDER_CUSTOMER_NOT_NULL": "NOT_NULL",
    "ORDER_AMOUNT_RANGE": "RANGE",
    "ORDER_STATUS_ENUM": "ENUM",
    "ORDER_EMAIL_REGEX": "REGEX",
    "ORDER_FK_CUSTOMER": "REFERENTIAL_INTEGRITY",
    "HIGH_VALUE_PAIR": "SQL_ASSERTION",
}


def standard_rules(client, pid):
    """一组覆盖字段/约束/跨表三类的 v1 规则。"""
    add_rule(client, pid, "ORDER_CUSTOMER_NOT_NULL", "field", "orders",
             {"column": "customer_id"}, "订单必须有客户")
    add_rule(client, pid, "ORDER_AMOUNT_RANGE", "constraint", "orders",
             {"column": "amount", "min": 0, "max": 1000}, "金额范围")
    add_rule(client, pid, "ORDER_STATUS_ENUM", "constraint", "orders",
             {"column": "status", "values": ["paid", "pending", "refunded"]},
             "状态枚举")
    add_rule(client, pid, "ORDER_EMAIL_REGEX", "field", "orders",
             {"column": "email", "pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
             "邮箱格式", severity="warning")
    add_rule(client, pid, "ORDER_FK_CUSTOMER", "cross_table", "orders",
             {"child_columns": ["customer_id"], "parent_table": "customers",
              "parent_columns": ["id"]}, "订单客户引用完整性")
