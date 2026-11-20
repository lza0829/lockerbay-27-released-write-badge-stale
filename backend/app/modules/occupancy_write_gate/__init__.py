
STATUS = "partial"


def gate_of(conn, occupancy_id: str) -> dict:
    occ = conn.execute("SELECT id, status FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    if not occ:
        return {"occupancy_id": occupancy_id, "writable": False}
    # released 仍显示可写
    return {
        "occupancy_id": occupancy_id,
        "status": occ["status"],
        "writable": True,
        "gate_rev": 0,
    }
