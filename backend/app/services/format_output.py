"""格式输出：将标准化数据按输出格式配置写入指定路径。

成品始终先写临时文件，校验行范围与行数后才原子替换到正式路径。因此：

- 生成中断时不会留下半份成品，也不会覆盖上一次可下载的成品；
- 编号成品（``*_export_v0001.jsonl``）只增不改，便于按交付记录复查；
- 交付确认以一次位点提交为边界，成品里的日志行范围必须与交付记录一致。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from app.services import delivery
from app.services.base import BaseModuleService, DATA_ROOT
from app.services.faults import take_fault


class GenerationInterrupted(RuntimeError):
    """模拟/真实文件生成在写入过程中中断。"""


_FORMAT_SUFFIX = {
    "jsonl": ".jsonl",
    "json": ".json",
    "gz": ".jsonl.gz",
    "csv": ".csv",
}


class FormatOutputService(BaseModuleService):
    MODULE = "format_output"
    ENTITY = "输出配置"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["配置编号", "关联源", "输出格式", "输出路径", "压缩方式", "分片大小", "命名规则", "配置版本"]
    REQUIRED_FIELDS = ["配置编号", "关联源", "输出格式"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用配置": "已停用"}
    NEGATIVE_ACTIONS = ["停用配置"]

    # -- 导出入口 --

    def export_source(
        self,
        source_key: str,
        format_type: str = "jsonl",
        *,
        from_line: int | None = None,
        to_line: int | None = None,
        delivery_id: str = "",
    ) -> dict[str, Any]:
        """将指定日志源的标准化数据导出为指定格式。

        传入 ``from_line`` / ``to_line`` 时只导出该行范围内的记录（交付成品），
        成品写入编号文件；不传范围时保持原有的整源导出行为。
        """
        if format_type not in _FORMAT_SUFFIX:
            return {"ok": False, "error": f"不支持的输出格式: {format_type}"}

        mapped_file = self._data_dir("logs") / f"{source_key}_mapped.jsonl"
        if not mapped_file.exists():
            return {
                "ok": False,
                "source_key": source_key,
                "error": f"日志源 {source_key} 无映射数据，请先执行字段映射",
                "available_to": 0,
                "records": 0,
            }

        records = self._read_jsonl(mapped_file)
        ranged = from_line is not None or to_line is not None
        if ranged:
            records, range_error = self._select_range(records, from_line, to_line)
            if range_error is not None:
                range_error.update({"ok": False, "source_key": source_key})
                return range_error

        exports_dir = self._data_dir("exports")
        suffix = _FORMAT_SUFFIX[format_type]
        canonical = exports_dir / f"{source_key}_export{suffix}"
        if ranged and delivery_id:
            output_file = exports_dir / (
                f"{source_key}_export_v{self._delivery_suffix(delivery_id)}{suffix}")
        elif ranged:
            output_file = exports_dir / f"{source_key}_export_{from_line}_{to_line}{suffix}"
        else:
            # 非交付的整源导出不能覆盖已确认的交付成品，另写一份工作副本。
            ledger = delivery.load(source_key)
            confirmed_path = str((ledger.get("artifact") or {}).get("path") or "")
            if confirmed_path and Path(confirmed_path).resolve() == canonical.resolve():
                output_file = exports_dir / f"{source_key}_export_working{suffix}"
            else:
                output_file = canonical

        plan = take_fault("format_output")
        tmp = output_file.with_name(
            f".{output_file.name}.tmp-{os.getpid()}-{time.time_ns()}"
        )
        try:
            written = self._write_records(tmp, records, format_type, plan)
            actual_lines = self._count_artifact_lines(tmp, format_type)
            if actual_lines != written:
                raise GenerationInterrupted(
                    f"成品校验失败：应写入 {written} 行，实际落盘 {actual_lines} 行"
                )
            with open(tmp, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()[:16]
            os.replace(tmp, output_file)
        except Exception as exc:  # 中断/校验失败：只清理临时文件，不动旧成品
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return {
                "ok": False,
                "source_key": source_key,
                "format": format_type,
                "error": str(exc),
                "reason": "generation_interrupted" if isinstance(exc, GenerationInterrupted) else "generation_failed",
                "available_to": self._available_line(records),
                "records": 0,
            }

        line_numbers = [int(rec.get("line_no") or 0) for rec in records]
        artifact = {
            "delivery_id": delivery_id,
            "source_key": source_key,
            "format": format_type,
            "path": str(output_file),
            "file_name": output_file.name,
            "lines": written,
            "line_from": min(line_numbers) if line_numbers else 0,
            "line_to": max(line_numbers) if line_numbers else 0,
            "size_bytes": output_file.stat().st_size,
            "checksum": digest,
            "created_at": time.time(),
            "verified": True,
        }

        # 整源导出（非交付链路）保持原有清单行为，交付链路由位点确认后写入。
        if not delivery_id:
            self._write_checkpoint(f"{source_key}_export", {
                "format": format_type,
                "records": written,
                "line_from": artifact["line_from"],
                "line_to": artifact["line_to"],
                "lines": written,
                "artifact": artifact,
                "output_file": str(output_file),
                "exported_at": time.time(),
            })

        return {
            "ok": True,
            **artifact,
            "source_key": source_key,
            "records": written,
            "output_file": str(output_file),
        }

    def export_batch(self, source_keys: list[str], format_type: str = "jsonl") -> dict[str, Any]:
        """批量导出多个日志源的数据。"""
        results = []
        total = 0
        for key in source_keys:
            r = self.export_source(key, format_type)
            results.append(r)
            total += r.get("records", 0)
        return {"total": total, "sources": len(results), "details": results}

    # -- 成品核对 --

    def verify_artifact(self, artifact: dict[str, Any] | None) -> dict[str, Any]:
        """核对交付成品：文件是否存在、行数、行范围、摘要是否与记录一致。"""
        if not artifact:
            return {"ok": False, "status": "missing", "error": "尚无已确认成品"}

        path = Path(str(artifact.get("path") or ""))
        exports_dir = (DATA_ROOT / "exports").resolve()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if exports_dir not in resolved.parents:
            return {"ok": False, "status": "invalid_path", "error": "成品路径不在导出目录内"}

        if not path.exists():
            return {
                "ok": False, "status": "missing", "error": "成品文件不存在",
                "delivery_id": artifact.get("delivery_id"),
                "line_from": artifact.get("line_from"), "line_to": artifact.get("line_to"),
                "lines": artifact.get("lines"),
            }

        try:
            actual_lines = self._count_artifact_lines(path, str(artifact.get("format") or "jsonl"))
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError as exc:
            return {"ok": False, "status": "unreadable", "error": f"成品无法读取: {exc}"}

        expected_lines = int(artifact.get("lines") or 0)
        checksum_ok = digest == artifact.get("checksum")
        lines_ok = actual_lines == expected_lines
        ok = bool(checksum_ok and lines_ok)
        return {
            "ok": ok,
            "status": "ready" if ok else "mismatch",
            "delivery_id": artifact.get("delivery_id"),
            "path": str(path),
            "file_name": artifact.get("file_name"),
            "line_from": artifact.get("line_from"),
            "line_to": artifact.get("line_to"),
            "lines": actual_lines,
            "expected_lines": expected_lines,
            "checksum": digest,
            "checksum_match": checksum_ok,
            "size_bytes": path.stat().st_size,
            "error": None if ok else "成品行数或摘要与交付记录不一致",
        }

    def get_confirmed_artifact(self, source_key: str) -> dict[str, Any] | None:
        """取得账本中最新已确认的成品记录（不改变任何状态）。"""
        state = delivery.load(source_key)
        return state.get("artifact")

    # -- 可恢复交付记录 --

    def get_export_manifest(self, source_key: str) -> dict[str, Any]:
        """导出清单：把位点进度、交付记录、成品范围与待办范围放在一起。"""
        state = delivery.load(source_key)
        collect_cp = self._read_checkpoint(source_key) or {}
        checkpoint_line = int(collect_cp.get("line_number") or 0)
        confirmed_line = int(state.get("confirmed_line") or 0)
        artifact = state.get("artifact")
        verify = self.verify_artifact(artifact)

        latest = delivery.latest_delivery(state)
        pending = delivery.pending_range(confirmed_line, checkpoint_line)
        failed = latest is not None and latest.get("status") in ("failed", "generating", "pending")
        downloadable = bool(verify.get("ok"))
        artifact_behind = downloadable and int(verify.get("line_to") or 0) < checkpoint_line
        artifact_broken = confirmed_line > 0 and not downloadable
        can_retry = bool(pending) or artifact_broken

        if pending and failed:
            next_action = "retry_generation"
            status = "partially_committed"
            message = (
                f"位点已推进到第 {checkpoint_line} 行，但成品只到第 "
                f"{verify.get('line_to') or confirmed_line} 行；可重试生成后确认。"
            )
        elif pending:
            next_action = "generate_delivery"
            status = "pending_delivery"
            message = f"第 {pending['from_line']}-{pending['to_line']} 行尚未生成并确认成品。"
        elif artifact_broken:
            next_action = "repair_confirmed"
            status = "artifact_missing"
            message = (
                f"已确认到第 {confirmed_line} 行，但成品文件缺失或校验不一致；"
                "可重试按已确认范围重建。"
            )
        elif artifact_behind:
            next_action = "download_confirmed"
            status = "confirmed_behind_checkpoint"
            message = (
                f"已确认成品到第 {verify.get('line_to')} 行，位点已到第 {checkpoint_line} 行；"
                "当前下载仍是旧成品。"
            )
        else:
            next_action = "download_confirmed" if downloadable else "none"
            status = "confirmed" if downloadable else "no_delivery"
            message = "位点与已确认成品一致。" if downloadable else "尚无已确认成品。"

        return {
            "source_key": source_key,
            "status": status,
            "message": message,
            "checkpoint_line": checkpoint_line,
            "checkpoint_offset": collect_cp.get("offset", 0),
            "checkpoint_status": collect_cp.get("status"),
            "confirmed_line": confirmed_line,
            "pending_range": pending,
            "todo_range": pending,
            "artifact": verify,
            "artifact_record": artifact,
            "downloadable": downloadable,
            "download_is_stale": bool(artifact_behind),
            "can_retry": can_retry,
            "retry_url": f"/api/delivery/{source_key}/retry",
            "download_url": f"/api/delivery/{source_key}/download",
            "last_delivery": latest,
            "deliveries": state.get("deliveries", [])[-20:],
            "downloads": state.get("downloads", [])[-20:],
            "history": state.get("history", [])[-50:],
            "next_action": next_action,
            # 兼容旧清单字段
            "collect": collect_cp,
            "parse": self._read_checkpoint(f"{source_key}_parse"),
            "mapping": self._read_checkpoint(f"{source_key}_map"),
            "export": self._read_checkpoint(f"{source_key}_export"),
        }

    def commit_delivery_checkpoint(self, source_key: str, record: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """按交付记录生成并核对成品；成功返回 (artifact, None)，失败返回 (None, 失败信息)。

        成品是“截至本次位点的累计快照”：交付记录只登记本次新增行段，
        但下载文件包含第 1 行到本次位点的全部已采集行，这样下载到的成品
        与位点确认进度一致，也不会因为增量分段而缺行。
        """
        segment_from = int(record.get("from_line") or 0)
        expected_to = int(record.get("to_line") or 0)
        result = self.export_source(
            source_key,
            str(record.get("format") or "jsonl"),
            from_line=1,
            to_line=expected_to,
            delivery_id=str(record.get("delivery_id") or ""),
        )
        if not result.get("ok"):
            return None, result

        verify = self.verify_artifact(result)
        if not verify.get("ok"):
            return None, {
                "ok": False,
                "error": verify.get("error") or "成品校验失败",
                "reason": "artifact_mismatch",
                "available_to": verify.get("line_to"),
            }

        actual_from = int(result.get("line_from") or 0)
        actual_to = int(result.get("line_to") or 0)
        if actual_to != expected_to:
            return None, {
                "ok": False,
                "error": (
                    f"成品日志范围到第 {actual_to} 行，与交付记录第 {expected_to} 行不一致"
                ),
                "reason": "range_mismatch",
                "available_to": actual_to,
            }
        if actual_from != 1 or actual_from > segment_from:
            return None, {
                "ok": False,
                "error": (
                    f"成品从第 {actual_from} 行开始，未覆盖本次交付段 "
                    f"{segment_from}-{expected_to}"
                ),
                "reason": "range_gap",
                "available_to": actual_to,
            }

        expected_count = actual_to - actual_from + 1
        if int(result.get("lines") or 0) != expected_count:
            return None, {
                "ok": False,
                "error": (
                    f"成品 {result.get('lines')} 行，连续范围应为 {expected_count} 行"
                ),
                "reason": "incomplete_range",
                "available_to": actual_to,
            }

        result["segment_from_line"] = segment_from
        result["segment_to_line"] = expected_to
        return result, None

    # -- 内部实现 --

    @staticmethod
    def _delivery_suffix(delivery_id: str) -> str:
        digits = "".join(ch for ch in delivery_id if ch.isdigit())
        return (digits or "0").zfill(4)

    def _select_range(
        self,
        records: list[dict[str, Any]],
        from_line: int | None,
        to_line: int | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        by_line: dict[int, dict[str, Any]] = {}
        for rec in records:
            line_no = int(rec.get("line_no") or 0)
            if line_no > 0:
                by_line[line_no] = rec

        start = int(from_line or 0)
        end = int(to_line or (max(by_line) if by_line else 0))
        available_to = max(by_line) if by_line else 0
        if start <= 0 or end < start:
            return [], {
                "error": f"无效的导出行范围: {start}-{end}",
                "reason": "invalid_range",
                "available_to": available_to,
            }

        missing = [line for line in range(start, end + 1) if line not in by_line]
        if missing:
            return [], {
                "error": (
                    f"第 {start}-{end} 行不完整，缺少 {len(missing)} 行"
                    f"（首个缺失第 {missing[0]} 行），请先完成解析与字段映射"
                ),
                "reason": "upstream_lag",
                "missing_lines": len(missing),
                "first_missing_line": missing[0],
                "available_to": available_to,
            }

        return [by_line[line] for line in range(start, end + 1)], None

    @staticmethod
    def _available_line(records: list[dict[str, Any]]) -> int:
        lines = [int(rec.get("line_no") or 0) for rec in records]
        return max(lines) if lines else 0

    def _write_records(
        self,
        path: Path,
        records: list[dict[str, Any]],
        format_type: str,
        plan: dict[str, Any] | None,
    ) -> int:
        limit = None
        if plan:
            try:
                limit = int(plan.get("after_lines"))
            except (TypeError, ValueError):
                limit = None

        written = 0
        if format_type == "jsonl":
            with open(path, "w", encoding="utf-8") as f:
                for rec in records:
                    if limit is not None and written >= limit:
                        f.flush()
                        os.fsync(f.fileno())
                        raise GenerationInterrupted(
                            plan.get("error") or f"文件生成在第 {written} 行后中断"
                        )
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                    written += 1
        elif format_type == "json":
            subset = records[:limit] if limit is not None else records
            with open(path, "w", encoding="utf-8") as f:
                json.dump(subset, f, ensure_ascii=False, indent=2, default=str)
            written = len(subset)
            if limit is not None:
                raise GenerationInterrupted(
                    plan.get("error") or f"文件生成在第 {written} 行后中断"
                )
        elif format_type == "gz":
            with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as f:
                for rec in records:
                    if limit is not None and written >= limit:
                        raise GenerationInterrupted(
                            plan.get("error") or f"文件生成在第 {written} 行后中断"
                        )
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                    written += 1
        elif format_type == "csv":
            keys: list[str] = []
            if records:
                keys = list(records[0].get("standardized", records[0]).keys())
            with open(path, "w", encoding="utf-8") as f:
                f.write(",".join(keys) + "\n")
                for rec in records:
                    if limit is not None and written >= limit:
                        f.flush()
                        os.fsync(f.fileno())
                        raise GenerationInterrupted(
                            plan.get("error") or f"文件生成在第 {written} 行后中断"
                        )
                    std = rec.get("standardized", rec)
                    f.write(",".join(str(std.get(k, "")) for k in keys) + "\n")
                    written += 1
        return written

    def _count_artifact_lines(self, path: Path, format_type: str) -> int:
        if format_type == "json":
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return len(payload) if isinstance(payload, list) else 0
        if format_type == "gz":
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        if format_type == "csv":
            with open(path, "r", encoding="utf-8") as f:
                rows = sum(1 for line in f if line.strip())
            return max(rows - 1, 0)
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
