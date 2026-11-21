import json
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import occupancy_write_gate as write_gate
from app.modules import released_occ_append_reject as released_reject

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cabinets (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  site TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cells (
  id TEXT PRIMARY KEY,
  cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
  code TEXT NOT NULL,
  size_code TEXT NOT NULL DEFAULT 'M',
  status TEXT NOT NULL DEFAULT 'free',
  UNIQUE(cabinet_id, code)
);

CREATE TABLE IF NOT EXISTS parcel_events (
  id TEXT PRIMARY KEY,
  cabinet_id TEXT NOT NULL,
  cell_id TEXT,
  event_type TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  tracking_no TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  channel TEXT NOT NULL DEFAULT 'primary'
);

CREATE TABLE IF NOT EXISTS occupancies (
  id TEXT PRIMARY KEY,
  cell_id TEXT NOT NULL REFERENCES cells(id),
  tracking_no TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  status TEXT NOT NULL DEFAULT 'occupied',
  last_event_id TEXT
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  event_id TEXT,
  occupancy_id TEXT
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS occupancy_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occupancy_id TEXT NOT NULL,
  cell_id TEXT NOT NULL,
  tracking_no TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  event_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 占用可写闸：独立表承载「能否向该占用追加事件」的角标。
-- 不往 occupancies 行加列，确保已释放占用行保持不可变。
CREATE TABLE IF NOT EXISTS occupancy_write_gate (
  occupancy_id TEXT PRIMARY KEY,
  writable INTEGER NOT NULL DEFAULT 1 CHECK (writable IN (0, 1)),
  gate_rev INTEGER NOT NULL DEFAULT 0,
  released_at TEXT,
  released_by_event_id TEXT,
  release_reason TEXT,
  updated_at TEXT NOT NULL
);

-- 卸标审计：每次真正把写闸从开到关都落一行。
CREATE TABLE IF NOT EXISTS occupancy_write_gate_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occupancy_id TEXT NOT NULL,
  event_id TEXT,
  action TEXT NOT NULL,
  gate_rev INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

-- 已释放占用是不可变历史账：任何字段的 UPDATE 一律 ABORT。
-- 追加事件只能进搁置表，禁止把 released 改回 occupied 来“接住”事件。
CREATE TRIGGER IF NOT EXISTS trg_occupancies_released_immutable
BEFORE UPDATE ON occupancies
WHEN OLD.status IN ('released', 'closed')
BEGIN
  SELECT RAISE(ABORT, 'released occupancy is immutable');
END;
"""

# 旧库补列：CREATE TABLE IF NOT EXISTS 不会给既有表加列。
_MIGRATIONS = (
    ("quarantine", "event_id", "TEXT"),
    ("quarantine", "occupancy_id", "TEXT"),
)

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_quarantine_occ
  ON quarantine(reason, occupancy_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_event
  ON quarantine(reason, event_id);
CREATE INDEX IF NOT EXISTS idx_write_gate_audit_occ
  ON occupancy_write_gate_audit(occupancy_id, action);
CREATE INDEX IF NOT EXISTS idx_write_gate_audit_event
  ON occupancy_write_gate_audit(occupancy_id, event_id);
"""


def overdue_hours(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='overdue_hours'").fetchone()
    return int(row["value"]) if row else 48


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(_INDEXES)
    if not conn.execute("SELECT 1 FROM settings WHERE key='overdue_hours'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('overdue_hours', '48')")
    if conn.execute("SELECT COUNT(*) AS c FROM cabinets").fetchone()["c"] == 0:
        _seed(conn)
    backfill_write_gates(conn)
    conn.commit()
    conn.close()


def backfill_write_gates(conn):
    """旧库补闸：让写闸角标与占用状态一致，且历史拒收都有卸标审计。

    1. 开放占用缺闸行 → 建 ``writable=1`` 闸行（不动占用行）。
    2. 已 released/closed 占用缺闸行 → 建 ``writable=0`` 闸行并补一条
       ``backfill_release`` 审计（``released_at`` 取 ``closed_at``）。
    3. 历史 ``append_rejected`` 搁置缺对应 event 的卸标审计 → 补审计行。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT o.id, o.status, o.closed_at, o.last_event_id FROM occupancies o "
        "LEFT JOIN occupancy_write_gate w ON w.occupancy_id = o.id "
        "WHERE w.occupancy_id IS NULL"
    ).fetchall()
    for row in rows:
        released = row["status"] in write_gate.RELEASED_STATUSES
        conn.execute(
            "INSERT OR IGNORE INTO occupancy_write_gate"
            "(occupancy_id, writable, gate_rev, released_at, released_by_event_id, "
            " release_reason, updated_at) VALUES (?,?,?,?,?,?,?)",
            (
                row["id"],
                0 if released else 1,
                1 if released else 0,
                row["closed_at"] if released else None,
                row["last_event_id"] if released else None,
                "backfill_released" if released else None,
                now,
            ),
        )
        if released:
            conn.execute(
                "INSERT INTO occupancy_write_gate_audit"
                "(occupancy_id, event_id, action, gate_rev, created_at, detail_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    row["id"],
                    row["last_event_id"],
                    "backfill_release",
                    1,
                    row["closed_at"] or now,
                    json.dumps({"backfill": True}, ensure_ascii=False),
                ),
            )

    # 历史拒收补审计：只补那些确实没有同 event 卸标审计的拒收行。
    rejects = conn.execute(
        "SELECT q.occupancy_id, q.event_id, q.created_at FROM quarantine q "
        "WHERE q.reason='append_rejected' AND q.occupancy_id IS NOT NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM occupancy_write_gate_audit a "
        "  WHERE a.occupancy_id=q.occupancy_id AND a.event_id IS q.event_id "
        "    AND a.action='reject_release'"
        ")"
    ).fetchall()
    for row in rejects:
        gate = conn.execute(
            "SELECT gate_rev FROM occupancy_write_gate WHERE occupancy_id=?",
            (row["occupancy_id"],),
        ).fetchone()
        conn.execute(
            "INSERT INTO occupancy_write_gate_audit"
            "(occupancy_id, event_id, action, gate_rev, created_at, detail_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                row["occupancy_id"],
                row["event_id"],
                "reject_release",
                int(gate["gate_rev"]) if gate else 0,
                row["created_at"] or now,
                json.dumps({"backfill": True}, ensure_ascii=False),
            ),
        )


def _migrate(conn):
    for table, column, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _seed(conn):
    path = FIXTURES_DIR / "seed_bay.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for c in data.get("cabinets", []):
        conn.execute(
            "INSERT INTO cabinets(id, label, site) VALUES (?,?,?)",
            (c["id"], c["label"], c["site"]),
        )
    for cell in data.get("cells", []):
        conn.execute(
            "INSERT INTO cells(id, cabinet_id, code, size_code, status) VALUES (?,?,?,?,?)",
            (cell["id"], cell["cabinet_id"], cell["code"], cell.get("size_code", "M"), "free"),
        )
    for ev in sorted(data.get("events", []), key=lambda e: (e["event_ts"], e["id"])):
        rejection = released_reject.classify(conn, ev)
        if rejection is not None:
            released_reject.record_reject(conn, rejection)
            continue
        conn.execute(
            "INSERT INTO parcel_events(id, cabinet_id, cell_id, event_type, event_ts, tracking_no, payload_json, channel) VALUES (?,?,?,?,?,?,?,?)",
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
        if ev["event_type"] == "drop" and ev.get("cell_id"):
            oid = f"OCC-{ev['cell_id']}-{ev['id']}"
            conn.execute(
                "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status, last_event_id) VALUES (?,?,?,?,?,?,?)",
                (oid, ev["cell_id"], ev["tracking_no"], ev["event_ts"], None, "occupied", ev["id"]),
            )
            conn.execute("UPDATE cells SET status='occupied' WHERE id=?", (ev["cell_id"],))
            write_gate.ensure_gate(conn, oid, at=ev["event_ts"])
            conn.execute(
                "INSERT INTO occupancy_proofs(occupancy_id, cell_id, tracking_no, opened_at, closed_at, event_ids_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    oid,
                    ev["cell_id"],
                    ev["tracking_no"],
                    ev["event_ts"],
                    None,
                    json.dumps([ev["id"]], ensure_ascii=False),
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
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
                write_gate.release(
                    conn,
                    row["id"],
                    event_id=ev["id"],
                    action=write_gate.ACTION_PICKUP,
                    at=ev["event_ts"],
                    reason="pickup_released",
                )
