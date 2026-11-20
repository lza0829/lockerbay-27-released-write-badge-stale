"""已释放占用拒收追加：released 占用是不可变历史账。

定死的两处选择：

1. 搁置落库形态：``quarantine`` 表（``reason=append_rejected``），
   ``member_json`` 保留原始事件与原始载荷，并冗余落 ``event_id`` /
   ``occupancy_id`` 便于只读核证。事件本身不写入 ``parcel_events``，
   避免主通道出现一条“看似已受理”的追加事件。
2. 失败闭路径：入口识别分流。装载在物化之前调用 :func:`classify`；
   目标占用已 released（或明确已关闭）即整条事件转入搁置，绝不进入
   占用物化分支，也绝不把 released 改回 occupied 来“接住”事件。
   SQLite 触发器另做兜底：released 行的任何 UPDATE 直接 ABORT。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

STATUS = "implemented"

REJECT_REASON = "append_rejected"
RELEASED_STATUSES = frozenset({"released", "closed"})

_OCCUPANCY_ID_KEYS = (
    "occupancy_id",
    "occ_id",
    "released_occupancy_id",
    "closed_occupancy_id",
    "target_occupancy_id",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def occupancy_id_of(event: dict) -> str | None:
    """从事件或事件载荷中提取显式指向的占用 id。

    支持 ``occupancy_id`` 及常见别名；也接受 ``payload.occupancy.id``。
    没有显式指向时返回 ``None``，不做猜测，避免误伤正常投取事件。
    """
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    for source in (event, payload):
        for key in _OCCUPANCY_ID_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    nested = payload.get("occupancy")
    if isinstance(nested, dict):
        value = nested.get("id") or nested.get("occupancy_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def classify(conn, event: dict) -> dict | None:
    """入口判别：事件明确指向已 released/closed 占用时返回拒收凭据。

    两条指向路径：

    1. 显式 ``occupancy_id``（含载荷别名）；
    2. 无 id，但事件是取件且该格口当前无开放占用、运单恰好落在一条已释放
       占用上 —— 即“对已关闭占用的重复取件”，属于明确指向。

    返回 ``None`` 表示放行。本函数只读，不产生任何写入。
    """
    occupancy_id = occupancy_id_of(event)
    if occupancy_id:
        row = conn.execute(
            "SELECT * FROM occupancies WHERE id=?", (occupancy_id,)
        ).fetchone()
        if row is not None and row["status"] in RELEASED_STATUSES:
            return _rejection(row, event)
        return None

    # 仅在无显式 id 时兜底；drop 不推断，避免误伤格口复用后的正常投柜。
    if event.get("event_type") != "pickup":
        return None
    cell_id = event.get("cell_id")
    tracking_no = event.get("tracking_no")
    if not cell_id or not tracking_no:
        return None
    open_row = conn.execute(
        "SELECT 1 FROM occupancies WHERE cell_id=? AND status NOT IN ('released','closed')",
        (cell_id,),
    ).fetchone()
    if open_row is not None:
        return None
    released = conn.execute(
        "SELECT * FROM occupancies WHERE cell_id=? AND tracking_no=? "
        "AND status IN ('released','closed') ORDER BY opened_at DESC LIMIT 1",
        (cell_id, tracking_no),
    ).fetchone()
    if released is None:
        return None
    return _rejection(released, event)


def _rejection(row, event: dict) -> dict:
    return {
        "occupancy_id": row["id"],
        "occupancy": dict(row),
        "event": event,
        "detected_at": utc_now(),
    }


def _safe_member(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dedupe_key(occupancy_id: str, event: dict) -> str:
    """稳定去重键：同一占用 + 同一条原始事件（忽略检测时刻）。"""
    return json.dumps(
        {
            "occupancy_id": occupancy_id,
            "event_id": event.get("id"),
            "event": event,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _reject_exists(conn, occupancy_id: str, event_id: str | None, dedupe_key: str) -> bool:
    if event_id:
        row = conn.execute(
            "SELECT 1 FROM quarantine WHERE reason=? AND occupancy_id=? AND event_id=?",
            (REJECT_REASON, occupancy_id, event_id),
        ).fetchone()
        return row is not None
    # 无事件 id 时按稳定键比对（解析 member_json，避免 LIKE 通配符歧义）。
    rows = conn.execute(
        "SELECT member_json FROM quarantine WHERE reason=? AND occupancy_id=? AND event_id IS NULL",
        (REJECT_REASON, occupancy_id),
    ).fetchall()
    return any(_safe_member(r["member_json"]).get("dedupe_key") == dedupe_key for r in rows)


def record_reject(conn, rejection: dict) -> bool:
    """把拒收事件落入搁置表；同一事件重复装载只保留一条。"""
    event = rejection["event"]
    occupancy_id = rejection["occupancy_id"]
    occupancy = rejection["occupancy"]
    event_id = event.get("id")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    dedupe_key = _dedupe_key(occupancy_id, event)
    member = {
        "reason": REJECT_REASON,
        "occupancy_id": occupancy_id,
        "occupancy_status": occupancy["status"],
        "event_id": event_id,
        "event": event,
        "payload": payload,
        "dedupe_key": dedupe_key,
        "detected_at": rejection["detected_at"],
    }
    member_json = json.dumps(member, ensure_ascii=False)
    if _reject_exists(conn, occupancy_id, event_id, dedupe_key):
        return False
    conn.execute(
        "INSERT INTO quarantine(reason, member_json, created_at, event_id, occupancy_id) "
        "VALUES (?,?,?,?,?)",
        (REJECT_REASON, member_json, rejection["detected_at"], event_id, occupancy_id),
    )
    return True


def list_rejects(conn, occupancy_id: str | None = None) -> list[dict]:
    """只读：列出 append_rejected 搁置记录（可按占用过滤）。"""
    sql = (
        "SELECT id, reason, member_json, created_at, event_id, occupancy_id "
        "FROM quarantine WHERE reason=?"
    )
    params: list[str] = [REJECT_REASON]
    if occupancy_id:
        sql += " AND occupancy_id=?"
        params.append(occupancy_id)
    sql += " ORDER BY id"
    out = []
    for row in conn.execute(sql, params).fetchall():
        member = _safe_member(row["member_json"])
        out.append(
            {
                "id": row["id"],
                "reason": row["reason"],
                "occupancy_id": row["occupancy_id"],
                "event_id": row["event_id"],
                "created_at": row["created_at"],
                "occupancy_status": member.get("occupancy_status"),
                "event": member.get("event"),
                "payload": member.get("payload", {}),
                "member_json": row["member_json"],
            }
        )
    return out


def proof(conn, occupancy_id: str) -> dict:
    """只读：某占用的拒收核证。

    同时给出占用历史行、既有物化核证、拒收记录，证明：
    released 行未被后续追加事件改写，追加事件仅存在于搁置表。
    """
    occupancy = conn.execute(
        "SELECT * FROM occupancies WHERE id=?", (occupancy_id,)
    ).fetchone()
    proofs = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM occupancy_proofs WHERE occupancy_id=? ORDER BY id",
            (occupancy_id,),
        ).fetchall()
    ]
    rejections = list_rejects(conn, occupancy_id)
    return {
        "occupancy_id": occupancy_id,
        "occupancy": dict(occupancy) if occupancy else None,
        "immutable": bool(
            occupancy and occupancy["status"] in RELEASED_STATUSES
        ),
        "proofs": proofs,
        "rejections": rejections,
        "rejected_count": len(rejections),
        "read_only": True,
    }
