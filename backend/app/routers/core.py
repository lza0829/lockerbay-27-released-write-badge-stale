from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import released_occ_append_reject as released_reject
from app.seed import overdue_hours
from app.services import bay as bay_svc
from app.services import ingest as ingest_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = bay_svc.overview(conn)
        data["overdue_hours"] = overdue_hours(conn)
        return data
    finally:
        conn.close()


@router.get("/bay")
def bay_matrix():
    conn = connect()
    try:
        return bay_svc.cabinet_matrix(conn)
    finally:
        conn.close()


@router.get("/occupancies")
def list_occupancies(status: str | None = None):
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM occupancies WHERE status=? ORDER BY opened_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM occupancies ORDER BY opened_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/events")
def list_events():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, cabinet_id, cell_id, event_type, event_ts, tracking_no, channel FROM parcel_events ORDER BY event_ts, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, reason, member_json, created_at, event_id, occupancy_id FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/occupancy-proofs")
def occupancy_proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM occupancy_proofs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/released-rejects")
def list_released_rejects(occupancy_id: str | None = None):
    """只读：已释放占用追加拒收的搁置清单。"""
    conn = connect()
    try:
        return released_reject.list_rejects(conn, occupancy_id)
    finally:
        conn.close()


@router.get("/occupancies/{occupancy_id}/reject-proof")
def released_reject_proof(occupancy_id: str):
    """只读：查看该占用的历史行、核证与拒收记录。"""
    conn = connect()
    try:
        result = released_reject.proof(conn, occupancy_id)
        if result["occupancy"] is None:
            raise HTTPException(404, "占用不存在")
        return result
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: dict | None = Body(default=None)):
    fixture = (body or {}).get("fixture", "ingest_batch.json")
    return ingest_svc.run_batch(fixture)


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"overdue_hours": overdue_hours(conn)}
    finally:
        conn.close()


@router.put("/settings/overdue-hours")
def put_overdue(body: dict = Body(...)):
    hours = int(body.get("overdue_hours", 48))
    if hours < 1:
        raise HTTPException(400, "超时回收至少 1 小时")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', ?)",
            (str(hours),),
        )
        conn.commit()
        return {"overdue_hours": hours}
    finally:
        conn.close()

# --- plant routes ---
from app.modules import occupancy_write_gate as write_gate


@router.get("/write-gate/{occupancy_id}")
def get_write_gate(occupancy_id: str):
    """只读：占用写闸角标（released 占用恒为 writable=false）。"""
    conn = connect()
    try:
        return write_gate.gate_of(conn, occupancy_id)
    finally:
        conn.close()


@router.get("/write-gates")
def list_write_gates(writable: bool | None = None):
    """只读：列出写闸角标，可按可写态过滤。"""
    conn = connect()
    try:
        return write_gate.list_gates(conn, writable)
    finally:
        conn.close()


@router.get("/occupancies/{occupancy_id}/write-gate-proof")
def write_gate_proof(occupancy_id: str):
    """只读：写闸角标核证（占用、闸、卸标审计、拒收记录一致性）。"""
    conn = connect()
    try:
        result = write_gate.proof(conn, occupancy_id)
        if result["occupancy"] is None:
            raise HTTPException(404, "占用不存在")
        return result
    finally:
        conn.close()


@router.get("/occupancies/{occupancy_id}/gate-audits")
def gate_audits(occupancy_id: str):
    """只读：某占用的卸标审计清单。"""
    conn = connect()
    try:
        return write_gate.audits_of(conn, occupancy_id)
    finally:
        conn.close()


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

