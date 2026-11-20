import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.db import FIXTURES_DIR, connect
from app.modules import released_occ_append_reject as released_reject


def run_batch(fixture: str = "ingest_batch.json"):
    # 夹具名只允许纯文件名，避免路径穿越。
    name = Path(fixture).name or "ingest_batch.json"
    path = FIXTURES_DIR / name
    if not path.exists():
        return {"batch_id": None, "accepted": 0, "message": "无夹具"}
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events", [])
    batch_id = f"B-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect()
    try:
        accepted, rejected, duplicates = ingest_events(conn, events)
        conn.execute(
            "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (
                batch_id,
                now,
                json.dumps(
                    {
                        "accepted": accepted,
                        "rejected": rejected,
                        "duplicates": duplicates,
                        "total": len(events),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
        return {
            "batch_id": batch_id,
            "accepted": accepted,
            "rejected": rejected,
            "duplicates": duplicates,
            "total": len(events),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ingest_events(conn, events):
    """逐条装载；显式指向已释放占用的事件入口分流进搁置。

    调用方负责 commit / rollback，便于整趟失败闭。
    返回 (accepted, rejected, duplicates)。
    """
    accepted = rejected = duplicates = 0
    for ev in events:
        rejection = released_reject.classify(conn, ev)
        if rejection is not None:
            if released_reject.record_reject(conn, rejection):
                rejected += 1
            else:
                duplicates += 1
            continue
        if apply_event(conn, ev):
            accepted += 1
        else:
            duplicates += 1
    return accepted, rejected, duplicates


def apply_event(conn, ev) -> bool:
    """把单条事件写入主通道并物化占用；返回是否为新事件。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO parcel_events(id, cabinet_id, cell_id, event_type, event_ts, tracking_no, payload_json, channel) VALUES (?,?,?,?,?,?,?,?)",
        (
            ev["id"],
            ev["cabinet_id"],
            ev.get("cell_id"),
            ev["event_type"],
            ev["event_ts"],
            ev.get("tracking_no"),
            json.dumps(ev.get("payload", {}), ensure_ascii=False),
            "primary",
        ),
    )
    if cur.rowcount == 0:
        return False
    if ev["event_type"] == "drop" and ev.get("cell_id"):
        cell = conn.execute("SELECT status FROM cells WHERE id=?", (ev["cell_id"],)).fetchone()
        if cell and cell["status"] == "free":
            oid = f"OCC-{ev['cell_id']}-{ev['id']}"
            conn.execute(
                "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status, last_event_id) VALUES (?,?,?,?,?,?,?)",
                (oid, ev["cell_id"], ev["tracking_no"], ev["event_ts"], None, "occupied", ev["id"]),
            )
            conn.execute("UPDATE cells SET status='occupied' WHERE id=?", (ev["cell_id"],))
    elif ev["event_type"] == "pickup" and ev.get("cell_id"):
        row = conn.execute(
            "SELECT id FROM occupancies WHERE cell_id=? AND status='occupied' ORDER BY opened_at DESC LIMIT 1",
            (ev["cell_id"],),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE occupancies SET closed_at=?, status='released', last_event_id=? WHERE id=?",
                (ev["event_ts"], ev["id"], row["id"]),
            )
            conn.execute("UPDATE cells SET status='free' WHERE id=?", (ev["cell_id"],))
    return True


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "occupancy_id" in out and "occ_id" not in out:
        out["occ_id"] = out.get("occupancy_id")
    if "cell_id" in out and "locker_cell" not in out:
        out["locker_cell"] = out.get("cell_id")
    if "tracking_no" in out and "parcel_key" not in out:
        out["parcel_key"] = out.get("tracking_no")
    if "verified" not in out:
        out["verified"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True}
    if extra:
        data.update(extra)
    return data

