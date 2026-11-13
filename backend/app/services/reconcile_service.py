from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db import connect
from app.engines.reconcile import compare_sides
from app.repositories import batches as batch_repo
from app.repositories import contracts as contract_repo
from app.repositories import pairs as pair_repo
from app.repositories import tickets as ticket_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dashboard_stats() -> dict[str, Any]:
    from app.services import apply_service

    conn = connect()
    try:
        stats = {
            "pair_count": conn.execute("SELECT COUNT(*) c FROM reconcile_pairs").fetchone()["c"],
            "enabled_pairs": conn.execute(
                "SELECT COUNT(*) c FROM reconcile_pairs WHERE enabled=1"
            ).fetchone()["c"],
            "batch_count": conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"],
            "diff_count": conn.execute("SELECT COUNT(*) c FROM reconcile_diffs").fetchone()["c"],
            "open_dispositions": conn.execute(
                "SELECT COUNT(*) c FROM diff_tickets WHERE status='open'"
            ).fetchone()["c"],
            "policy": conn.execute(
                "SELECT value FROM settings WHERE key='default_policy'"
            ).fetchone()["value"],
        }
    finally:
        conn.close()
    apply = apply_service.apply_stats()
    stats["applies"] = apply["applies"]
    stats["rolled_back"] = apply["rolled_back"]
    stats["rollback_failed"] = apply["rollback_failed"]
    stats["dirty_keys"] = apply["dirty_keys"]
    return stats


def list_pairs() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return pair_repo.list_pairs(conn)
    finally:
        conn.close()


def list_batches(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = batch_repo.list_batches(conn, limit=limit)
        pairs = {p["id"]: p for p in pair_repo.list_pairs(conn)}
        for r in rows:
            p = pairs.get(r["pair_id"])
            r["pair_code"] = p["code"] if p else None
            r["pair_title"] = p["title"] if p else None
        return rows
    finally:
        conn.close()


def list_diffs(batch_id: int | None = None, pair_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_diffs(conn, batch_id=batch_id, pair_id=pair_id)
    finally:
        conn.close()


def list_dispositions() -> list[dict[str, Any]]:
    """Difference disposition records (not a customer-service ticket desk)."""
    conn = connect()
    try:
        return ticket_repo.list_tickets(conn)
    finally:
        conn.close()


def list_contracts() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return contract_repo.list_contracts(conn)
    finally:
        conn.close()


def list_settings() -> dict[str, str]:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def run_reconcile(pair_id: int, note: str | None = None) -> dict[str, Any]:
    """Extract both sides (fixture + 侧数据 overlay), align, persist batch + diffs.

    重对：批次落库完成后，清除本对账对全部脏标记（dirty_cleared 留痕）——
    这是脏标记唯一的清除通道，回滚失败/进行中不会走到这里。
    """
    from app.services import apply_service

    conn = connect()
    try:
        pair = pair_repo.get_pair(conn, pair_id)
        if not pair:
            raise ValueError("pair not found")
        if not pair["enabled"]:
            raise ValueError("pair disabled")

        left_rows = apply_service.effective_rows(conn, pair, "left")
        right_rows = apply_service.effective_rows(conn, pair, "right")
        result = compare_sides(
            left_rows,
            right_rows,
            align_keys=pair["align_keys"],
            compare_fields=pair["compare_fields"],
        )
        now = _now()
        seq = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
        batch_no = f"RB-{now[:10].replace('-', '')}-{seq:04d}"
        batch_id = batch_repo.insert_batch(
            conn,
            batch_no=batch_no,
            pair_id=pair_id,
            status="completed",
            counts=result["counts"],
            note=note,
            created_at=now,
        )
        for diff in result["diffs"]:
            if diff["kind"] == "matched":
                continue
            batch_repo.insert_diff(
                conn, batch_id=batch_id, pair_id=pair_id, diff=diff, created_at=now
            )
        dirty_cleared = apply_service.clear_dirty_for_pair(conn, pair_id, batch_id=batch_id, now=now)
        conn.commit()
        batch = batch_repo.get_batch(conn, batch_id)
        assert batch is not None
        batch["pair_code"] = pair["code"]
        batch["pair_title"] = pair["title"]
        batch["policy"] = "record_only"
        batch["counts"] = result["counts"]
        batch["dirty_cleared"] = dirty_cleared
        batch["diff_preview"] = batch_repo.list_diffs(conn, batch_id=batch_id, limit=20)
        return batch
    finally:
        conn.close()


def audit_summary(batch_id: int) -> dict[str, Any]:
    """Lightweight audit view for a batch (export hooks left for later cuts)."""
    conn = connect()
    try:
        batch = batch_repo.get_batch(conn, batch_id)
        if not batch:
            raise ValueError("batch not found")
        pair = pair_repo.get_pair(conn, batch["pair_id"])
        diffs = batch_repo.list_diffs(conn, batch_id=batch_id)
        return {
            "batch": batch,
            "pair": pair,
            "diff_count": len(diffs),
            "kinds": {
                "left_only": sum(1 for d in diffs if d["kind"] == "left_only"),
                "right_only": sum(1 for d in diffs if d["kind"] == "right_only"),
                "field_mismatch": sum(1 for d in diffs if d["kind"] == "field_mismatch"),
            },
            "export_ready": False,
            "note": "底座仅提供摘要；审计包导出留给后题切口",
        }
    finally:
        conn.close()
