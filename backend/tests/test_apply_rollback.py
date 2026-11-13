"""Apply/rollback (盖写/回滚) audit rules — see services/apply_service.py docstring."""

from __future__ import annotations

import os
import tempfile

# DATA_DIR must be set before any app module is imported (config reads it at import).
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="syncledger-test-"))

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.db import connect  # noqa: E402
from app.repositories import applies as apply_repo  # noqa: E402
from app.seed import init_db  # noqa: E402
from app.services import apply_service, reconcile_service  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    init_db()
    yield


def _pair_id(code: str) -> int:
    return next(p["id"] for p in reconcile_service.list_pairs() if p["code"] == code)


def _run_and_diffs(pair_code: str):
    pair_id = _pair_id(pair_code)
    batch = reconcile_service.run_reconcile(pair_id)
    diffs = reconcile_service.list_diffs(batch_id=batch["id"])
    return pair_id, batch, diffs


def _diff_by_key(diffs, **key):
    for d in diffs:
        if all(d["align_key"].get(k) == v for k, v in key.items()):
            return d
    raise AssertionError(f"diff not found for key {key}")


# ------------------------------------------------------------------ 盖写


def test_apply_without_old_value_is_rejected_and_traced(fresh_db):
    """单侧差异目标端无旧值 -> 禁止盖写成功，留 apply_blocked 痕。"""
    pair_id, _, diffs = _run_and_diffs("payment_vs_ledger")
    left_only = _diff_by_key(diffs, txn_id="TXN-003")  # 仅左有，右端无旧值
    assert left_only["kind"] == "left_only"

    with pytest.raises(ValueError, match="无旧值"):
        apply_service.apply_diff(left_only["id"], "left_to_right")

    assert apply_service.list_records(pair_id=pair_id) == []
    stats = apply_service.apply_stats(pair_id=pair_id)
    assert stats["applies"] == 0
    assert stats["apply_blocked"] == 1
    assert stats["consistency"]["consistent"]


def test_apply_success_snapshots_old_value_and_side_data(fresh_db):
    pair_id, _, diffs = _run_and_diffs("payment_vs_ledger")
    diff = _diff_by_key(diffs, txn_id="TXN-002")  # amount 50.50 vs 50.00

    rec = apply_service.apply_diff(diff["id"], "left_to_right")
    assert rec["status"] == "applied"
    assert rec["old_values"]["amount"] == 50.00  # 旧值留痕
    assert rec["new_values"]["amount"] == 50.50
    assert rec["dirty"] is False

    side = apply_service.list_side_values(pair_id=pair_id, side="right")
    assert len(side) == 1 and side[0]["values"]["amount"] == 50.50

    stats = apply_service.apply_stats(pair_id=pair_id)
    assert stats["applies"] == 1 and stats["dirty_keys"] == 0
    assert stats["consistency"]["consistent"]


def test_apply_blocked_while_rollback_in_progress(fresh_db):
    """回滚进行中同一键禁止再次盖写；其他键不受影响。"""
    pair_id, _, diffs = _run_and_diffs("inventory_hq_vs_store")
    diff = _diff_by_key(diffs, sku_id="SKU-100", store_id="S01")
    rec = apply_service.apply_diff(diff["id"], "left_to_right")

    # 模拟回滚做到一半（崩溃现场）：记录停在 rolling_back
    conn = connect()
    try:
        apply_repo.set_record_status(conn, rec["id"], status="rolling_back", now="2026-11-13T00:00:00Z")
        conn.commit()
    finally:
        conn.close()

    again = _diff_by_key(diffs, sku_id="SKU-100", store_id="S01")
    with pytest.raises(ValueError, match="回滚进行中"):
        apply_service.apply_diff(again["id"], "left_to_right")
    stats = apply_service.apply_stats(pair_id=pair_id)
    assert stats["applies"] == 1  # 没有第二笔记账
    assert stats["apply_blocked"] == 1


# ------------------------------------------------------------------ 回滚


