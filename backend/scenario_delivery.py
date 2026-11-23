"""可恢复交付场景：位点提交部分成功时的清单、下载与重试行为。

场景（对应需求）：
1. 采集 100 行并提交 -> 成品 1-100 行确认、可下载；
2. 位点推进到 200 行，提交时文件生成中断 -> 位点 200、成品仍是 1-100；
3. 重新打开清单能核对到待办 101-200；
4. 重试后生成截至第 200 行的完整成品，只确认一次；
5. 再次提交同一位点：只复核，不重复确认；
6. 下载记录能复查每一步拿到的是哪一版成品。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services import delivery, faults
from app.services.base import DATA_ROOT
from app.services.collect_checkpoint import CollectCheckpointService
from app.services.format_output import FormatOutputService
from app.services.log_source import LogSourceService
from app.store import store

SOURCE_ID = 3
SOURCE_KEY = "LOG_-0003"


def _reset_source_files() -> None:
    for sub in ("exports", "logs"):
        root = DATA_ROOT / sub
        if not root.exists():
            continue
        for path in root.glob(f"{SOURCE_KEY}*"):
            if path.is_file():
                path.unlink()
    ckpt = DATA_ROOT / "checkpoints"
    for name in (
        f"{SOURCE_KEY}.json",
        f"{SOURCE_KEY}_delivery.json",
        f"{SOURCE_KEY}_export.json",
    ):
        path = ckpt / name
        if path.exists():
            path.unlink()
    # 解析/映射元数据也一并清空，保证场景从干净状态开始。
    for path in ckpt.glob(f"{SOURCE_KEY}_*"):
        if path.name.endswith(("_parse.json", "_map.json")):
            path.unlink()
    faults.clear_faults()


def _map_and_parse() -> None:
    from app.services.field_mapping import FieldMappingService
    from app.services.log_parser import LogParserService

    LogParserService().parse_log_file(SOURCE_KEY)
    FieldMappingService().apply_mappings(SOURCE_KEY)


def _print(title: str, payload: dict) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _download(source_key: str, line: int | None = None, allow_stale: bool = False):
    """直接调用下载接口函数，避免场景脚本依赖额外测试客户端。"""
    from fastapi.responses import FileResponse, JSONResponse

    from app.routers.delivery import download_artifact

    response = download_artifact(source_key, line=line, allow_stale=allow_stale)
    if isinstance(response, JSONResponse):
        return {"status_code": response.status_code, "body": json.loads(response.body)}
    assert isinstance(response, FileResponse)
    with open(response.path, "r", encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    headers = response.headers
    return {
        "status_code": 200,
        "line_from": headers.get("X-Artifact-Line-From"),
        "line_to": headers.get("X-Artifact-Line-To"),
        "lines": len(lines),
        "stale": headers.get("X-Artifact-Stale"),
        "download_id": headers.get("X-Delivery-Download-Id"),
        "first_line_no": json.loads(lines[0]).get("line_no") if lines else None,
        "last_line_no": json.loads(lines[-1]).get("line_no") if lines else None,
    }


def run_scenario() -> dict:
    _reset_source_files()
    checkpoint_svc = CollectCheckpointService()
    exporter = FormatOutputService()

    # 准备 100 行已采集数据
    source = store.find("log_source", SOURCE_ID)
    source["status"] = "已接入"
    LogSourceService().collect_from_source(SOURCE_ID, batch_size=100)
    LogSourceService().collect_from_source(SOURCE_ID, batch_size=0)  # 只刷新 checkpoint 元数据
    _map_and_parse()
    # 采集自带一次解析/映射，这里再显式解析一次，确认覆盖写不会重复。
    _map_and_parse()

    commit1 = checkpoint_svc.commit_checkpoint(SOURCE_KEY, "jsonl")
    manifest1 = exporter.get_export_manifest(SOURCE_KEY)
    download1 = _download(SOURCE_KEY, 100)
    download_stale_before = _download(SOURCE_KEY, 200)

    # 推进到 200 行：下一次文件生成中断
    LogSourceService().collect_from_source(SOURCE_ID, batch_size=100)
    _map_and_parse()
    faults.arm_fault("format_output", mode="interrupt", after_lines=40,
                     error="磁盘写入中断：成品写到第 40 行后失败")
    commit2 = checkpoint_svc.commit_checkpoint(SOURCE_KEY, "jsonl")
    manifest2 = exporter.get_export_manifest(SOURCE_KEY)
    download2 = _download(SOURCE_KEY, 200)
    download2_stale_ok = _download(SOURCE_KEY, 200, allow_stale=True)
    canonical_lines = sum(
        1 for line in (DATA_ROOT / "exports" / f"{SOURCE_KEY}_export.jsonl")
        .read_text(encoding="utf-8").splitlines() if line.strip())

    # 另一位同事看到的清单：待办范围仍在
    reopened = exporter.get_export_manifest(SOURCE_KEY)

    retry = checkpoint_svc.retry_delivery(SOURCE_KEY)
    manifest3 = exporter.get_export_manifest(SOURCE_KEY)
    download3 = _download(SOURCE_KEY, 200)

    replay = checkpoint_svc.commit_checkpoint(SOURCE_KEY, "jsonl")
    manifest4 = exporter.get_export_manifest(SOURCE_KEY)
    download4 = _download(SOURCE_KEY, 200)

    ledger = delivery.load(SOURCE_KEY)
    return {
        "commit_100": commit1,
        "manifest_after_100": manifest1,
        "download_100": download1,
        "download_200_before_commit": download_stale_before,
        "commit_200_interrupted": commit2,
        "manifest_partial": manifest2,
        "download_200_partial": download2,
        "download_200_partial_allow_stale": download2_stale_ok,
        "canonical_lines_after_failure": canonical_lines,
        "manifest_reopened_by_colleague": {
            "status": reopened["status"],
            "checkpoint_line": reopened["checkpoint_line"],
            "confirmed_line": reopened["confirmed_line"],
            "pending_range": reopened["pending_range"],
            "next_action": reopened["next_action"],
            "downloadable": reopened["downloadable"],
        },
        "retry": retry,
        "manifest_after_retry": {
            "status": manifest3["status"],
            "confirmed_line": manifest3["confirmed_line"],
            "pending_range": manifest3["pending_range"],
            "artifact": manifest3["artifact"],
            "deliveries": [
                {k: d.get(k) for k in ("delivery_id", "from_line", "to_line", "status", "confirmation_count", "attempts")}
                for d in manifest3["deliveries"]
            ],
        },
        "download_200_after_retry": download3,
        "replay_commit": replay,
        "manifest_after_replay": {
            "status": manifest4["status"],
            "confirmed_line": manifest4["confirmed_line"],
            "deliveries": [
                {k: d.get(k) for k in ("delivery_id", "from_line", "to_line", "status", "confirmation_count", "attempts")}
                for d in manifest4["deliveries"]
            ],
        },
        "download_200_after_replay": download4,
        "downloads_recorded": ledger.get("downloads"),
        "history_actions": [h.get("action") for h in ledger.get("history", [])],
    }


if __name__ == "__main__":
    result = run_scenario()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
