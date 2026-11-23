"""采集位点管理：采集进度记录、提交、回滚和交付确认。

位点提交不再只改一个状态字段，而是生成一份可恢复的交付记录：

1. 先把本次提交的行范围、偏移和格式写入交付账本（pending）；
2. 再生成成品并按行范围/行数/摘要校验；
3. 校验通过后才确认位点，并把成品标记为可下载；
4. 生成失败时位点进度照常保留，旧成品保持原样，记录为可重试状态。

因此“确认进度”与“实际可读成品”始终能对上，重复提交也不会重复确认。
"""
from __future__ import annotations

import time
from typing import Any

from app.services import delivery
from app.services.base import BaseModuleService
from app.services.delivery import source_lock
from app.store import store


class CollectCheckpointService(BaseModuleService):
    MODULE = "collect_checkpoint"
    ENTITY = "位点记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["记录编号", "关联源", "文件偏移", "处理行号", "位点时间", "校验摘要", "提交状态", "回滚位点"]
    REQUIRED_FIELDS = ["记录编号", "关联源", "文件偏移"]
    STATUS_ORDER = ["采集中", "已提交", "已回滚", "已过期"]
    ACTION_RULES = {"确认提交": "已提交", "发起回滚": "已回滚", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    # -- 采集进度 --

    def save_checkpoint(self, source_key: str, offset: int, line_number: int) -> dict[str, Any]:
        """保存采集位点状态（采集前进度，不代表成品已经可下载）。"""
        with source_lock(source_key):
            state = {
                "source_key": source_key,
                "offset": offset,
                "line_number": line_number,
                "status": "active",
                "updated_at": time.time(),
            }
            self._write_checkpoint(source_key, state)
            self._append_history(source_key, {
                "source_key": source_key, "offset": offset,
                "line_number": line_number, "at": time.time(), "action": "save",
            })
            ledger = delivery.load(source_key)
            delivery.record_event(
                source_key, ledger, "progress_saved",
                offset=offset, line_number=line_number,
                confirmed_line=ledger.get("confirmed_line", 0),
            )
            delivery.save(source_key, ledger)
        return state

    def rollback_to(self, source_key: str, to_offset: int) -> dict[str, Any]:
        """将指定位点回滚到指定偏移位置。"""
        with source_lock(source_key):
            cp = self._read_checkpoint(source_key)
            if cp is None:
                return {"error": f"位点 {source_key} 不存在"}

            previous_offset = cp.get("offset", 0)
            cp["offset"] = to_offset
            cp["status"] = "rolled_back"
            cp["rolled_back_at"] = time.time()
            cp["previous_offset"] = previous_offset
            self._write_checkpoint(source_key, cp)

            self._append_history(source_key, {
                "source_key": source_key, "offset": to_offset, "at": time.time(), "action": "rollback",
            })
            ledger = delivery.load(source_key)
            for record in ledger.get("deliveries") or []:
                if record.get("status") not in ("delivered", "superseded"):
                    record["status"] = "abandoned"
                    record["state"] = "abandoned"
                    record["can_retry"] = True
            delivery.record_event(
                source_key, ledger, "rollback",
                to_offset=to_offset, previous_offset=previous_offset,
                confirmed_line=ledger.get("confirmed_line", 0),
            )
            delivery.save(source_key, ledger)
        return cp

    def get_checkpoint_state(self, source_key: str) -> dict[str, Any]:
        """获取指定源的位点信息。"""
        cp = self._read_checkpoint(source_key)
        if cp is None:
            return {"source_key": source_key, "status": "no_checkpoint", "offset": 0, "line_number": 0}
        return cp

    def list_all_checkpoints(self) -> list[dict[str, Any]]:
        """列出所有位点状态。"""
        all_cps = []
        for src in self._collect_sources():
            key = src["源编号"]
            cp = self._read_checkpoint(key)
            if cp:
                all_cps.append(cp)
        return all_cps

    # -- 提交与交付 --

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """位点记录的“确认提交”动作走可恢复交付链路，而不是只改状态字段。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "确认提交":
            source_key = str(entry.get("关联源") or "").strip()
            cp = self._read_checkpoint(source_key) if source_key else None
            if cp is not None and int(cp.get("line_number") or 0) > 0:
                result = self.commit_checkpoint(source_key)
                entry["status"] = "已提交"
                entry["pending"] = False
                entry["delivery_status"] = result.get("delivery_status")
                entry["confirmed_line"] = result.get("confirmed_line")
                if result.get("partial"):
                    return entry, (
                        f"{self.ENTITY}已提交到位点第 {result.get('line_number')} 行，"
                        f"但成品未生成完成：{result.get('message')}"
                    )
                return entry, f"{self.ENTITY}已提交，{result.get('message')}"
        return super().run_action(entry_id, action)

    def commit_checkpoint(self, source_key: str, format_type: str = "jsonl") -> dict[str, Any]:
        """提交采集位点，并生成、校验、确认对应行范围的日志成品。

        返回结构区分两件事：``committed`` 表示位点是否已经推进；
        ``delivery_status`` 表示成品是否就绪。部分成功时位点不回退，
        成品失败记录保留在交付账本里，可继续重试。
        """
        with source_lock(source_key):
            ledger = delivery.load(source_key)
            delivery.reconcile(source_key, ledger)

            cp = self._read_checkpoint(source_key)
            if cp is None:
                return {"ok": False, "error": f"位点 {source_key} 不存在"}

            line_number = int(cp.get("line_number") or 0)
            offset = int(cp.get("offset") or 0)
            confirmed_line = int(ledger.get("confirmed_line") or 0)
            result: dict[str, Any] = {
                "source_key": source_key,
                "line_number": line_number,
                "offset": offset,
                "previous_confirmed_line": confirmed_line,
            }

            if line_number <= 0:
                cp["status"] = "committed"
                cp["committed_at"] = time.time()
                self._write_checkpoint(source_key, cp)
                result.update({
                    "ok": True,
                    "committed": True,
                    "complete": True,
                    "delivery_status": "empty",
                    "confirmed_line": confirmed_line,
                    "message": "位点尚无已采集行，无需生成成品",
                })
                return result

            verify = self._verify_confirmed_artifact(source_key, ledger, line_number)
            if line_number <= confirmed_line:
                if verify.get("ok") and int(verify.get("line_to") or 0) >= line_number:
                    # 已确认过同一行范围：只做核对，不重复生成、不重复确认。
                    unchanged = self.verify_checksum_existing(ledger)
                    delivery.record_event(
                        source_key, ledger, "commit_replayed",
                        line_number=line_number,
                        delivery_id=ledger.get("last_confirmed_delivery_id"),
                        checksum_match=unchanged,
                    )
                    delivery.save(source_key, ledger)
                    cp["status"] = "committed"
                    cp.setdefault("committed_at", time.time())
                    cp["last_replayed_at"] = time.time()
                    cp["last_delivery_id"] = ledger.get("last_confirmed_delivery_id")
                    cp["confirmed_line"] = int(ledger.get("confirmed_line") or 0)
                    self._write_checkpoint(source_key, cp)
                    result.update({
                        "ok": True,
                        "committed": True,
                        "complete": True,
                        "replayed": True,
                        "already_confirmed": True,
                        "confirmed_line": int(ledger.get("confirmed_line") or 0),
                        "delivery_status": "delivered",
                        "artifact": verify,
                        "auto_export": verify,
                        "message": "该位点已确认过对应成品，本次仅复核，未重复确认",
                    })
                    return result

                # 位点没有新行，但已确认的成品缺失/损坏：按原确认范围修复一次。
                return self._repair_confirmed(source_key, ledger, cp, result, format_type)

            # 找到可继续使用的待生成记录：同一目标行、同一格式且尚未成功。
            target_line = line_number if line_number > confirmed_line else int(ledger.get("confirmed_line") or 0)
            record = self._reusable_delivery(ledger, target_line, format_type)
            if record is None:
                record = delivery.create_pending(
                    ledger,
                    source_key=source_key,
                    from_line=int(ledger.get("confirmed_line") or 0) + 1,
                    to_line=target_line,
                    offset=offset,
                    format_type=format_type,
                    previous_confirmed_line=int(ledger.get("confirmed_line") or 0),
                    reason="checkpoint_commit",
                )
            else:
                delivery.record_event(
                    source_key, ledger, "delivery_retried",
                    delivery_id=record.get("delivery_id"),
                    from_line=record.get("from_line"), to_line=record.get("to_line"),
                )

            # 位点先落“已提交”，随后再生成文件；失败时这份进度仍然可追查。
            cp["status"] = "committed"
            cp["committed_at"] = time.time()
            cp["last_delivery_id"] = record["delivery_id"]
            cp["last_submitted_line"] = line_number
            self._write_checkpoint(source_key, cp)
            self._append_history(source_key, {
                "source_key": source_key, "offset": offset, "line_number": line_number,
                "at": time.time(), "action": "commit",
                "delivery_id": record["delivery_id"],
            })

            delivery.start_attempt(source_key, ledger, record)
            artifact, failure = self._generate_and_verify(source_key, record)
            if failure is not None:
                preserved = ledger.get("artifact")
                delivery.fail(
                    source_key, ledger, record,
                    error=str(failure.get("error") or "成品生成失败"),
                    reason=str(failure.get("reason") or "generation_failed"),
                    available_to=(preserved or {}).get("line_to"),
                    preserved_artifact=preserved,
                )
                failed_verify = self._verify_confirmed_artifact(
                    source_key, ledger, int(ledger.get("confirmed_line") or 0)
                )
                self._refresh_checkpoint_delivery(source_key, ledger)
                result.update({
                    "ok": True,
                    "committed": True,
                    "complete": False,
                    "partial": True,
                    "confirmed_line": int(ledger.get("confirmed_line") or 0),
                    "pending_line": int(ledger.get("pending_line") or target_line),
                    "pending_range": delivery.pending_range(
                        int(ledger.get("confirmed_line") or 0), line_number),
                    "delivery_status": "failed",
                    "delivery": self._delivery_public(record),
                    "can_retry": True,
                    "retry_url": f"/api/delivery/{source_key}/retry",
                    "downloadable": bool(failed_verify.get("ok")),
                    "download_is_stale": bool(
                        failed_verify.get("ok")
                        and int(failed_verify.get("line_to") or 0) < line_number
                    ),
                    "available_artifact": failed_verify,
                    "auto_export": {
                        "ok": False,
                        "error": failure.get("error"),
                        "error_code": failure.get("reason"),
                    },
                    "message": (
                        f"位点已推进到第 {line_number} 行，成品生成失败；"
                        f"仍可下载第 {failed_verify.get('line_to') or 0} 行成品，重试后确认"
                    ),
                })
                self._trigger_quality_safe(source_key, result)
                return result

            # 成品校验通过：原子更新正式导出文件后再确认交付。
            canonical_error = self._publish_canonical(source_key, artifact)
            if canonical_error is not None:
                delivery.fail(
                    source_key, ledger, record,
                    error=canonical_error,
                    reason="publish_failed",
                    available_to=(ledger.get("artifact") or {}).get("line_to"),
                    preserved_artifact=ledger.get("artifact"),
                )
                self._refresh_checkpoint_delivery(source_key, ledger)
                result.update({
                    "ok": True,
                    "committed": True,
                    "complete": False,
                    "partial": True,
                    "confirmed_line": int(ledger.get("confirmed_line") or 0),
                    "delivery_status": "failed",
                    "delivery": self._delivery_public(record),
                    "can_retry": True,
                    "retry_url": f"/api/delivery/{source_key}/retry",
                    "auto_export": {"ok": False, "error": canonical_error},
                    "message": "位点已推进，但正式成品发布失败；旧成品保持不变，可重试",
                })
                self._trigger_quality_safe(source_key, result)
                return result

            confirmed = delivery.confirm(
                source_key, ledger, record,
                artifact=artifact,
                confirmed_line=target_line,
            )
            self._refresh_checkpoint_delivery(source_key, ledger)
            result.update({
                "ok": True,
                "committed": True,
                "complete": True,
                "confirmed_line": int(ledger.get("confirmed_line") or 0),
                "pending_line": 0,
                "pending_range": None,
                "delivery_status": "delivered",
                "delivery": self._delivery_public(confirmed),
                "artifact": self.verify_checksum_existing(ledger),
                "auto_export": artifact,
                "downloadable": True,
                "download_url": f"/api/delivery/{source_key}/download",
                "message": (
                    f"位点第 {line_number} 行已提交，成品第 "
                    f"{artifact.get('line_from')}-{artifact.get('line_to')} 行已确认"
                ),
            })
            self._trigger_quality_safe(source_key, result)
            return result

    def _repair_confirmed(
        self, source_key: str, ledger: dict[str, Any], cp: dict[str, Any],
        result: dict[str, Any], format_type: str,
    ) -> dict[str, Any]:
        """已确认成品缺失或损坏时，按原确认范围重建一次，不改变确认行号。"""
        confirmed_line = int(ledger.get("confirmed_line") or 0)
        if confirmed_line <= 0:
            result.update({
                "ok": True,
                "committed": True,
                "complete": True,
                "delivery_status": "empty",
                "confirmed_line": 0,
                "message": "位点尚无已采集行，无需生成成品",
            })
            return result

        fmt = format_type or self._last_format(ledger) or "jsonl"
        previous = ledger.get("artifact") or {}
        path = previous.get("path")
        delivery.record_event(
            source_key, ledger, "artifact_repair_started",
            confirmed_line=confirmed_line, format=fmt, previous_path=path,
        )
        delivery.save(source_key, ledger)

        from app.services.format_output import FormatOutputService
        rebuilt = FormatOutputService().export_source(
            source_key, fmt, from_line=1, to_line=confirmed_line,
            delivery_id=str(previous.get("delivery_id") or "repair") + "-r",
        )
        if not rebuilt.get("ok"):
            delivery.record_event(
                source_key, ledger, "artifact_repair_failed",
                confirmed_line=confirmed_line, error=rebuilt.get("error"),
            )
            delivery.save(source_key, ledger)
            self._refresh_checkpoint_delivery(source_key, ledger)
            result.update({
                "ok": False,
                "committed": True,
                "complete": False,
                "partial": True,
                "confirmed_line": confirmed_line,
                "delivery_status": "repair_failed",
                "can_retry": True,
                "retry_url": f"/api/delivery/{source_key}/retry",
                "error": rebuilt.get("error") or "已确认成品重建失败",
                "message": "位点未变化，但已确认成品缺失且重建失败，可重试",
            })
            return result

        rebuilt["delivery_id"] = previous.get("delivery_id") or ""
        canonical_error = self._publish_canonical(source_key, rebuilt)
        if canonical_error is not None:
            delivery.record_event(
                source_key, ledger, "artifact_repair_failed",
                confirmed_line=confirmed_line, error=canonical_error,
            )
            delivery.save(source_key, ledger)
            result.update({
                "ok": False,
                "committed": True,
                "complete": False,
                "partial": True,
                "confirmed_line": confirmed_line,
                "delivery_status": "repair_failed",
                "can_retry": True,
                "retry_url": f"/api/delivery/{source_key}/retry",
                "error": canonical_error,
                "message": "位点未变化，但已确认成品发布失败，可重试",
            })
            return result

        ledger["artifact"] = rebuilt
        for record in ledger.get("deliveries") or []:
            if record.get("delivery_id") == rebuilt["delivery_id"]:
                record["artifact"] = rebuilt
        delivery.record_event(
            source_key, ledger, "artifact_repaired",
            confirmed_line=confirmed_line, artifact_line_to=rebuilt.get("line_to"),
        )
        delivery.save(source_key, ledger)
        self._refresh_checkpoint_delivery(source_key, ledger)
        result.update({
            "ok": True,
            "committed": True,
            "complete": True,
            "repaired": True,
            "confirmed_line": confirmed_line,
            "delivery_status": "delivered",
            "artifact": self.verify_checksum_existing(ledger),
            "auto_export": rebuilt,
            "message": f"已确认成品缺失/损坏，已按第 1-{confirmed_line} 行重建，确认行号未变",
        })
        return result

    def retry_delivery(self, source_key: str, format_type: str = "") -> dict[str, Any]:
        """对未完成的交付记录重新生成成品；不新建重复的交付记录。

        已确认成品本身缺失/损坏时，按原确认范围重建，不改变确认行号。
        """
        with source_lock(source_key):
            ledger = delivery.load(source_key)
            delivery.reconcile(source_key, ledger)
            cp = self._read_checkpoint(source_key)
            if cp is None:
                return {"ok": False, "status": "no_checkpoint", "error": f"位点 {source_key} 不存在"}

            line_number = int(cp.get("line_number") or 0)
            confirmed_line = int(ledger.get("confirmed_line") or 0)
            effective_format = format_type or self._last_format(ledger) or "jsonl"
            record = self._reusable_delivery(
                ledger, line_number, effective_format,
            ) if line_number > confirmed_line else None

            if record is None:
                verify = self._verify_confirmed_artifact(source_key, ledger, line_number)
                if line_number <= confirmed_line and verify.get("ok"):
                    return {
                        "ok": True,
                        "status": "already_confirmed",
                        "delivery_status": "delivered",
                        "confirmed_line": confirmed_line,
                        "line_number": line_number,
                        "pending_range": None,
                        "artifact": verify,
                        "message": "该位点已有对得上的已确认成品，无需重复生成",
                    }
                if line_number <= confirmed_line and not verify.get("ok"):
                    result: dict[str, Any] = {"source_key": source_key}
                    delivery.record_event(
                        source_key, ledger, "repair_requested",
                        confirmed_line=confirmed_line,
                    )
                    delivery.save(source_key, ledger)
                    return self._repair_confirmed(
                        source_key, ledger, cp, result, effective_format)
                # 没有失败记录也要能按当前位点补一次交付。
                return self.commit_checkpoint(source_key, effective_format)

            delivery.record_event(
                source_key, ledger, "retry_requested",
                delivery_id=record["delivery_id"],
                from_line=record.get("from_line"), to_line=record.get("to_line"),
            )
            delivery.start_attempt(source_key, ledger, record)
            artifact, failure = self._generate_and_verify(source_key, record)
            if failure is not None:
                delivery.fail(
                    source_key, ledger, record,
                    error=str(failure.get("error") or "成品生成失败"),
                    reason=str(failure.get("reason") or "generation_failed"),
                    available_to=(ledger.get("artifact") or {}).get("line_to"),
                    preserved_artifact=ledger.get("artifact"),
                )
                self._refresh_checkpoint_delivery(source_key, ledger)
                return {
                    "ok": False,
                    "status": "failed",
                    "delivery_status": "failed",
                    "can_retry": True,
                    "error": failure.get("error"),
                    "error_code": failure.get("reason"),
                    "confirmed_line": int(ledger.get("confirmed_line") or 0),
                    "pending_range": delivery.pending_range(
                        int(ledger.get("confirmed_line") or 0), line_number),
                    "delivery": self._delivery_public(record),
                    "downloadable": bool(
                        (ledger.get("artifact") or {}).get("line_to")
                        and self._verify_confirmed_artifact(
                            source_key, ledger, int(ledger.get("confirmed_line") or 0)).get("ok")
                    ),
                    "retry_url": f"/api/delivery/{source_key}/retry",
                }

            canonical_error = self._publish_canonical(source_key, artifact)
            if canonical_error is not None:
                delivery.fail(
                    source_key, ledger, record, error=canonical_error,
                    reason="publish_failed",
                    available_to=(ledger.get("artifact") or {}).get("line_to"),
                    preserved_artifact=ledger.get("artifact"),
                )
                self._refresh_checkpoint_delivery(source_key, ledger)
                return {
                    "ok": False, "status": "failed", "delivery_status": "failed",
                    "can_retry": True, "error": canonical_error,
                    "delivery": self._delivery_public(record),
                }

            confirmed = delivery.confirm(
                source_key, ledger, record,
                artifact=artifact,
                confirmed_line=int(record.get("to_line") or line_number),
            )
            self._refresh_checkpoint_delivery(source_key, ledger)
            return {
                "ok": True,
                "status": "delivered",
                "delivery_status": "delivered",
                "confirmed_line": int(ledger.get("confirmed_line") or 0),
                "pending_range": None,
                "delivery": self._delivery_public(confirmed),
                "artifact": artifact,
                "download_url": f"/api/delivery/{source_key}/download",
                "message": (
                    f"已确认第 {artifact.get('line_from')}-{artifact.get('line_to')} 行成品"
                ),
            }

    def verify_checksum_existing(self, ledger: dict[str, Any]) -> dict[str, Any]:
        """核对账本中已确认的成品（不改变账本状态）。"""
        from app.services.format_output import FormatOutputService  # 延迟导入避免循环
        return FormatOutputService().verify_artifact(ledger.get("artifact"))

    def verify_checksum(self, source_key: str, expected_checksum: str) -> dict[str, Any]:
        """验证采集位点的校验摘要。"""
        cp = self._read_checkpoint(source_key)
        if cp is None:
            return {"source_key": source_key, "status": "no_checkpoint", "match": False}

        actual = cp.get("checksum", "")
        match = actual == expected_checksum
        cp["last_verified_at"] = time.time()
        cp["last_verified_match"] = match
        self._write_checkpoint(source_key, cp)
        return {"source_key": source_key, "match": match, "expected": expected_checksum, "actual": actual}

    # -- 内部工具 --

    def _generate_and_verify(
        self, source_key: str, record: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        from app.services.format_output import FormatOutputService

        return FormatOutputService().commit_delivery_checkpoint(source_key, record)

    def _publish_canonical(self, source_key: str, artifact: dict[str, Any]) -> str | None:
        """把已校验的编号成品原子替换到正式导出文件；失败时不动旧文件。"""
        import os
        from pathlib import Path

        numbered = Path(str(artifact.get("path") or ""))
        suffix = "".join(Path(str(artifact.get("file_name") or "")).suffixes) or ".jsonl"
        canonical = self._data_dir("exports") / f"{source_key}_export{suffix}"
        tmp = canonical.with_name(f".{canonical.name}.tmp-{os.getpid()}-{time.time_ns()}")
        try:
            with open(numbered, "rb") as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, canonical)
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return f"正式成品发布失败: {exc}"
        artifact["canonical_path"] = str(canonical)
        return None

    def _verify_confirmed_artifact(
        self, source_key: str, ledger: dict[str, Any], line_number: int,
    ) -> dict[str, Any]:
        verify = self.verify_checksum_existing(ledger)
        if verify.get("ok") and int(verify.get("line_to") or 0) < line_number:
            verify = dict(verify)
            verify["ok"] = False
            verify["status"] = "stale"
            verify["error"] = (
                f"已确认成品只到第 {verify.get('line_to')} 行，位点已到第 {line_number} 行"
            )
        return verify

    def _reusable_delivery(
        self, ledger: dict[str, Any], target_line: int, format_type: str,
    ) -> dict[str, Any] | None:
        """查找能继续重试的交付记录：目标行一致且尚未成功。

        ``format_type`` 只用于说明预期格式；同一位点的失败/待生成记录
        一律复用，重试时沿用记录里的格式，避免清单里出现两条“同一行”待办。
        """
        for record in reversed(ledger.get("deliveries") or []):
            if int(record.get("to_line") or 0) != int(target_line):
                continue
            status = record.get("status")
            if status in ("pending", "failed", "generating"):
                return record
            if status == "delivered":
                return None
        return None

    @staticmethod
    def _last_format(ledger: dict[str, Any]) -> str:
        latest = delivery.latest_delivery(ledger)
        return str((latest or {}).get("format") or "")

    def _refresh_checkpoint_delivery(self, source_key: str, ledger: dict[str, Any]) -> None:
        cp = self._read_checkpoint(source_key)
        if cp is None:
            return
        latest = delivery.latest_delivery(ledger)
        cp["status"] = "committed" if ledger.get("confirmed_line") else cp.get("status", "active")
        cp["last_delivery_id"] = (latest or {}).get("delivery_id") or cp.get("last_delivery_id")
        cp["confirmed_line"] = int(ledger.get("confirmed_line") or 0)
        cp["pending_line"] = int(ledger.get("pending_line") or 0)
        cp["delivery_status"] = (latest or {}).get("status")
        self._write_checkpoint(source_key, cp)

    @staticmethod
    def _delivery_public(record: dict[str, Any]) -> dict[str, Any]:
        public = dict(record)
        public.pop("preserved_artifact", None)
        return public

    def _trigger_quality_safe(self, source_key: str, result: dict[str, Any]) -> None:
        quality_r = self._trigger_quality_diagnosis(source_key)
        if "error" not in quality_r:
            result["auto_quality"] = quality_r

    def _append_history(self, source_key: str, entry: dict[str, Any]) -> None:
        self._write_jsonl(
            self._data_dir("checkpoints") / "checkpoint_history.jsonl", [entry])
