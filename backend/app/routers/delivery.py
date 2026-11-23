"""交付接口：位点提交、重试、导出清单与成品下载。

这些接口把“位点确认进度”和“实际可下载成品”分开暴露：提交不等于交付，
未准备好的下载返回可继续处理的 409 状态，而不是给出一个对不上的文件。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from app.services import delivery, faults
from app.services.collect_checkpoint import CollectCheckpointService
from app.services.format_output import FormatOutputService

router = APIRouter(prefix="/api/delivery", tags=["可恢复交付"])


def _services() -> tuple[CollectCheckpointService, FormatOutputService]:
    return CollectCheckpointService(), FormatOutputService()


# -- 故障注入：复现“提交成功但文件生成中断”，重试时自动恢复 --

@router.get("/faults")
def list_faults() -> dict[str, Any]:
    return {"faults": faults.current_faults()}


@router.post("/faults")
def arm_fault(
    target: str = Query(default="format_output"),
    mode: str = Query(default="interrupt"),
    after_lines: int | None = Query(default=None, ge=0),
    error: str = Query(default=""),
    times: int = Query(default=1, ge=1),
) -> dict[str, Any]:
    """安排下一次文件生成中断（一次性）。target 目前支持 format_output。"""
    plan = faults.arm_fault(
        target, mode=mode, after_lines=after_lines, error=error, times=times)
    return {"ok": True, "target": target, "plan": plan}


@router.delete("/faults")
def clear_faults() -> dict[str, Any]:
    faults.clear_faults()
    return {"ok": True, "cleared": True}


# -- 交付主链路 --

@router.get("/{source_key}/manifest")
def get_manifest(source_key: str) -> dict[str, Any]:
    """重新打开也能核对的导出清单：确认范围、待办范围、成品与下载记录。"""
    _, exporter = _services()
    with delivery.source_lock(source_key):
        return exporter.get_export_manifest(source_key)


@router.post("/{source_key}/commit")
def commit_checkpoint(
    source_key: str,
    format_type: str = Query(default="jsonl"),
) -> dict[str, Any]:
    """提交当前位点：先生成并校验成品，成功后才确认交付。"""
    checkpoint, _ = _services()
    return checkpoint.commit_checkpoint(source_key, format_type)


@router.post("/{source_key}/retry")
def retry_delivery(
    source_key: str,
    format_type: str = Query(default=""),
) -> dict[str, Any]:
    """重试未完成的交付记录；已确认过的同一行范围不会重复确认。"""
    checkpoint, _ = _services()
    return checkpoint.retry_delivery(source_key, format_type)


@router.get("/{source_key}/download")
def download_artifact(
    source_key: str,
    line: int | None = Query(default=None, ge=0),
    allow_stale: bool = Query(default=False),
) -> Any:
    """下载已确认的日志成品；成品未覆盖请求行时返回可继续处理的状态。"""
    _, exporter = _services()
    with delivery.source_lock(source_key):
        manifest = exporter.get_export_manifest(source_key)
        artifact = manifest.get("artifact_record")
        verify = manifest.get("artifact") or {}
        requested_line = int(line if line is not None else manifest.get("checkpoint_line") or 0)
        latest = manifest.get("last_delivery") or {}

        ready = bool(verify.get("ok"))
        covered = ready and int(verify.get("line_to") or 0) >= requested_line
        stale = ready and int(verify.get("line_to") or 0) < requested_line

        state = delivery.load(source_key)
        if not covered:
            served_stale = bool(stale and allow_stale)
            entry = delivery.record_download(
                source_key, state,
                requested_line=requested_line,
                artifact=artifact,
                ready=False,
                served=served_stale,
                served_stale=served_stale,
                status="stale_allowed" if served_stale else ("stale" if stale else "not_ready"),
                delivery_id=str(latest.get("delivery_id") or ""),
            )
            if served_stale:
                return _file_response(artifact, stale=True, download_id=entry["download_id"])
            return JSONResponse(status_code=409, content={
                "ok": False,
                "status": "stale" if stale else "not_ready",
                "download_id": entry["download_id"],
                "source_key": source_key,
                "requested_line": requested_line,
                "confirmed_line": manifest.get("confirmed_line", 0),
                "checkpoint_line": manifest.get("checkpoint_line", 0),
                "pending_range": manifest.get("pending_range"),
                "artifact_line_to": verify.get("line_to"),
                "can_retry": manifest.get("can_retry"),
                "retry_url": manifest.get("retry_url"),
                "manifest_url": f"/api/delivery/{source_key}/manifest",
                "message": (
                    f"已确认成品只到第 {verify.get('line_to')} 行，请求第 {requested_line} 行；"
                    "请先重试生成并确认，或显式允许下载旧成品"
                    if stale else "尚无已确认的可下载成品，请先完成位点提交或重试生成"
                ),
            })

        entry = delivery.record_download(
            source_key, state,
            requested_line=requested_line,
            artifact=artifact,
            ready=True,
            served=True,
            served_stale=False,
            status="served",
            delivery_id=str(latest.get("delivery_id") or ""),
        )
        return _file_response(artifact, stale=False, download_id=entry["download_id"])


def _file_response(artifact: dict[str, Any], *, stale: bool, download_id: str) -> FileResponse:
    path = str(artifact.get("path") or "")
    if not path:
        raise HTTPException(status_code=409, detail="尚无已确认成品")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=str(artifact.get("file_name") or ""),
        headers={
            "X-Delivery-Id": str(artifact.get("delivery_id") or ""),
            "X-Delivery-Download-Id": download_id,
            "X-Artifact-Line-From": str(artifact.get("line_from") or 0),
            "X-Artifact-Line-To": str(artifact.get("line_to") or 0),
            "X-Artifact-Lines": str(artifact.get("lines") or 0),
            "X-Artifact-Stale": "true" if stale else "false",
            "X-Artifact-Checksum": str(artifact.get("checksum") or ""),
        },
    )
