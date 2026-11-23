"""可恢复交付记录的自动化测试：位点提交、成品生成中断、清单核对与重试。

覆盖需求中的完整场景：
- 第 100 行提交成功后成品 1-100 可下载；
- 位点推进到 200 行时文件生成中断：位点已推进、旧成品不被改写；
- 清单重新打开仍能核对 101-200 待办；
- 重试后只确认一次，成品覆盖 1-200；
- 再次提交同一位点只复核、不重复确认；
- 下载未覆盖的行给出可继续处理的状态。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services import delivery, faults
from app.services.base import DATA_ROOT
from app.services.collect_checkpoint import CollectCheckpointService
from app.services.field_mapping import FieldMappingService
from app.services.format_output import FormatOutputService
from app.services.log_parser import LogParserService
from app.services.log_source import LogSourceService
from app.store import store

SOURCE_ID = 3
SOURCE_KEY = "LOG_-0003"


@pytest.fixture()
def clean_source():
    """让 LOG_-0003 从干净状态开始，且不触碰其它源的既有产物。"""
    for sub in ("exports", "logs"):
        root = DATA_ROOT / sub
        if root.exists():
            for path in root.glob(f"{SOURCE_KEY}*"):
                if path.is_file():
                    path.unlink()
    for name in (f"{SOURCE_KEY}.json", f"{SOURCE_KEY}_delivery.json",
                 f"{SOURCE_KEY}_export.json", f"{SOURCE_KEY}_parse.json",
                 f"{SOURCE_KEY}_map.json"):
        path = DATA_ROOT / "checkpoints" / name
        if path.exists():
            path.unlink()
    faults.clear_faults()

    source = store.find("log_source", SOURCE_ID)
    source["status"] = "已接入"
    checkpoint = CollectCheckpointService()
    checkpoint._write_checkpoint(SOURCE_KEY, {
        "source_key": SOURCE_KEY, "offset": 0, "line_number": 0, "status": "active",
    })
    yield checkpoint
    faults.clear_faults()


def _collect_and_map(batch_size: int) -> None:
    LogSourceService().collect_from_source(SOURCE_ID, batch_size=batch_size)
    LogParserService().parse_log_file(SOURCE_KEY)
    FieldMappingService().apply_mappings(SOURCE_KEY)


def _artifact_lines(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _commit_to(clean_source: CollectCheckpointService, line_number: int) -> dict:
    """先把位点推进到目标行（模拟采集前进），再提交。"""
    from app.services.base import BaseModuleService

    svc = BaseModuleService()
    cp = clean_source._read_checkpoint(SOURCE_KEY)
    svc._write_checkpoint(SOURCE_KEY, {
        **cp, "line_number": line_number, "offset": line_number * 116, "status": "active",
    })
    return clean_source.commit_checkpoint(SOURCE_KEY)


def test_first_commit_publishes_100_line_artifact(clean_source):
    _collect_and_map(100)
    result = _commit_to(clean_source, 100)

    assert result["ok"] and result["committed"] and result["complete"]
    assert result["confirmed_line"] == 100
    assert result["delivery_status"] == "delivered"
    artifact = result["artifact"]
    assert artifact["line_from"] == 1 and artifact["line_to"] == 100
    assert artifact["lines"] == 100

    records = _artifact_lines(artifact["path"])
    assert [rec["line_no"] for rec in records] == list(range(1, 101))
    assert result["delivery"]["confirmation_count"] == 1

    manifest = FormatOutputService().get_export_manifest(SOURCE_KEY)
    assert manifest["status"] == "confirmed"
    assert manifest["pending_range"] is None
    assert manifest["downloadable"] is True


def test_interrupted_generation_keeps_progress_and_old_artifact(clean_source):
    _collect_and_map(100)
    _commit_to(clean_source, 100)
    first_artifact = FormatOutputService().get_export_manifest(SOURCE_KEY)["artifact"]
    first_path = first_artifact["file_name"]

    # 采集推进到 200 行，下一次文件生成中断
    _collect_and_map(100)
    faults.arm_fault("format_output", mode="interrupt", after_lines=40,
                     error="磁盘写入中断")
    result = _commit_to(clean_source, 200)

    assert result["ok"] is True, "位点提交本身应成功"
    assert result["committed"] is True
    assert result["complete"] is False and result["partial"] is True
    assert result["delivery_status"] == "failed"
    assert result["confirmed_line"] == 100, "失败时确认行号不能前移"
    assert result["pending_range"] == {"from_line": 101, "to_line": 200}
    assert result["can_retry"] is True
    assert result["delivery"]["attempts"] == 1
    assert result["delivery"]["confirmation_count"] == 0

    # 旧成品文件没有被改写，也没有半份临时文件被当成成品
    manifest = FormatOutputService().get_export_manifest(SOURCE_KEY)
    assert manifest["artifact"]["ok"] is True
    assert manifest["artifact"]["file_name"] == first_path
    assert manifest["artifact"]["line_to"] == 100
    assert manifest["artifact"]["lines"] == 100
    assert len(_artifact_lines(manifest["artifact_record"]["path"])) == 100

    # 位点进度保留，可追查
    checkpoint = clean_source.get_checkpoint_state(SOURCE_KEY)
    assert checkpoint["line_number"] == 200
    assert checkpoint["delivery_status"] == "failed"

    # 重新打开清单（另一位同事）仍能核对待办范围
    reopened = FormatOutputService().get_export_manifest(SOURCE_KEY)
    assert reopened["status"] == "partially_committed"
    assert reopened["checkpoint_line"] == 200
    assert reopened["confirmed_line"] == 100
    assert reopened["pending_range"] == {"from_line": 101, "to_line": 200}
    assert reopened["next_action"] == "retry_generation"
    assert reopened["download_is_stale"] is True


def test_retry_confirms_once_and_covers_200(clean_source):
    _collect_and_map(100)
    _commit_to(clean_source, 100)
    _collect_and_map(100)
    faults.arm_fault("format_output", after_lines=40, error="磁盘写入中断")
    _commit_to(clean_source, 200)

    retry = clean_source.retry_delivery(SOURCE_KEY)
    assert retry["ok"] is True
    assert retry["confirmed_line"] == 200
    assert retry["delivery"]["confirmation_count"] == 1
    assert retry["delivery"]["attempts"] == 2

    records = _artifact_lines(retry["artifact"]["path"])
    assert [rec["line_no"] for rec in records] == list(range(1, 201))
    assert retry["artifact"]["line_to"] == 200

    manifest = FormatOutputService().get_export_manifest(SOURCE_KEY)
    assert manifest["status"] == "confirmed"
    assert manifest["pending_range"] is None
    assert manifest["download_is_stale"] is False
    assert manifest["artifact"]["lines"] == 200
    assert manifest["deliveries"][-1]["confirmation_count"] == 1


def test_replay_commit_does_not_confirm_twice(clean_source):
    _collect_and_map(100)
    _commit_to(clean_source, 100)
    _collect_and_map(100)
    _commit_to(clean_source, 200)

    replay = clean_source.commit_checkpoint(SOURCE_KEY)
    assert replay["ok"] and replay["replayed"] is True
    assert replay["already_confirmed"] is True
    assert replay["confirmed_line"] == 200

    manifest = FormatOutputService().get_export_manifest(SOURCE_KEY)
    delivered = [d for d in manifest["deliveries"] if d["to_line"] == 200]
    assert len(delivered) == 1, "同一位点只能有一条交付记录"
    assert delivered[0]["confirmation_count"] == 1
    assert manifest["artifact"]["line_to"] == 200


def test_replay_after_retry_is_still_single_confirmation(clean_source):
    _collect_and_map(100)
    _commit_to(clean_source, 100)
    _collect_and_map(100)
    faults.arm_fault("format_output", after_lines=40, error="磁盘写入中断")
    _commit_to(clean_source, 200)
    clean_source.retry_delivery(SOURCE_KEY)

    replay = clean_source.commit_checkpoint(SOURCE_KEY)
    assert replay["replayed"] is True
    manifest = FormatOutputService().get_export_manifest(SOURCE_KEY)
    target = [d for d in manifest["deliveries"] if d["to_line"] == 200]
    assert len(target) == 1
    assert target[0]["confirmation_count"] == 1
    assert target[0]["status"] == "delivered"


def test_download_states_are_actionable(clean_source):
    from fastapi.responses import FileResponse, JSONResponse

    from app.routers.delivery import download_artifact

    _collect_and_map(100)
    _commit_to(clean_source, 100)
    _collect_and_map(100)
    faults.arm_fault("format_output", after_lines=40, error="磁盘写入中断")
    _commit_to(clean_source, 200)

    # 请求第 200 行时，成品只有 100 行：409 + 可继续处理的信息
    response = download_artifact(SOURCE_KEY, line=200, allow_stale=False)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["status"] == "stale"
    assert payload["requested_line"] == 200
    assert payload["confirmed_line"] == 100
    assert payload["pending_range"] == {"from_line": 101, "to_line": 200}
    assert payload["can_retry"] is True
    assert payload["retry_url"].endswith("/retry")

    # 显式允许旧成品时明确标记 stale
    stale = download_artifact(SOURCE_KEY, line=200, allow_stale=True)
    assert isinstance(stale, FileResponse)
    assert stale.headers["X-Artifact-Stale"] == "true"
    assert stale.headers["X-Artifact-Line-To"] == "100"
    assert len([ln for ln in open(stale.path, encoding="utf-8") if ln.strip()]) == 100

    # 重试后同一请求可正常下载 1-200
    clean_source.retry_delivery(SOURCE_KEY)
    ok = download_artifact(SOURCE_KEY, line=200, allow_stale=False)
    assert isinstance(ok, FileResponse)
    assert ok.headers["X-Artifact-Line-To"] == "200"
    assert ok.headers["X-Artifact-Stale"] == "false"
    downloaded = [json.loads(ln) for ln in open(ok.path, encoding="utf-8") if ln.strip()]
    assert downloaded[-1]["line_no"] == 200

    manifest = FormatOutputService().get_export_manifest(SOURCE_KEY)
    statuses = [item["status"] for item in manifest["downloads"]]
    assert statuses[:2] == ["stale", "stale_allowed"]
    assert statuses[-1] == "served"


def test_retry_load_is_stable_across_reopen(clean_source):
    """重启后从磁盘重新读取账本，状态与待办范围保持一致。"""
    _collect_and_map(100)
    _commit_to(clean_source, 100)
    _collect_and_map(100)
    faults.arm_fault("format_output", after_lines=40, error="磁盘写入中断")
    _commit_to(clean_source, 200)

    raw = delivery.ledger_path(SOURCE_KEY)
    assert raw.exists()
    with open(raw, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["confirmed_line"] == 100
    assert on_disk["pending_line"] == 200

    fresh = delivery.load(SOURCE_KEY)
    assert fresh["confirmed_line"] == 100
    assert fresh["deliveries"][-1]["status"] == "failed"
    assert fresh["deliveries"][-1]["can_retry"] is True


def test_non_delivery_export_does_not_overwrite_confirmed_artifact(clean_source):
    """普通整源导出不能把已确认成品改写成别的行范围。"""
    _collect_and_map(100)
    _commit_to(clean_source, 100)
    confirmed = FormatOutputService().get_export_manifest(SOURCE_KEY)["artifact_record"]

    exporter = FormatOutputService()
    exporter.export_source(SOURCE_KEY, "jsonl")

    manifest = exporter.get_export_manifest(SOURCE_KEY)
    assert manifest["artifact"]["ok"] is True
    assert manifest["artifact"]["line_to"] == 100
    assert manifest["artifact"]["checksum"] == confirmed["checksum"]


def test_missing_artifact_can_be_repaired(clean_source):
    _collect_and_map(100)
    _commit_to(clean_source, 100)
    manifest = FormatOutputService().get_export_manifest(SOURCE_KEY)
    path = manifest["artifact_record"]["path"]
    __import__("os").remove(path)

    broken = FormatOutputService().get_export_manifest(SOURCE_KEY)
    assert broken["status"] == "artifact_missing"
    assert broken["can_retry"] is True

    repaired = clean_source.retry_delivery(SOURCE_KEY)
    assert repaired["ok"] is True
    assert repaired["repaired"] is True
    healthy = FormatOutputService().get_export_manifest(SOURCE_KEY)
    assert healthy["status"] == "confirmed"
    assert healthy["artifact"]["lines"] == 100
