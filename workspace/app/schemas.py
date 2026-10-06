"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""


class SnapshotIn(BaseModel):
    snapshot_ref: str = Field(min_length=1, max_length=120)
    # {表名: [行对象]}
    tables: dict[str, list[dict[str, Any]]]
    # {表名: [主键列]}
    key_columns: dict[str, list[str]] = Field(default_factory=dict)
    note: str = ""


class RepairIn(BaseModel):
    source_snapshot_id: int
    new_snapshot_ref: str = Field(min_length=1, max_length=120)
    edits: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    deletes: dict[str, list[Any]] = Field(default_factory=dict)
    key_columns: dict[str, list[str]] = Field(default_factory=dict)
    note: str = ""


class RuleIn(BaseModel):
    rule_code: str = Field(min_length=1, max_length=64)  # 稳定业务规则标识
    engine_code: str | None = None  # 缺省与 rule_code 相同；须为受支持算子
    name: str = Field(min_length=1, max_length=120)
    rule_type: str  # field | constraint | cross_table
    table_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    severity: str = "error"
    created_by: str = "system"


class ExecuteIn(BaseModel):
    data_snapshot_id: int
    rule_codes: list[str] | None = None
    executor: str = "scheduler"
    note: str = ""


class ReplayIn(BaseModel):
    data_snapshot_id: int
    supersedes_id: int
    rule_codes: list[str] | None = None
    executor: str = "scheduler"
    note: str = ""


class ExemptionIn(BaseModel):
    data_snapshot_id: int | None = None
    table_name: str | None = None
    row_key: Any | None = None
    column_name: str = ""
    rule_code: str | None = None
    rule_version: int | None = None
    finding_id: int | None = None
    reason: str = Field(min_length=1, max_length=500)
    created_by: str = "steward"
