"""Auditable apply (盖写) and rollback (回滚) over reconcile diffs.

Rules enforced here (see README "盖写与回滚"):

1. 盖写前必须留旧值：目标端当前行整体快照进 old_values；目标端无此行（无旧值）
   一律拒绝盖写，留 apply_blocked 痕，绝不记 applied。
2. 已盖写可回滚到旧值并标脏待重对：回滚成功 -> rolled_back + dirty；
   脏标记只能被「重对」（本对账对新跑一趟对账批次）清除，绝不提前清除。
3. 回滚进行中（rolling_back）同一键禁止再次盖写。
4. 回滚失败不得标成功：恢复检查失败 -> rollback_failed + dirty，留痕。
5. 批量回滚策略 = partial_success（部分成功）：逐键独立提交，已回滚键保持
   回滚结果，失败键留痕，批次按结果记 completed / partial / failed；
   不做整批补偿回盖。意外异常则该键停在 rolling_back、批次停在 running，
   由留痕还原现场。
6. 计数一致性：盖写数 / 回滚成功失败数 / 仍脏键数由 apply_stats 与留痕
   （apply_events）及侧数据（side_values）交叉核对。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.repositories import applies as apply_repo
from app.repositories import batches as batch_repo
from app.repositories import pairs as pair_repo

STATUS_APPLIED = "applied"
STATUS_ROLLING_BACK = "rolling_back"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_ROLLBACK_FAILED = "rollback_failed"

DIRECTIONS = {"left_to_right": ("left", "right"), "right_to_left": ("right", "left")}

# 批量回滚策略：部分成功。已回滚键保持结果、失败键留痕、批次记部分成功。
STRATEGY_PARTIAL_SUCCESS = "partial_success"


class ApplyRejected(ValueError):
    """盖写被拒绝（留 apply_blocked 痕）。"""


class RollbackCheckFailed(Exception):
    """回滚恢复检查失败（记 rollback_failed + dirty，留痕）。"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def key_hash_of(align_key: dict[str, Any]) -> str:
    return json.dumps(align_key, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _load_fixture(name: str) -> list[dict[str, Any]]:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"fixture {name} must be a JSON array")
    return data


def _fixture_row(pair: dict[str, Any], side: str, align_key: dict[str, Any]) -> dict[str, Any] | None:
    rows = _load_fixture(pair[f"{side}_fixture"])
    for row in rows:
        if all(row.get(k) == v for k, v in align_key.items()):
            return row
    return None


def current_side_row(
    conn, pair: dict[str, Any], side: str, align_key: dict[str, Any]
) -> dict[str, Any] | None:
    """Effective current row on one side: overlay (侧数据) wins, else fixture."""
    overlay = apply_repo.get_side_value(conn, pair["id"], side, key_hash_of(align_key))
    if overlay is not None:
        return overlay["values"]
    return _fixture_row(pair, side, align_key)


def effective_rows(conn, pair: dict[str, Any], side: str) -> list[dict[str, Any]]:
    """Fixture rows with the mutable overlay applied — what reconcile reads."""
    rows = _load_fixture(pair[f"{side}_fixture"])
    overlays = {
        sv["key_hash"]: sv["values"]
        for sv in apply_repo.list_side_values(conn, pair_id=pair["id"], side=side)
    }
    if not overlays:
        return rows
    out = []
    for row in rows:
        kh = key_hash_of({k: row.get(k) for k in pair["align_keys"]})
        out.append(overlays.get(kh, row))
    return out


# --------------------------------------------------------------------- 盖写


