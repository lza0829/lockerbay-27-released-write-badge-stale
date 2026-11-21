"""占用可写闸（write gate）与卸标审计。

背景：一条事件若指向已 ``released`` / ``closed`` 的占用，装载会把它整条
拒收进搁置；但基线只写了搁置，没有卸掉该占用的「可写角标」，于是
``GET /api/write-gate/{id}`` 仍报 ``writable=true``，格阵也继续亮可写点，
后续路由因此误判可追加。

定死的两处选择：

1. **落库形态**：独立 ``occupancy_write_gate`` 表 +
   ``occupancy_write_gate_audit`` 审计表。不再往 ``occupancies`` 行上加列，
   从而与基线「已释放占用行不可变」（UPDATE 触发器直接 ABORT）硬约束共存。
   表内自带 ``writable`` / ``gate_rev``，等价承载「不可变 writable + gate_rev」。
2. **提交形态**：拒收、卸写闸、写审计在**同一事务**内完成；任一步缺失即
   整体回滚（见 :func:`release` 的 SAVEPOINT 与
   ``released_occ_append_reject.record_reject``）。不采用「先写卸标草稿再
   二次落标志」的两段式。

不变量：

- 已 ``released`` / ``closed`` 的占用，``writable`` 恒为 ``False``（即使
  闸表里因历史原因残留 ``writable=1``，读取时也按占用状态强制收敛）。
- 每次真正卸掉写闸都写一条审计行（``occupancy_id`` / ``event_id`` /
  ``created_at`` / ``gate_rev`` / ``action``）。
- :func:`release` 只接受确实已 released/closed 的占用；对仍开放的占用调用
  会抛 :class:`GateViolation`，保证不会误卸其它 open 占用的写闸。
"""

from __future__ import annotations

import json
import itertools
from contextlib import contextmanager
from datetime import datetime, timezone

STATUS = "implemented"

TABLE = "occupancy_write_gate"
AUDIT_TABLE = "occupancy_write_gate_audit"

# 占用状态里只有这两类视为「已关闭」；其余（occupied 等）视为开放可写。
RELEASED_STATUSES = frozenset({"released", "closed"})

ACTION_REJECT = "reject_release"
ACTION_PICKUP = "pickup_release"
ACTION_MANUAL = "manual_release"


class GateViolation(RuntimeError):
    """试图给不存在的占用、或仍开放/未关闭的占用卸写闸。"""


_savepoint_seq = itertools.count(1)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def atomic(conn, tag: str):
    """函数级原子性：失败只回滚本函数写入，成功则释放保存点。

    若调用方已有事务（如整趟装载），保存点是嵌套的，不会提前提交；
    若没有事务，RELEASE 会提交本次写入，使单独调用 :func:`release` /
    ``record_reject`` 也具备「缺一回滚」。
    """
    name = f"wg_{tag}_{next(_savepoint_seq)}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


def _occupancy(conn, occupancy_id: str):
    return conn.execute(
        "SELECT id, cell_id, status, opened_at, closed_at, last_event_id "
        "FROM occupancies WHERE id=?",
        (occupancy_id,),
    ).fetchone()


