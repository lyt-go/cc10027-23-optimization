"""可恢复交付记录：位点提交、成品生成、确认与下载的可追查账本。

一次位点提交可能只成功了一半：位点已经向前推进，但格式输出的成品没有
生成完成。这里把“提交尝试”“成品生成结果”“确认范围”“下载记录”落到
独立文件里，刷新页面或重新打开导出清单后仍能核对待办范围。

账本文件：``data/checkpoints/<source_key>_delivery.json``
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from app.services.base import DATA_ROOT

# 同一进程内按日志源串行化“提交 -> 生成 -> 确认”，避免两个请求同时改写
# 同一份成品或交付记录。RLock 允许 commit 在持锁时继续调用 export。
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

_LEDGER_VERSION = 1
_MAX_HISTORY = 500
_MAX_DOWNLOADS = 200


def source_lock(source_key: str) -> threading.RLock:
    """取得指定日志源的交付锁。"""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(source_key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[source_key] = lock
        return lock


def ledger_path(source_key: str) -> Path:
    return DATA_ROOT / "checkpoints" / f"{source_key}_delivery.json"


def _now() -> float:
    return time.time()


def _new_state(source_key: str) -> dict[str, Any]:
    return {
        "source_key": source_key,
        "version": _LEDGER_VERSION,
        "sequence": 0,
        "confirmed_line": 0,
        "pending_line": 0,
        "last_delivery_id": "",
        "last_confirmed_delivery_id": "",
        "artifact": None,
        "deliveries": [],
        "downloads": [],
        "download_sequence": 0,
        "history": [],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load(source_key: str) -> dict[str, Any]:
    """读取交付账本；不存在或损坏时返回空账本，不影响历史成品读取。"""
    path = ledger_path(source_key)
    state = _new_state(source_key)
    if not path.exists():
        return state
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return state
    if isinstance(loaded, dict):
        state.update(loaded)
    state.setdefault("source_key", source_key)
    state.setdefault("version", _LEDGER_VERSION)
    state.setdefault("sequence", 0)
    state.setdefault("confirmed_line", 0)
    state.setdefault("pending_line", 0)
    state.setdefault("deliveries", [])
    state.setdefault("downloads", [])
    state.setdefault("download_sequence", 0)
    state.setdefault("history", [])
    return state


def save(source_key: str, state: dict[str, Any]) -> None:
    state["source_key"] = source_key
    state["version"] = _LEDGER_VERSION
    state["updated_at"] = _now()
    state["history"] = state.get("history", [])[-_MAX_HISTORY:]
    state["downloads"] = state.get("downloads", [])[-_MAX_DOWNLOADS:]
    _atomic_write_json(ledger_path(source_key), state)


def record_event(source_key: str, state: dict[str, Any], action: str,
                 **fields: Any) -> None:
    """在账本里追加一条可追查事件（不单独落盘）。"""
    event = {"at": _now(), "action": action}
    event.update(fields)
    state.setdefault("history", []).append(event)


def latest_delivery(state: dict[str, Any]) -> dict[str, Any] | None:
    deliveries = state.get("deliveries") or []
    return deliveries[-1] if deliveries else None


def find_delivery(state: dict[str, Any], delivery_id: str) -> dict[str, Any] | None:
    for record in state.get("deliveries") or []:
        if record.get("delivery_id") == delivery_id:
            return record
    return None


def delivered_for_line(state: dict[str, Any], line_number: int) -> dict[str, Any] | None:
    """查找已经确认过指定行号的交付记录（从新到旧）。"""
    for record in reversed(state.get("deliveries") or []):
        if int(record.get("to_line") or 0) == int(line_number) and record.get("status") == "delivered":
            return record
    return None


def reconcile(source_key: str, state: dict[str, Any]) -> dict[str, Any]:
    """读取账本时收尾“生成中”的悬空记录：进程中断后仍能重试而不是卡住。"""
    changed = False
    stale_after = 300
    for record in state.get("deliveries") or []:
        if record.get("status") != "generating":
            continue
        if _now() - float(record.get("updated_at") or 0) < stale_after:
            continue
        record["status"] = "failed"
        record["state"] = "retryable"
        record["can_retry"] = True
        record["error"] = record.get("error") or "生成过程在完成前中断，可重新生成"
        record["failure_reason"] = "interrupted"
        record["updated_at"] = _now()
        changed = True
        record_event(source_key, state, "generation_reconciled",
                     delivery_id=record.get("delivery_id"))
    if changed:
        save(source_key, state)
    return state


def create_pending(
    state: dict[str, Any],
    *,
    source_key: str,
    from_line: int,
    to_line: int,
    offset: int,
    format_type: str,
    previous_confirmed_line: int,
    reason: str = "checkpoint_commit",
) -> dict[str, Any]:
    """创建一条待生成交付记录，先落账再动文件。"""
    sequence = int(state.get("sequence") or 0) + 1
    state["sequence"] = sequence
    delivery_id = f"DLV-{source_key}-{sequence:04d}"
    record = {
        "delivery_id": delivery_id,
        "source_key": source_key,
        "format": format_type,
        "from_line": int(from_line),
        "to_line": int(to_line),
        "checkpoint_offset": int(offset or 0),
        "previous_confirmed_line": int(previous_confirmed_line),
        "status": "pending",
        "state": "queued",
        "reason": reason,
        "attempts": 0,
        "confirmation_count": 0,
        "can_retry": True,
        "created_at": _now(),
        "updated_at": _now(),
        "confirmed_at": None,
        "error": None,
        "artifact": None,
    }
    state.setdefault("deliveries", []).append(record)
    state["last_delivery_id"] = delivery_id
    state["pending_line"] = int(to_line)
    record_event(
        source_key, state, "delivery_created",
        delivery_id=delivery_id, from_line=from_line, to_line=to_line,
        format=format_type, reason=reason,
    )
    return record


def start_attempt(source_key: str, state: dict[str, Any],
                  record: dict[str, Any]) -> dict[str, Any]:
    """标记开始生成成品，并持久化尝试次数。"""
    record["status"] = "generating"
    record["state"] = "generating"
    record["attempts"] = int(record.get("attempts") or 0) + 1
    record["updated_at"] = _now()
    state["pending_line"] = int(record.get("to_line") or 0)
    record_event(
        source_key, state, "generation_started",
        delivery_id=record["delivery_id"], attempt=record["attempts"],
    )
    save(source_key, state)
    return record


def fail(
    source_key: str,
    state: dict[str, Any],
    record: dict[str, Any] | None,
    *,
    error: str,
    reason: str = "generation_failed",
    available_to: int | None = None,
    preserved_artifact: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """记录生成失败；不修改已确认范围，也不改写旧成品元数据。"""
    if record is None:
        return None
    record["status"] = "failed"
    record["state"] = "retryable"
    record["can_retry"] = True
    record["error"] = error
    record["failure_reason"] = reason
    record["available_to"] = available_to
    record["updated_at"] = _now()
    if preserved_artifact is not None:
        record["preserved_artifact"] = preserved_artifact
    state["pending_line"] = int(record.get("to_line") or 0)
    record_event(
        source_key, state, "generation_failed",
        delivery_id=record["delivery_id"], error=error, reason=reason,
        attempt=record.get("attempts", 0),
    )
    save(source_key, state)
    return record


def confirm(
    source_key: str,
    state: dict[str, Any],
    record: dict[str, Any],
    *,
    artifact: dict[str, Any],
    confirmed_line: int,
) -> dict[str, Any]:
    """成品校验通过后确认交付；只确认一次与当前位点对得上的成品。"""
    record["status"] = "delivered"
    record["state"] = "delivered"
    record["artifact"] = artifact
    record["confirmed_at"] = _now()
    record["updated_at"] = _now()
    record["error"] = None
    record["can_retry"] = False
    record["confirmation_count"] = int(record.get("confirmation_count") or 0) + 1

    confirmed_line = int(confirmed_line)
    state["confirmed_line"] = max(int(state.get("confirmed_line") or 0), confirmed_line)
    state["pending_line"] = 0
    state["artifact"] = artifact
    state["last_confirmed_delivery_id"] = record["delivery_id"]

    for other in state.get("deliveries") or []:
        if other is record:
            continue
        if other.get("status") == "delivered" and int(other.get("to_line") or 0) < confirmed_line:
            other["status"] = "superseded"
            other["state"] = "superseded"
            other["superseded_by"] = record["delivery_id"]
            other["can_retry"] = False

    record_event(
        source_key, state, "delivery_confirmed",
        delivery_id=record["delivery_id"], to_line=record.get("to_line"),
        confirmed_line=confirmed_line, artifact_lines=artifact.get("lines"),
        artifact_line_to=artifact.get("line_to"),
    )
    save(source_key, state)
    return record


def record_download(
    source_key: str,
    state: dict[str, Any],
    *,
    requested_line: int,
    artifact: dict[str, Any] | None,
    ready: bool,
    served: bool,
    served_stale: bool,
    status: str,
    delivery_id: str = "",
) -> dict[str, Any]:
    """追加一次下载记录，便于事后复查下载到的是哪一版成品。"""
    sequence = int(state.get("download_sequence") or 0) + 1
    state["download_sequence"] = sequence
    entry = {
        "download_id": f"DLD-{source_key}-{sequence:04d}",
        "source_key": source_key,
        "at": _now(),
        "requested_line": int(requested_line),
        "delivery_id": delivery_id,
        "artifact_line_from": (artifact or {}).get("line_from"),
        "artifact_line_to": (artifact or {}).get("line_to"),
        "artifact_lines": (artifact or {}).get("lines"),
        "ready": bool(ready),
        "served": bool(served),
        "served_stale": bool(served_stale),
        "status": status,
    }
    state.setdefault("downloads", []).append(entry)
    record_event(
        source_key, state, "download",
        download_id=entry["download_id"], requested_line=requested_line,
        ready=ready, served=served, served_stale=served_stale,
    )
    save(source_key, state)
    return entry


def pending_range(confirmed_line: int, checkpoint_line: int) -> dict[str, int] | None:
    """待办行范围：已确认行之后的下一行到当前位点行。"""
    confirmed_line = int(confirmed_line or 0)
    checkpoint_line = int(checkpoint_line or 0)
    if checkpoint_line <= confirmed_line:
        return None
    return {"from_line": confirmed_line + 1, "to_line": checkpoint_line}
