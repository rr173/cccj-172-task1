"""异常工作台：固定结果快照分页、豁免绑定精确样本+规则摘要。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import config, service
from ..schemas import ExemptionIn

router = APIRouter(prefix="/api/v1", tags=["workbench"])


@router.get("/results/{result_snapshot_id}/findings")
def list_findings(
    result_snapshot_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    rule_code: str | None = None,
    exempt: str | None = Query(None, pattern="^(exempted|outstanding)$"),
):
    """分页只读取一个固定结果快照（seq 固定行序），与后续豁免/回放隔离。"""
    try:
        return service.list_findings_page(
            result_snapshot_id, offset, limit, rule_code, exempt
        )
    except service.DomainError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/findings/{finding_id}")
def get_finding(finding_id: int):
    try:
        return service.get_finding(finding_id)
    except service.DomainError:
        raise HTTPException(404, "finding not found") from None


@router.get("/products/{product_id}/workbench/{result_snapshot_id}")
def workbench(
    product_id: int,
    result_snapshot_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    status: str | None = Query(None, pattern="^(exempted|outstanding)$"),
):
    """异常工作台：结果快照头信息 + 固定分页的失败样本 + 当前豁免标注。"""
    try:
        return service.workbench(
            product_id, result_snapshot_id, offset, limit, status
        )
    except service.DomainError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/products/{product_id}/exemptions", status_code=201)
def grant_exemption(product_id: int, body: ExemptionIn):
    """豁免必须绑定精确样本与规则摘要（可用 finding_id 一键绑定）。"""
    try:
        return service.grant_exemption(
            product_id,
            data_snapshot_id=body.data_snapshot_id or 0,
            rule_code=body.rule_code or "",
            rule_version=body.rule_version or 0,
            reason=body.reason,
            table_name=body.table_name,
            row_key=body.row_key,
            column_name=body.column_name,
            finding_id=body.finding_id,
            created_by=body.created_by,
        )
    except service.DomainError as exc:
        raise HTTPException(exc.status or 400, str(exc)) from None


@router.post("/products/{product_id}/exemptions/{exemption_id}/revoke")
def revoke_exemption(product_id: int, exemption_id: int):
    try:
        return service.revoke_exemption(product_id, exemption_id)
    except service.DomainError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/products/{product_id}/exemptions")
def list_exemptions(
    product_id: int,
    data_snapshot_id: int | None = None,
    status_filter: str = Query("granted", alias="status"),
    offset: int = Query(0, ge=0),
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
):
    try:
        return service.list_exemptions(
            product_id, data_snapshot_id, status_filter, offset, limit
        )
    except service.DomainError:
        raise HTTPException(404, "product not found") from None