def _row(conn, occupancy_id: str):
    return conn.execute(
        f"SELECT * FROM {TABLE} WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()


def is_released(status: str | None) -> bool:
    return status in RELEASED_STATUSES


def ensure_gate(conn, occupancy_id: str, at: str | None = None) -> dict | None:
    """开放占用开账时物化一条可写闸行（幂等）。

    只给仍开放的占用建闸；已 released/closed 的占用不补 ``writable=1``。
    """
    occ = _occupancy(conn, occupancy_id)
    if occ is None or is_released(occ["status"]):
        return None
    conn.execute(
        f"INSERT OR IGNORE INTO {TABLE}"
        "(occupancy_id, writable, gate_rev, released_at, released_by_event_id, "
        " release_reason, updated_at) VALUES (?, 1, 0, NULL, NULL, NULL, ?)",
        (occupancy_id, at or utc_now()),
    )
    return gate_of(conn, occupancy_id)


def release(
    conn,
    occupancy_id: str,
    *,
    event_id: str | None,
    action: str = ACTION_REJECT,
    at: str | None = None,
    reason: str | None = None,
    detail: dict | None = None,
) -> dict:
    """原子地卸掉某占用的写闸并写审计。

    - 只允许对确实已 released/closed 的占用卸闸；否则抛
      :class:`GateViolation`，避免误卸 open 占用的写闸。
    - 闸行 upsert 为 ``writable=0`` 且 ``gate_rev+1``；缺失时从 rev=1 建行。
    - 同一事务内写一条 :data:`AUDIT_TABLE` 审计行。
    """
    stamp = at or utc_now()
    with atomic(conn, "release"):
        occ = _occupancy(conn, occupancy_id)
        if occ is None:
            raise GateViolation(f"占用不存在，拒绝卸写闸: {occupancy_id}")
        if not is_released(occ["status"]):
            raise GateViolation(
                f"占用仍开放({occ['status']})，拒绝卸写闸: {occupancy_id}"
            )

        row = _row(conn, occupancy_id)
        gate_rev = (row["gate_rev"] if row is not None else 0) + 1
        conn.execute(
            f"INSERT INTO {TABLE}"
            "(occupancy_id, writable, gate_rev, released_at, released_by_event_id, "
            " release_reason, updated_at) VALUES (?, 0, ?, ?, ?, ?, ?) "
            "ON CONFLICT(occupancy_id) DO UPDATE SET "
            " writable=0, gate_rev=excluded.gate_rev, "
            " released_at=COALESCE(released_at, excluded.released_at), "
            " released_by_event_id=COALESCE(released_by_event_id, excluded.released_by_event_id), "
            " release_reason=COALESCE(release_reason, excluded.release_reason), "
            " updated_at=excluded.updated_at",
            (occupancy_id, gate_rev, stamp, event_id, reason or action, stamp),
        )
        cur = conn.execute(
            f"INSERT INTO {AUDIT_TABLE}"
            "(occupancy_id, event_id, action, gate_rev, created_at, detail_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                occupancy_id,
                event_id,
                action,
                gate_rev,
                stamp,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )
        audit_id = cur.lastrowid

    return {
        "occupancy_id": occupancy_id,
        "event_id": event_id,
        "action": action,
        "gate_rev": gate_rev,
        "created_at": stamp,
        "audit_id": audit_id,
    }


def gate_of(conn, occupancy_id: str) -> dict:
    """只读：某占用的写闸角标。

    ``writable`` 是「还能否向该占用追加事件」的唯一判据：
    占用不存在、已 released/closed、或闸行已卸，均返回 ``False``。
    """
    occ = _occupancy(conn, occupancy_id)
    if occ is None:
        return {
            "occupancy_id": occupancy_id,
            "status": None,
            "writable": False,
            "gate_rev": 0,
            "released_at": None,
            "released_by_event_id": None,
            "release_reason": "unknown_occupancy",
            "materialized": False,
        }

    row = _row(conn, occupancy_id)
    open_occ = not is_released(occ["status"])
    if row is None:
        # 未物化闸行的开放占用按可写处理（开账时通常已建行）。
        writable = open_occ
        gate_rev = 0
        released_at = None
        released_by_event_id = None
        release_reason = None
    else:
        # 占用状态是权威：已释放的行无论闸表残留什么都不可写。
        writable = bool(row["writable"]) and open_occ
        gate_rev = int(row["gate_rev"])
        released_at = row["released_at"]
        released_by_event_id = row["released_by_event_id"]
        release_reason = row["release_reason"]
        if open_occ and not writable:
            release_reason = release_reason or "gate_closed"

    return {
        "occupancy_id": occupancy_id,
        "status": occ["status"],
        "writable": writable,
        "gate_rev": gate_rev,
        "released_at": released_at,
        "released_by_event_id": released_by_event_id,
        "release_reason": release_reason,
        "materialized": row is not None,
    }


def audits_of(conn, occupancy_id: str, action: str | None = None) -> list[dict]:
    sql = (
        f"SELECT id, occupancy_id, event_id, action, gate_rev, created_at, detail_json "
        f"FROM {AUDIT_TABLE} WHERE occupancy_id=?"
    )
    params: list[str] = [occupancy_id]
    if action:
        sql += " AND action=?"
        params.append(action)
    sql += " ORDER BY id"
    out = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        try:
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
        except (TypeError, ValueError):
            item["detail"] = {}
        out.append(item)
    return out


def list_gates(conn, writable: bool | None = None) -> list[dict]:
    """只读：列出占用写闸（可按可写态过滤）。"""
    rows = conn.execute(
        f"SELECT w.*, o.status AS occupancy_status FROM {TABLE} w "
        "LEFT JOIN occupancies o ON o.id = w.occupancy_id ORDER BY w.occupancy_id"
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["writable"] = bool(item["writable"]) and not is_released(
            item.get("occupancy_status")
        )
        out.append(item)
    if writable is not None:
        out = [x for x in out if x["writable"] is writable]
    return out


def proof(conn, occupancy_id: str) -> dict:
    """只读：写闸角标核证。

    给出占用行、闸角标、全部卸标审计，并核对：占用已释放时写闸必须为关，
    且每条被拒事件都应有对应的卸标审计行。
    """
    occ = _occupancy(conn, occupancy_id)
    gate = gate_of(conn, occupancy_id)
    audits = audits_of(conn, occupancy_id)

    # 懒导入，避免 released_occ_append_reject 与本模块的顶层循环依赖。
    from app.modules import released_occ_append_reject as rejects_mod

    rejections = rejects_mod.list_rejects(conn, occupancy_id) if occ else []
    rejected_event_ids = [r["event_id"] for r in rejections]
    audited_event_ids = {
        a["event_id"] for a in audits if a["action"] == ACTION_REJECT and a["event_id"]
    }
    missing_audits = [
        eid for eid in rejected_event_ids if eid and eid not in audited_event_ids
    ]

    released = bool(occ) and is_released(occ["status"])
    release_audits = [a for a in audits if a["action"] != ACTION_PICKUP]
    consistent = True
    problems: list[str] = []
    if released:
        if gate["writable"]:
            consistent = False
            problems.append("occupancy_released_but_gate_writable")
        if not audits:
            consistent = False
            problems.append("gate_released_without_audit")
    if missing_audits:
        consistent = False
        problems.append("rejected_event_missing_gate_audit")

    return {
        "occupancy_id": occupancy_id,
        "occupancy": dict(occ) if occ else None,
        "released": released,
        "gate": gate,
        "audits": audits,
        "audit_count": len(audits),
        "release_audit_count": len(release_audits),
        "rejections": rejections,
        "rejected_event_ids": rejected_event_ids,
        "missing_audits": missing_audits,
        "consistent": consistent,
        "problems": problems,
        "read_only": True,
    }