def test_rollback_success_restores_old_value_and_marks_dirty(fresh_db):
    pair_id, _, diffs = _run_and_diffs("payment_vs_ledger")
    rec = apply_service.apply_diff(_diff_by_key(diffs, txn_id="TXN-002")["id"], "left_to_right")

    out = apply_service.rollback_batch([rec["id"]])
    assert out["batch"]["status"] == "completed"
    assert out["batch"]["succeeded"] == 1

    after = apply_service.list_records(pair_id=pair_id)[0]
    assert after["status"] == "rolled_back"
    assert after["dirty"] is True  # 标脏待重对
    side = apply_service.list_side_values(pair_id=pair_id, side="right")
    assert side[0]["values"]["amount"] == 50.00  # 回到旧值

    stats = apply_service.apply_stats(pair_id=pair_id)
    assert stats["rolled_back"] == 1 and stats["dirty_keys"] == 1
    assert stats["consistency"]["consistent"]


def test_rollback_failure_not_success_and_dirty_kept(fresh_db):
    """侧数据被漂改 -> 回滚失败：不得标成功，脏标记不得提前清除。"""
    pair_id, _, diffs = _run_and_diffs("payment_vs_ledger")
    rec = apply_service.apply_diff(_diff_by_key(diffs, txn_id="TXN-002")["id"], "left_to_right")

    # 外部漂改侧数据（盖写值 50.50 被改成 999）
    conn = connect()
    try:
        row = apply_repo.get_side_value(conn, pair_id, "right", rec["key_hash"])
        values = dict(row["values"])
        values["amount"] = 999.0
        apply_repo.upsert_side_value(
            conn, pair_id=pair_id, side="right", key_hash=rec["key_hash"],
            align_key=rec["align_key"], values=values, now="2026-11-13T00:00:00Z",
        )
        conn.commit()
    finally:
        conn.close()

    out = apply_service.rollback_batch([rec["id"]])
    assert out["batch"]["status"] == "failed"
    assert out["batch"]["succeeded"] == 0 and out["batch"]["failed"] == 1
    assert "漂移" in out["results"][0]["reason"]

    after = apply_service.list_records(pair_id=pair_id)[0]
    assert after["status"] == "rollback_failed"  # 不得标成功
    assert after["dirty"] is True  # 脏标记保留
    side = apply_service.list_side_values(pair_id=pair_id, side="right")
    assert side[0]["values"]["amount"] == 999.0  # 未被假装回滚

    stats = apply_service.apply_stats(pair_id=pair_id)
    assert stats["rollback_failed"] == 1 and stats["rolled_back"] == 0
    assert stats["dirty_keys"] == 1
    assert stats["consistency"]["consistent"]


def test_batch_rollback_partial_success_strategy(fresh_db):
    """策略=部分成功：已回滚键保持结果、失败键留痕、批次记 partial。"""
    pair_id, _, diffs = _run_and_diffs("payment_vs_ledger")
    rec = apply_service.apply_diff(_diff_by_key(diffs, txn_id="TXN-002")["id"], "left_to_right")
    first = apply_service.rollback_batch([rec["id"]])
    assert first["batch"]["status"] == "completed"

    # 第二批复一好一坏：坏键（已回滚过的记录）留痕，好键（新盖写）正常回滚
    rec2 = apply_service.apply_diff(_diff_by_key(diffs, txn_id="TXN-002")["id"], "left_to_right")
    out = apply_service.rollback_batch([rec2["id"], rec["id"]])
    assert out["batch"]["strategy"] == "partial_success"
    assert out["batch"]["status"] == "partial"
    assert out["batch"]["succeeded"] == 1 and out["batch"]["failed"] == 1

    by_id = {r["apply_id"]: r for r in out["results"]}
    assert by_id[rec2["id"]]["ok"] is True  # 已回滚键保持回滚结果
    assert by_id[rec["id"]]["ok"] is False and "非盖写中" in by_id[rec["id"]]["reason"]

    side = apply_service.list_side_values(pair_id=pair_id, side="right")
    assert side[0]["values"]["amount"] == 50.00  # 好键回滚结果保持

    stats = apply_service.apply_stats(pair_id=pair_id)
    assert stats["rolled_back"] == 2 and stats["rollback_rejected"] == 1
    assert stats["consistency"]["consistent"]


