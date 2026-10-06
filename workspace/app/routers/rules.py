"""规则 API、数据快照、版本化执行与回放。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import service
from ..engine import RuleDefinitionError, RuleExecutionError
from ..schemas import ExecuteIn, ProductIn, RepairIn, ReplayIn, RuleIn, SnapshotIn

router = APIRouter(prefix="/api/v1", tags=["rules"])


def _error(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


# ------------------------------------------------------------ 产品

@router.post("/products", status_code=201)
def create_product(body: ProductIn):
    try:
        return service.create_product(body.name, body.description)
    except service.DomainError as exc:
        raise _error(exc, exc.status or 409) from None


@router.get("/products")
def list_products():
    return service.list_products()


@router.get("/products/{product_id}")
def get_product(product_id: int):
    try:
        return service.fetch_product(product_id)
    except service.DomainError:
        raise HTTPException(404, "product not found") from None


# ------------------------------------------------------------ 数据快照（固定发布字段）

@router.post("/products/{product_id}/snapshots", status_code=201)
def publish_snapshot(product_id: int, body: SnapshotIn):
    try:
        return service.publish_snapshot(
            product_id, body.snapshot_ref, body.tables, body.key_columns, body.note
        )
    except service.DomainError as exc:
        raise _error(exc, 409) from None


@router.get("/products/{product_id}/snapshots")
def list_snapshots(product_id: int):
    try:
        return service.list_snapshots(product_id)
    except service.DomainError:
        raise HTTPException(404, "product not found") from None


@router.post("/products/{product_id}/snapshots/repair", status_code=201)
def repair_snapshot(product_id: int, body: RepairIn):
    """数据修复：旧快照保持不可变，派生一份新快照。"""
    try:
        return service.repair_snapshot(
            product_id, body.source_snapshot_id, body.new_snapshot_ref,
            body.edits, body.deletes, body.key_columns, body.note,
        )
    except service.DomainError as exc:
        raise _error(exc, 409) from None


@router.get("/snapshots/{snapshot_id}/rows")
def snapshot_rows(snapshot_id: int, table: str | None = None):
    try:
        return service.get_snapshot_rows(snapshot_id, table)
    except service.DomainError:
        raise HTTPException(404, "snapshot not found") from None


# ------------------------------------------------------------ 规则版本

@router.post("/products/{product_id}/rules", status_code=201)
def create_rule(product_id: int, body: RuleIn):
    try:
        return service.create_rule_version(
            product_id, body.rule_code, body.name, body.rule_type,
            body.table_name, body.params, body.engine_code,
            body.severity, body.created_by,
        )
    except RuleDefinitionError as exc:
        raise _error(exc, 422) from None
    except service.DomainError as exc:
        raise _error(exc, 404) from None


@router.get("/products/{product_id}/rules")
def list_rules(product_id: int, rule_code: str | None = None):
    try:
        return service.list_rule_versions(product_id, rule_code)
    except service.DomainError:
        raise HTTPException(404, "product not found") from None


@router.post("/products/{product_id}/rules/{rule_code}/deprecate")
def deprecate_rule(product_id: int, rule_code: str):
    try:
        return service.deprecate_rule(product_id, rule_code)
    except service.DomainError as exc:
        raise _error(exc, 404) from None


# ------------------------------------------------------------ 执行 / 回放 / 冻结

@router.post("/products/{product_id}/execute", status_code=201)
def execute_publish(product_id: int, body: ExecuteIn):
    """对当前数据快照按现行规则版本执行，形成并冻结发布判断。"""
    try:
        return service.execute_publish(
            product_id, body.data_snapshot_id, body.rule_codes,
            body.executor, body.note,
        )
    except RuleExecutionError as exc:
        raise _error(exc, 422) from None
    except service.DomainError as exc:
        raise _error(exc, 409) from None


@router.post("/products/{product_id}/replay", status_code=201)
def replay(product_id: int, body: ReplayIn):
    """升级规则后对历史快照回放，只产生修订版，不改写当时发布判断。"""
    try:
        return service.replay(
            product_id, body.data_snapshot_id, body.supersedes_id,
            body.rule_codes, body.executor, body.note,
        )
    except RuleExecutionError as exc:
        raise _error(exc, 422) from None
    except service.DomainError as exc:
        raise _error(exc, 409) from None


@router.post("/products/{product_id}/results/{result_snapshot_id}/freeze")
def freeze_result(product_id: int, result_snapshot_id: int):
    try:
        return service.freeze_result(product_id, result_snapshot_id)
    except service.DomainError as exc:
        raise _error(exc, 409) from None


@router.get("/products/{product_id}/results")
def list_results(product_id: int, data_snapshot_id: int | None = None):
    try:
        return service.list_results(product_id, data_snapshot_id)
    except service.DomainError:
        raise HTTPException(404, "product not found") from None


@router.get("/products/{product_id}/results/{result_snapshot_id}/chain")
def revision_chain(product_id: int, result_snapshot_id: int):
    try:
        return service.revision_chain(product_id, result_snapshot_id)
    except service.DomainError as exc:
        raise _error(exc, 404) from None


@router.get("/results/{result_snapshot_id}")
def get_result(result_snapshot_id: int):
    try:
        return service.get_result(result_snapshot_id)
    except service.DomainError:
        raise HTTPException(404, "result snapshot not found") from None
