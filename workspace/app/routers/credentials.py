"""可下载质量凭据：签发、下载、验真。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from .. import service

router = APIRouter(prefix="/api/v1", tags=["credentials"])


@router.post("/products/{product_id}/results/{result_snapshot_id}/credential",
             status_code=201)
def issue_credential(product_id: int, result_snapshot_id: int):
    try:
        cred = service.issue_credential(product_id, result_snapshot_id)
    except service.DomainError as exc:
        raise HTTPException(404, str(exc)) from None
    cred = dict(cred)
    cred["download_url"] = (
        f"/api/v1/results/{result_snapshot_id}/credential/download"
    )
    cred.pop("file_path", None)
    return cred


@router.get("/results/{result_snapshot_id}/credential/download",
            response_class=PlainTextResponse)
def download_credential(result_snapshot_id: int):
    try:
        with service.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM credentials WHERE result_snapshot_id=?",
                (result_snapshot_id,),
            ).fetchone()
    except Exception:
        row = None
    if not row:
        raise HTTPException(
            404, "credential not issued yet; POST .../credential first"
        )
    path = Path(row["file_path"])
    if not path.exists():
        raise HTTPException(410, "credential file missing on disk")
    return PlainTextResponse(
        path.read_text(encoding="utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{row["credential_ref"]}.txt"'
        },
    )


@router.get("/results/{result_snapshot_id}/credential/verify")
def verify_credential(result_snapshot_id: int):
    """对已签发凭据重算封条哈希，验证文件未被篡改。"""
    with service.get_conn() as conn:
        row = conn.execute(
            "SELECT file_path FROM credentials WHERE result_snapshot_id=?",
            (result_snapshot_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "credential not issued")
    try:
        result = service.verify_credential_file(row["file_path"])
    except service.DomainError as exc:
        raise HTTPException(422, str(exc)) from None
    registered = result.pop("registered", None)
    if registered:
        result["registered_sha256"] = registered["sha256"]
        result["credential_ref"] = registered["credential_ref"]
        result["valid"] = (
            result["seal_self_consistent"]
            and result["file_sha256"] == registered["sha256"]
        )
    return result