def test_rollback_rejected_when_not_latest(fresh_db):
    """同一键盖写两次后，旧一笔须先滚新一笔，否则拒绝并留痕。"""
    pair_id, _, diffs = _run_and_diffs("payment_vs_ledger")
    diff_id = _diff_by_key(diffs, txn_id="TXN-002")["id"]
    first = apply_service.apply_diff(diff_id, "left_to_right")
    second = apply_service.apply_diff(diff_id, "left_to_right")
    assert second["old_values"]["amount"] == 50.50  # 第二笔快照到第一笔的盖写值

    out = apply_service.rollback_batch([first["id"]])
    assert out["batch"]["status"] == "failed"
    assert "更新的盖写" in out["results"][0]["reason"]

    kept = {r["id"]: r for r in apply_service.list_records(pair_id=pair_id)}
    assert kept[first["id"]]["status"] == "applied"  # 拒绝不改动原记录
    assert kept[second["id"]]["status"] == "applied"

    # LIFO：先滚第二笔，再滚第一笔，最终回到旧值
    apply_service.rollback_batch([second["id"]])
    apply_service.rollback_batch([first["id"]])
    side = apply_service.list_side_values(pair_id=pair_id, side="right")
    assert side[0]["values"]["amount"] == 50.00
    assert apply_service.apply_stats(pair_id=pair_id)["consistency"]["consistent"]


# --------------------------------------------------------------- 重对清脏


def test_dirty_cleared_only_by_reconcile(fresh_db):
    pair_id, _, diffs = _run_and_diffs("payment_vs_ledger")
    rec = apply_service.apply_diff(_diff_by_key(diffs, txn_id="TXN-002")["id"], "left_to_right")
    apply_service.rollback_batch([rec["id"]])
    assert apply_service.apply_stats(pair_id=pair_id)["dirty_keys"] == 1

    # 重对：新对账批次完成 -> 清脏并留痕；回滚后的旧值使差异重现
    batch = reconcile_service.run_reconcile(pair_id)
    assert batch["dirty_cleared"] == 1
    stats = apply_service.apply_stats(pair_id=pair_id)
    assert stats["dirty_keys"] == 0
    assert stats["consistency"]["consistent"]
    events = apply_service.list_events(pair_id=pair_id)
    assert any(e["event"] == "dirty_cleared" for e in events)
    new_diffs = reconcile_service.list_diffs(batch_id=batch["id"])
    assert _diff_by_key(new_diffs, txn_id="TXN-002")["kind"] == "field_mismatch"


def test_reconcile_reads_overlay_after_apply(fresh_db):
    pair_id, batch, diffs = _run_and_diffs("payment_vs_ledger")
    apply_service.apply_diff(_diff_by_key(diffs, txn_id="TXN-002")["id"], "left_to_right")
    batch2 = reconcile_service.run_reconcile(pair_id)
    assert batch2["field_mismatch"] == batch["field_mismatch"] - 1
    assert apply_service.apply_stats(pair_id=pair_id)["consistency"]["consistent"]


def test_apply_again_after_rollback(fresh_db):
    pair_id, _, diffs = _run_and_diffs("payment_vs_ledger")
    diff_id = _diff_by_key(diffs, txn_id="TXN-002")["id"]
    rec = apply_service.apply_diff(diff_id, "left_to_right")
    apply_service.rollback_batch([rec["id"]])
    rec2 = apply_service.apply_diff(diff_id, "left_to_right")
    assert rec2["status"] == "applied"
    assert rec2["old_values"]["amount"] == 50.00  # 快照到回滚后的旧值
    assert apply_service.apply_stats(pair_id=pair_id)["consistency"]["consistent"]


# ------------------------------------------------------------------ API


def test_api_flow(fresh_db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        pair_id = _pair_id("payment_vs_ledger")
        batch = client.post("/api/reconcile", json={"pair_id": pair_id}).json()
        diffs = client.get(f"/api/diffs?batch_id={batch['id']}").json()
        diff = _diff_by_key(diffs, txn_id="TXN-002")

        r = client.post("/api/apply", json={"diff_id": diff["id"], "direction": "left_to_right"})
        assert r.status_code == 200, r.text
        apply_id = r.json()["id"]

        r = client.post("/api/apply/rollback", json={"apply_ids": [apply_id]})
        assert r.status_code == 200, r.text
        assert r.json()["batch"]["status"] == "completed"

        stats = client.get("/api/apply/stats").json()
        assert stats["applies"] == 1 and stats["rolled_back"] == 1
        assert stats["dirty_keys"] == 1
        assert stats["consistency"]["consistent"]

        # 无旧值留痕 -> 盖写被拒（400），不记成功
        left_only = _diff_by_key(diffs, txn_id="TXN-003")
        r = client.post("/api/apply", json={"diff_id": left_only["id"], "direction": "left_to_right"})
        assert r.status_code == 400
        stats = client.get("/api/apply/stats").json()
        assert stats["applies"] == 1
