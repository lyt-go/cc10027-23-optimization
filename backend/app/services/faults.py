"""故障注入：让“文件生成中断”可以真实复现，而不是只在测试里 monkeypatch。

一次性故障（one-shot）写到 ``data/checkpoints/faults.json``，格式::

    {"format_output": {"mode": "interrupt", "after_lines": 40, "error": "磁盘写入中断"}}

下一次格式输出生成会读取并消费该故障，因此重试时自然恢复。生产环境不会
写入这个文件；只有调试或演练时才会用到。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from app.services.base import DATA_ROOT


def _fault_file():
    return DATA_ROOT / "checkpoints" / "faults.json"


def _read_all() -> dict[str, Any]:
    path = _fault_file()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_all(payload: dict[str, Any]) -> None:
    path = _fault_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def arm_fault(target: str, *, mode: str = "interrupt", after_lines: int | None = None,
              error: str = "", times: int = 1) -> dict[str, Any]:
    """为下一次（或接下来 times 次）“文件生成”安排一次性故障。"""
    plan = {
        "mode": mode,
        "after_lines": after_lines,
        "error": error or "文件生成中断（故障注入）",
        "times": max(int(times), 1),
        "armed_at": time.time(),
    }
    payload = _read_all()
    payload[target] = plan
    _write_all(payload)
    return plan


def take_fault(target: str) -> dict[str, Any] | None:
    """取出并消费指定目标的一次性故障；没有则返回 None。"""
    payload = _read_all()
    plan = payload.get(target)
    if not isinstance(plan, dict):
        return None

    remaining = int(plan.get("times") or 1) - 1
    if remaining > 0:
        plan["times"] = remaining
        payload[target] = plan
    else:
        payload.pop(target, None)
    _write_all(payload)
    return plan


def current_faults() -> dict[str, Any]:
    return _read_all()


def clear_faults() -> None:
    path = _fault_file()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
