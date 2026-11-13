"""Persistence for auditable apply (盖写) / rollback (回滚) side data.

Three stores that must always agree (the stats verifier cross-checks them):
- apply_records: one row per successful overwrite, with the pre-overwrite
  snapshot (old_values_json) — a record must never reach 'applied' without it.
- apply_events: append-only audit trail (留痕) of every attempt and transition.
- side_values: the mutable overlay ("侧数据") holding current effective rows.
"""

from __future__ import annotations

import json
from typing import Any

import sqlite3


def _record(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["align_key"] = json.loads(d.pop("align_key_json"))
    d["fields"] = json.loads(d.pop("fields_json"))
    d["old_values"] = json.loads(d.pop("old_values_json")) if d.get("old_values_json") else None
    d["new_values"] = json.loads(d.pop("new_values_json"))
    d["dirty"] = bool(d["dirty"])
    return d


def _event(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["align_key"] = json.loads(d.pop("align_key_json")) if d.get("align_key_json") else None
    d["detail"] = json.loads(d.pop("detail_json")) if d.get("detail_json") else None
    return d


def _side_value(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["align_key"] = json.loads(d.pop("align_key_json"))
    d["values"] = json.loads(d.pop("values_json"))
    return d


# ---------------------------------------------------------------- apply records

def insert_record(
    conn: sqlite3.Connection,
    *,
    pair_id: int,
    diff_id: int | None,
    align_key: dict[str, Any],
    key_hash: str,
    direction: str,
    target_side: str,
    fields: list[str],
    old_values: dict[str, Any],
    new_values: dict[str, Any],
    status: str,
    now: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO apply_records(
            pair_id, diff_id, align_key_json, key_hash, direction, target_side,
            fields_json, old_values_json, new_values_json, status, dirty,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)
        """,
        (
            pair_id,
            diff_id,
            json.dumps(align_key, ensure_ascii=False),
            key_hash,
            direction,
            target_side,
            json.dumps(fields, ensure_ascii=False),
            json.dumps(old_values, ensure_ascii=False),
            json.dumps(new_values, ensure_ascii=False),
            status,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def get_record(conn: sqlite3.Connection, apply_id: int) -> dict[str, Any] | None:
    return _record(
        conn.execute("SELECT * FROM apply_records WHERE id=?", (apply_id,)).fetchone()
    )


def list_records(
    conn: sqlite3.Connection,
    *,
    pair_id: int | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM apply_records WHERE 1=1"
    args: list[Any] = []
    if pair_id is not None:
        sql += " AND pair_id=?"
        args.append(pair_id)
    if status is not None:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [_record(r) for r in conn.execute(sql, args).fetchall()]  # type: ignore[misc]


def latest_record_for_key(
    conn: sqlite3.Connection, pair_id: int, key_hash: str
) -> dict[str, Any] | None:
    return _record(
        conn.execute(
            "SELECT * FROM apply_records WHERE pair_id=? AND key_hash=? ORDER BY id DESC LIMIT 1",
            (pair_id, key_hash),
        ).fetchone()
    )


def latest_applied_record_for_key(
    conn: sqlite3.Connection, pair_id: int, key_hash: str
) -> dict[str, Any] | None:
    return _record(
        conn.execute(
            """
            SELECT * FROM apply_records
            WHERE pair_id=? AND key_hash=? AND status='applied'
            ORDER BY id DESC LIMIT 1
            """,
            (pair_id, key_hash),
        ).fetchone()
    )


def rolling_back_record_for_key(
    conn: sqlite3.Connection, pair_id: int, key_hash: str
) -> dict[str, Any] | None:
    return _record(
        conn.execute(
            """
            SELECT * FROM apply_records
            WHERE pair_id=? AND key_hash=? AND status='rolling_back'
            ORDER BY id DESC LIMIT 1
            """,
            (pair_id, key_hash),
        ).fetchone()
    )


def set_record_status(
    conn: sqlite3.Connection,
    apply_id: int,
    *,
    status: str,
    dirty: bool | None = None,
    now: str,
) -> None:
    if dirty is None:
        conn.execute(
            "UPDATE apply_records SET status=?, updated_at=? WHERE id=?",
            (status, now, apply_id),
        )
    else:
        conn.execute(
            "UPDATE apply_records SET status=?, dirty=?, updated_at=? WHERE id=?",
            (status, 1 if dirty else 0, now, apply_id),
        )


def set_record_dirty(conn: sqlite3.Connection, apply_id: int, *, dirty: bool, now: str) -> None:
    conn.execute(
        "UPDATE apply_records SET dirty=?, updated_at=? WHERE id=?",
        (1 if dirty else 0, now, apply_id),
    )


def list_dirty_records(conn: sqlite3.Connection, pair_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM apply_records WHERE pair_id=? AND dirty=1 ORDER BY id", (pair_id,)
    ).fetchall()
    return [_record(r) for r in rows]  # type: ignore[misc]


# ------------------------------------------------------------------ audit events

def insert_event(
    conn: sqlite3.Connection,
    *,
    event: str,
    apply_id: int | None = None,
    pair_id: int | None = None,
    rollback_batch_id: int | None = None,
    align_key: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    now: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO apply_events(
            apply_id, pair_id, rollback_batch_id, align_key_json, event, detail_json, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            apply_id,
            pair_id,
            rollback_batch_id,
            json.dumps(align_key, ensure_ascii=False) if align_key is not None else None,
            event,
            json.dumps(detail, ensure_ascii=False) if detail is not None else None,
            now,
        ),
    )
    return int(cur.lastrowid)


def list_events(
    conn: sqlite3.Connection,
    *,
    pair_id: int | None = None,
    apply_id: int | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM apply_events WHERE 1=1"
    args: list[Any] = []
    if pair_id is not None:
        sql += " AND pair_id=?"
        args.append(pair_id)
    if apply_id is not None:
        sql += " AND apply_id=?"
        args.append(apply_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [_event(r) for r in conn.execute(sql, args).fetchall()]


def count_events(conn: sqlite3.Connection, event: str, pair_id: int | None = None) -> int:
    sql = "SELECT COUNT(*) c FROM apply_events WHERE event=?"
    args: list[Any] = [event]
    if pair_id is not None:
        sql += " AND pair_id=?"
        args.append(pair_id)
    return int(conn.execute(sql, args).fetchone()["c"])


def events_for_record(conn: sqlite3.Connection, apply_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM apply_events WHERE apply_id=? ORDER BY id", (apply_id,)
    ).fetchall()
    return [_event(r) for r in rows]


# ------------------------------------------------------------------- side values

def get_side_value(
    conn: sqlite3.Connection, pair_id: int, side: str, key_hash: str
) -> dict[str, Any] | None:
    return _side_value(
        conn.execute(
            "SELECT * FROM side_values WHERE pair_id=? AND side=? AND key_hash=?",
            (pair_id, side, key_hash),
        ).fetchone()
    )


def upsert_side_value(
    conn: sqlite3.Connection,
    *,
    pair_id: int,
    side: str,
    key_hash: str,
    align_key: dict[str, Any],
    values: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO side_values(pair_id, side, key_hash, align_key_json, values_json, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(pair_id, side, key_hash)
        DO UPDATE SET values_json=excluded.values_json, updated_at=excluded.updated_at
        """,
        (
            pair_id,
            side,
            key_hash,
            json.dumps(align_key, ensure_ascii=False),
            json.dumps(values, ensure_ascii=False),
            now,
        ),
    )


def list_side_values(
    conn: sqlite3.Connection, *, pair_id: int, side: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM side_values WHERE pair_id=?"
    args: list[Any] = [pair_id]
    if side is not None:
        sql += " AND side=?"
        args.append(side)
    sql += " ORDER BY side, key_hash"
    return [_side_value(r) for r in conn.execute(sql, args).fetchall()]  # type: ignore[misc]


# -------------------------------------------------------------- rollback batches

def insert_rollback_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    strategy: str,
    status: str,
    total: int,
    note: str | None,
    now: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO rollback_batches(batch_no, strategy, status, total, succeeded, failed, note, created_at)
        VALUES (?,?,?,?,0,0,?,?)
        """,
        (batch_no, strategy, status, total, note, now),
    )
    return int(cur.lastrowid)


def finish_rollback_batch(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    status: str,
    succeeded: int,
    failed: int,
    now: str,
) -> None:
    conn.execute(
        """
        UPDATE rollback_batches
        SET status=?, succeeded=?, failed=?, finished_at=?
        WHERE id=?
        """,
        (status, succeeded, failed, now, batch_id),
    )


def get_rollback_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM rollback_batches WHERE id=?", (batch_id,)).fetchone()
    return dict(r) if r else None


def list_rollback_batches(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM rollback_batches ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