def apply_diff(diff_id: int, direction: str) -> dict[str, Any]:
    """Overwrite one side's compare fields with the other side's values.

    Never succeeds without a restorable old-value snapshot.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction: {direction}")
    source_side, target_side = DIRECTIONS[direction]
    conn = connect()
    try:
        diff = batch_repo.get_diff(conn, diff_id)
        if not diff:
            raise ValueError("diff not found")
        pair = pair_repo.get_pair(conn, diff["pair_id"])
        if not pair:
            raise ValueError("pair not found")
        align_key = diff["align_key"]
        key_hash = key_hash_of(align_key)
        now = _now()

        def blocked(reason: str) -> ApplyRejected:
            apply_repo.insert_event(
                conn,
                event="apply_blocked",
                pair_id=pair["id"],
                align_key=align_key,
                detail={"reason": reason, "diff_id": diff_id, "direction": direction},
                now=now,
            )
            conn.commit()
            return ApplyRejected(reason)

        if diff["kind"] != "field_mismatch":
            raise blocked("单侧差异目标端无旧值可留痕，禁止盖写")

        lock = apply_repo.rolling_back_record_for_key(conn, pair["id"], key_hash)
        if lock is not None:
            raise blocked(f"该键回滚进行中（盖写记录 #{lock['id']}），禁止再次盖写")

        source_row = current_side_row(conn, pair, source_side, align_key)
        target_row = current_side_row(conn, pair, target_side, align_key)
        if source_row is None:
            raise blocked("源端无此行，无值可盖")
        if target_row is None:
            # 无旧值 -> 不得允许盖写成功
            raise blocked("目标端无旧值可留痕，禁止盖写")

        old_values = dict(target_row)  # 盖写前留可回退旧值（整行快照）
        new_values = dict(target_row)
        for f in pair["compare_fields"]:
            new_values[f] = source_row.get(f)

        apply_repo.upsert_side_value(
            conn,
            pair_id=pair["id"],
            side=target_side,
            key_hash=key_hash,
            align_key=align_key,
            values=new_values,
            now=now,
        )
        apply_id = apply_repo.insert_record(
            conn,
            pair_id=pair["id"],
            diff_id=diff_id,
            align_key=align_key,
            key_hash=key_hash,
            direction=direction,
            target_side=target_side,
            fields=pair["compare_fields"],
            old_values=old_values,
            new_values=new_values,
            status=STATUS_APPLIED,
            now=now,
        )
        apply_repo.insert_event(
            conn,
            event="apply",
            apply_id=apply_id,
            pair_id=pair["id"],
            align_key=align_key,
            detail={
                "diff_id": diff_id,
                "direction": direction,
                "fields": pair["compare_fields"],
                "old_values": old_values,
                "new_values": new_values,
            },
            now=now,
        )
        conn.commit()
        record = apply_repo.get_record(conn, apply_id)
        assert record is not None
        return record
    finally:
        conn.close()


# --------------------------------------------------------------------- 回滚


def _rollback_one(conn, record: dict[str, Any], *, batch_id: int, now: str) -> dict[str, Any]:
    """Roll back a single record inside the batch. Per-key commit.

    Expected check failures -> rollback_failed + dirty（不标成功、不清脏）。
    Ineligible attempts -> rollback_rejected 留痕，记录原样（不置失败状态）。
    Unexpected exceptions propagate: the record stays rolling_back and the
    batch stays running, so the half-done state is visible and locks the key.
    """
    apply_id = record["id"]
    pair_id = record["pair_id"]
    align_key = record["align_key"]
    key_hash = record["key_hash"]

    def reject(reason: str) -> dict[str, Any]:
        apply_repo.insert_event(
            conn,
            event="rollback_rejected",
            apply_id=apply_id,
            pair_id=pair_id,
            rollback_batch_id=batch_id,
            align_key=align_key,
            detail={"reason": reason},
            now=now,
        )
        conn.commit()
        return {"apply_id": apply_id, "align_key": align_key, "ok": False, "reason": reason}

    if record["status"] != STATUS_APPLIED:
        return reject(f"记录状态为 {record['status']}，非盖写中，无法回滚")
    in_flight = apply_repo.rolling_back_record_for_key(conn, pair_id, key_hash)
    if in_flight is not None:
        return reject(f"该键已有回滚进行中（记录 #{in_flight['id']}），禁止并发回滚")
    latest_applied = apply_repo.latest_applied_record_for_key(conn, pair_id, key_hash)
    if latest_applied is None or latest_applied["id"] != apply_id:
        return reject(
            f"该键存在更新的盖写记录 #{latest_applied['id'] if latest_applied else '?'}，须先回滚最新一笔"
        )

    # 进入回滚中：同一键禁止再次盖写（apply_diff 会检查 rolling_back）。
    apply_repo.set_record_status(conn, apply_id, status=STATUS_ROLLING_BACK, now=now)
    apply_repo.insert_event(
        conn,
        event="rollback_start",
        apply_id=apply_id,
        pair_id=pair_id,
        rollback_batch_id=batch_id,
        align_key=align_key,
        detail=None,
        now=now,
    )
    conn.commit()

    try:
        old_values = record["old_values"]
        if old_values is None:
            raise RollbackCheckFailed("缺旧值留痕，无法回滚")
        side_row = apply_repo.get_side_value(conn, pair_id, record["target_side"], key_hash)
        if side_row is None:
            raise RollbackCheckFailed("侧数据缺失，无法安全回滚")
        drifted = [
            f
            for f in record["fields"]
            if side_row["values"].get(f) != record["new_values"].get(f)
        ]
        if drifted:
            raise RollbackCheckFailed(f"当前值已漂移（{', '.join(drifted)}），回滚会盖花账")

        apply_repo.upsert_side_value(
            conn,
            pair_id=pair_id,
            side=record["target_side"],
            key_hash=key_hash,
            align_key=align_key,
            values=old_values,
            now=now,
        )
        apply_repo.set_record_status(
            conn, apply_id, status=STATUS_ROLLED_BACK, dirty=True, now=now
        )
        apply_repo.insert_event(
            conn,
            event="rollback_success",
            apply_id=apply_id,
            pair_id=pair_id,
            rollback_batch_id=batch_id,
            align_key=align_key,
            detail={"restored_values": old_values},
            now=now,
        )
        conn.commit()
        return {"apply_id": apply_id, "align_key": align_key, "ok": True}
    except RollbackCheckFailed as e:
        # 回滚失败：不得标成功，标脏待重对（脏标记不得提前清除）。
        apply_repo.set_record_status(
            conn, apply_id, status=STATUS_ROLLBACK_FAILED, dirty=True, now=now
        )
        apply_repo.insert_event(
            conn,
            event="rollback_failed",
            apply_id=apply_id,
            pair_id=pair_id,
            rollback_batch_id=batch_id,
            align_key=align_key,
            detail={"reason": str(e)},
            now=now,
        )
        conn.commit()
        return {"apply_id": apply_id, "align_key": align_key, "ok": False, "reason": str(e)}


def rollback_batch(apply_ids: list[int], note: str | None = None) -> dict[str, Any]:
    """Batch rollback, strategy=partial_success：逐键独立提交。

    已回滚键保持回滚结果；失败键留痕；批次按结果记 completed/partial/failed。
    """
    if not apply_ids:
        raise ValueError("apply_ids must not be empty")
    conn = connect()
    try:
        now = _now()
        seq = conn.execute("SELECT COUNT(*) c FROM rollback_batches").fetchone()["c"] + 1
        batch_no = f"BK-{now[:10].replace('-', '')}-{seq:04d}"
        batch_id = apply_repo.insert_rollback_batch(
            conn,
            batch_no=batch_no,
            strategy=STRATEGY_PARTIAL_SUCCESS,
            status="running",
            total=len(apply_ids),
            note=note,
            now=now,
        )
        conn.commit()

        results: list[dict[str, Any]] = []
        for apply_id in apply_ids:
            record = apply_repo.get_record(conn, apply_id)
            if record is None:
                apply_repo.insert_event(
                    conn,
                    event="rollback_rejected",
                    apply_id=apply_id,
                    rollback_batch_id=batch_id,
                    detail={"reason": "盖写记录不存在"},
                    now=_now(),
                )
                conn.commit()
                results.append({"apply_id": apply_id, "align_key": None, "ok": False,
                                "reason": "盖写记录不存在"})
                continue
            results.append(_rollback_one(conn, record, batch_id=batch_id, now=_now()))

        succeeded = sum(1 for r in results if r["ok"])
        failed = len(results) - succeeded
        status = "completed" if failed == 0 else ("partial" if succeeded > 0 else "failed")
        apply_repo.finish_rollback_batch(
            conn, batch_id, status=status, succeeded=succeeded, failed=failed, now=_now()
        )
        conn.commit()
        batch = apply_repo.get_rollback_batch(conn, batch_id)
        assert batch is not None
        return {"batch": batch, "results": results}
    finally:
        conn.close()


# --------------------------------------------------------------- 重对清脏


def clear_dirty_for_pair(conn, pair_id: int, *, batch_id: int, now: str) -> int:
    """重对（新对账批次完成）后清脏。这是脏标记唯一的清除通道。"""
    cleared = 0
    for record in apply_repo.list_dirty_records(conn, pair_id):
        apply_repo.set_record_dirty(conn, record["id"], dirty=False, now=now)
        apply_repo.insert_event(
            conn,
            event="dirty_cleared",
            apply_id=record["id"],
            pair_id=pair_id,
            align_key=record["align_key"],
            detail={"reconcile_batch_id": batch_id},
            now=now,
        )
        cleared += 1
    return cleared


# --------------------------------------------------------------------- 查询


def list_records(pair_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return apply_repo.list_records(conn, pair_id=pair_id, status=status)
    finally:
        conn.close()


def list_events(pair_id: int | None = None, limit: int = 300) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return apply_repo.list_events(conn, pair_id=pair_id, limit=limit)
    finally:
        conn.close()


def list_side_values(pair_id: int, side: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return apply_repo.list_side_values(conn, pair_id=pair_id, side=side)
    finally:
        conn.close()


def list_rollback_batches(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return apply_repo.list_rollback_batches(conn, limit=limit)
    finally:
        conn.close()


# ----------------------------------------------------------- 计数与一致性


def _verify_consistency(conn, pair_id: int | None) -> dict[str, Any]:
    """交叉核对：记录计数 == 留痕计数，且与侧数据当前值一致。"""
    records = apply_repo.list_records(conn, pair_id=pair_id, limit=100000)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    missing_old = [r["id"] for r in records if r["old_values"] is None]
    check(
        "盖写必有旧值留痕",
        not missing_old,
        "全部盖写记录均带旧值快照" if not missing_old else f"缺旧值留痕的记录: {missing_old}",
    )

    apply_events = apply_repo.count_events(conn, "apply", pair_id)
    check(
        "盖写数与留痕一致",
        apply_events == len(records),
        f"盖写记录 {len(records)} 笔，apply 留痕 {apply_events} 条",
    )

    rolled_back = [r for r in records if r["status"] == STATUS_ROLLED_BACK]
    rb_success_events = apply_repo.count_events(conn, "rollback_success", pair_id)
    check(
        "回滚成功数与留痕一致",
        rb_success_events == len(rolled_back),
        f"已回滚 {len(rolled_back)} 笔，rollback_success 留痕 {rb_success_events} 条",
    )

    rb_failed = [r for r in records if r["status"] == STATUS_ROLLBACK_FAILED]
    rb_failed_events = apply_repo.count_events(conn, "rollback_failed", pair_id)
    check(
        "回滚失败数与留痕一致",
        rb_failed_events == len(rb_failed),
        f"回滚失败 {len(rb_failed)} 笔，rollback_failed 留痕 {rb_failed_events} 条",
    )

    undirty_terminal = [
        r["id"]
        for r in records
        if r["status"] in (STATUS_ROLLED_BACK, STATUS_ROLLBACK_FAILED) and not r["dirty"]
    ]
    # 已被重对清脏的属正常：须有 dirty_cleared 留痕佐证
    uncleared: list[int] = []
    for rid in undirty_terminal:
        events = apply_repo.events_for_record(conn, rid)
        if not any(e["event"] == "dirty_cleared" for e in events):
            uncleared.append(rid)
    check(
        "脏标记不提前清除",
        not uncleared,
        "回滚过的记录均保持脏标记或经重对清脏"
        if not uncleared
        else f"回滚后无重对留痕却未标脏的记录: {uncleared}",
    )

    dirty_applied = [r["id"] for r in records if r["status"] == STATUS_APPLIED and r["dirty"]]
    check(
        "盖写中记录不误标脏",
        not dirty_applied,
        "盖写中记录均未标脏" if not dirty_applied else f"盖写中却标脏的记录: {dirty_applied}",
    )

    # 侧数据一致性：按留痕时间线重放（apply -> 盖写值，rollback_success -> 旧值），
    # 每个键的最终期望值必须等于侧数据当前值；rolling_back/rollback_failed 的键
    # 状态未知，跳过值断言（由脏标记兜底）。
    records_by_id = {r["id"]: r for r in records}
    expected: dict[tuple[int, str], dict[str, Any]] = {}
    events = apply_repo.list_events(conn, pair_id=pair_id, limit=100000)
    for e in sorted(events, key=lambda x: x["id"]):
        rec = records_by_id.get(e.get("apply_id") or -1)
        if rec is None or e["align_key"] is None:
            continue
        k = (rec["pair_id"], rec["key_hash"])
        if e["event"] == "apply":
            expected[k] = {"rec": rec, "values": rec["new_values"], "label": "盖写值"}
        elif e["event"] == "rollback_success":
            expected[k] = {"rec": rec, "values": rec["old_values"], "label": "回滚旧值"}
    mismatches: list[str] = []
    for (pid, key_hash), exp in expected.items():
        rec = exp["rec"]
        if rec["status"] in (STATUS_ROLLING_BACK, STATUS_ROLLBACK_FAILED):
            continue
        side_row = apply_repo.get_side_value(conn, pid, rec["target_side"], key_hash)
        if side_row is None:
            mismatches.append(f"记录 #{rec['id']} 侧数据缺失")
            continue
        diff_fields = [
            f for f in rec["fields"] if side_row["values"].get(f) != (exp["values"] or {}).get(f)
        ]
        if diff_fields:
            mismatches.append(f"记录 #{rec['id']} 侧数据≠{exp['label']}（{', '.join(diff_fields)}）")
    check(
        "侧数据与留痕一致",
        not mismatches,
        "各键侧数据当前值与留痕重放结果相符" if not mismatches else "；".join(mismatches),
    )

    return {"consistent": all(c["ok"] for c in checks), "checks": checks}


def apply_stats(pair_id: int | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        records = apply_repo.list_records(conn, pair_id=pair_id, limit=100000)
        by_status: dict[str, int] = {}
        for r in records:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        dirty = sum(1 for r in records if r["dirty"])
        batches = apply_repo.list_rollback_batches(conn, limit=100000)
        batch_by_status: dict[str, int] = {}
        for b in batches:
            batch_by_status[b["status"]] = batch_by_status.get(b["status"], 0) + 1
        return {
            "applies": len(records),
            "rolled_back": by_status.get(STATUS_ROLLED_BACK, 0),
            "rollback_failed": by_status.get(STATUS_ROLLBACK_FAILED, 0),
            "rolling_back": by_status.get(STATUS_ROLLING_BACK, 0),
            "applied": by_status.get(STATUS_APPLIED, 0),
            "dirty_keys": dirty,
            "rollback_rejected": apply_repo.count_events(conn, "rollback_rejected", pair_id),
            "apply_blocked": apply_repo.count_events(conn, "apply_blocked", pair_id),
            "rollback_batches": batch_by_status,
            "strategy": STRATEGY_PARTIAL_SUCCESS,
            "consistency": _verify_consistency(conn, pair_id),
        }
    finally:
        conn.close()
