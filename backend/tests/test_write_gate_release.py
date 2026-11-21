"""写闸卸标：拒收成功必须同成败卸掉该占用可写角标，并留卸标审计。

硬约束：
- 禁止「回执 rejected 而该占用仍可写」；
- 禁止卸标审计缺行；
- 禁止误卸其它 open 占用写闸。

定死形态：独立 ``occupancy_write_gate`` + ``occupancy_write_gate_audit`` 表；
提交形态：拒收 + 卸闸 + 审计同事务，缺一回滚。
"""

import sqlite3

import pytest

from app.modules import occupancy_write_gate as write_gate

RELEASED_OCC = "OCC-CAB-A-01-E1"
OPEN_OCC = "OCC-CAB-A-02-E2"


def gate_row(conn, occupancy_id):
    return conn.execute(
        "SELECT * FROM occupancy_write_gate WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()


def audits(conn, occupancy_id, action=None):
    if action:
        rows = conn.execute(
            "SELECT * FROM occupancy_write_gate_audit WHERE occupancy_id=? AND action=? ORDER BY id",
            (occupancy_id, action),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM occupancy_write_gate_audit WHERE occupancy_id=? ORDER BY id",
            (occupancy_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def run_reject_fixture(client):
    r = client.post("/api/ingest/run", json={"fixture": "released_append_reject.json"})
    assert r.status_code == 200
    return r.json()


# --- 核心：已释放占用再灌后 writable=false -------------------------------


def test_released_gate_is_sealed_after_seed(conn):
    gate = write_gate.gate_of(conn, RELEASED_OCC)
    assert gate["writable"] is False
    assert gate["status"] == "released"
    # 种子取件释放已留下一条卸标审计
    assert any(a["action"] == write_gate.ACTION_PICKUP for a in audits(conn, RELEASED_OCC))


def test_rejected_append_leaves_released_occupancy_unwritable(client, conn):
    report = run_reject_fixture(client)
    assert report["rejected"] == 3

    row = gate_row(conn, RELEASED_OCC)
    assert row is not None
    assert row["writable"] == 0
    assert row["gate_rev"] >= 1

    gate = write_gate.gate_of(conn, RELEASED_OCC)
    assert gate["writable"] is False
    assert gate["status"] == "released"
    # 路由接口同判据
    assert client.get(f"/api/write-gate/{RELEASED_OCC}").json()["writable"] is False


def test_neighbor_open_gate_not_released(client, conn):
    """误卸防护：同柜/同格其它 open 占用写闸保持可写。"""
    before_open = write_gate.gate_of(conn, OPEN_OCC)
    assert before_open["writable"] is True
    assert before_open["status"] == "occupied"

    run_reject_fixture(client)

    after_open = write_gate.gate_of(conn, OPEN_OCC)
    assert after_open["writable"] is True
    assert after_open["status"] == "occupied"
    assert after_open["gate_rev"] == before_open["gate_rev"]
    assert audits(conn, OPEN_OCC, write_gate.ACTION_REJECT) == []

    # 拒收时新投放的 R0 占用同样可写，未被误卸
    assert write_gate.gate_of(conn, "OCC-CAB-A-01-R0")["writable"] is True


# --- 审计 ---------------------------------------------------------------


def test_each_rejection_has_release_audit(client, conn):
    run_reject_fixture(client)
    reject_audits = audits(conn, RELEASED_OCC, write_gate.ACTION_REJECT)
    assert [a["event_id"] for a in reject_audits] == ["R1", "R2", "R3"]
    for a in reject_audits:
        assert a["occupancy_id"] == RELEASED_OCC
        assert a["event_id"] in {"R1", "R2", "R3"}
        assert a["created_at"]  # 时刻必填
        assert a["action"] == write_gate.ACTION_REJECT


def test_rejected_events_audit_matches_quarantine(client, conn):
    run_reject_fixture(client)
    quarantined = {
        r["event_id"]
        for r in conn.execute(
            "SELECT event_id FROM quarantine WHERE reason='append_rejected' AND occupancy_id=?",
            (RELEASED_OCC,),
        ).fetchall()
    }
    audited = {
        a["event_id"] for a in audits(conn, RELEASED_OCC, write_gate.ACTION_REJECT)
    }
    assert quarantined == audited == {"R1", "R2", "R3"}


def test_gate_audits_endpoint(client):
    run_reject_fixture(client)
    rows = client.get(f"/api/occupancies/{RELEASED_OCC}/gate-audits").json()
    assert [r["event_id"] for r in rows if r["action"] == "reject_release"] == [
        "R1",
        "R2",
        "R3",
    ]


# --- 只读核证接口 -------------------------------------------------------


def test_write_gate_proof_reports_consistency(client, conn):
    run_reject_fixture(client)
    body = client.get(f"/api/occupancies/{RELEASED_OCC}/write-gate-proof").json()
    assert body["read_only"] is True
    assert body["released"] is True
    assert body["gate"]["writable"] is False
    assert body["consistent"] is True
    assert body["missing_audits"] == []
    assert body["audit_count"] == len(audits(conn, RELEASED_OCC))
    assert body["rejections"] and body["released"]


def test_write_gate_proof_404(client):
    assert client.get("/api/occupancies/NOPE/write-gate-proof").status_code == 404


def test_list_write_gates_and_filter(client):
    run_reject_fixture(client)
    sealed = client.get("/api/write-gates", params={"writable": "false"}).json()
    assert any(g["occupancy_id"] == RELEASED_OCC for g in sealed)
    assert all(g["writable"] is False for g in sealed)

    writable = client.get("/api/write-gates", params={"writable": "true"}).json()
    assert all(g["writable"] is True for g in writable)
    assert not any(g["occupancy_id"] == RELEASED_OCC for g in writable)


def test_proof_is_read_only(client, conn):
    run_reject_fixture(client)
    before = (
        [dict(r) for r in conn.execute("SELECT * FROM occupancy_write_gate ORDER BY occupancy_id")],
        [dict(r) for r in conn.execute("SELECT * FROM occupancy_write_gate_audit ORDER BY id")],
    )
    client.get(f"/api/occupancies/{RELEASED_OCC}/write-gate-proof")
    client.get("/api/write-gates")
    after = (
        [dict(r) for r in conn.execute("SELECT * FROM occupancy_write_gate ORDER BY occupancy_id")],
        [dict(r) for r in conn.execute("SELECT * FROM occupancy_write_gate_audit ORDER BY id")],
    )
    assert before == after


# --- 格阵角标 -----------------------------------------------------------


def test_bay_badge_reflects_gate(client, conn):
    bay = client.get("/api/bay").json()
    cell = next(
        c
        for cab in bay
        for c in cab["cells"]
        if c["id"] == "CAB-A-01"
    )
    # 种子后该格最新占用是已释放的 E1 → 角标不可写
    assert cell["occupancy"]["id"] == RELEASED_OCC
    assert cell["writable"] is False
    assert cell["write_gate"]["writable"] is False


def test_bay_badge_open_cell_writable(client):
    bay = client.get("/api/bay").json()
    cell = next(c for cab in bay for c in cab["cells"] if c["id"] == "CAB-A-02")
    assert cell["occupancy"]["id"] == OPEN_OCC
    assert cell["writable"] is True


# --- 失败闭：缺一回滚 ---------------------------------------------------


def test_reject_rolls_back_when_gate_release_fails(client, conn, monkeypatch):
    """卸闸失败 → 拒收搁置也不得留下（同事务缺一回滚）。"""

    def boom(*args, **kwargs):
        raise RuntimeError("gate release failed")

    before = {
        "quarantine": conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine WHERE reason='append_rejected'"
        ).fetchone()["c"],
        "events": conn.execute("SELECT COUNT(*) AS c FROM parcel_events").fetchone()["c"],
        "batches": conn.execute("SELECT COUNT(*) AS c FROM ingest_batches").fetchone()["c"],
        "audits": conn.execute(
            "SELECT COUNT(*) AS c FROM occupancy_write_gate_audit"
        ).fetchone()["c"],
        "gate": dict(gate_row(conn, RELEASED_OCC)),
    }
    monkeypatch.setattr(write_gate, "release", boom)
    # TestClient 默认把服务端异常抛回，这里断言整趟装载失败。
    with pytest.raises(RuntimeError, match="gate release failed"):
        client.post("/api/ingest/run", json={"fixture": "released_append_reject.json"})

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM quarantine WHERE reason='append_rejected'"
    ).fetchone()["c"] == before["quarantine"]
    assert conn.execute("SELECT COUNT(*) AS c FROM parcel_events").fetchone()["c"] == before["events"]
    assert conn.execute("SELECT COUNT(*) AS c FROM ingest_batches").fetchone()["c"] == before["batches"]
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM occupancy_write_gate_audit"
    ).fetchone()["c"] == before["audits"]
    assert dict(gate_row(conn, RELEASED_OCC)) == before["gate"]


def test_record_reject_atomic_when_quarantine_insert_fails(conn, monkeypatch):
    """搁置写入失败（审计前）→ 不留下任何写闸改动/审计。"""
    from app.modules import released_occ_append_reject as mod

    occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (RELEASED_OCC,)).fetchone()
    ev = {
        "id": "ATOMIC1",
        "cabinet_id": "CAB-A",
        "cell_id": "CAB-A-01",
        "event_type": "pickup",
        "event_ts": "2026-11-19T11:00:00Z",
        "tracking_no": "TN1001",
        "payload": {"occupancy_id": RELEASED_OCC},
    }
    rejection = mod.classify(conn, ev)
    assert rejection is not None

    real_release = write_gate.release

    def release_then_fail(*args, **kwargs):
        real_release(*args, **kwargs)
        raise RuntimeError("after release")

    monkeypatch.setattr(write_gate, "release", release_then_fail)
    with pytest.raises(RuntimeError):
        mod.record_reject(conn, rejection)
    conn.rollback()

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM quarantine WHERE occupancy_id=? AND event_id='ATOMIC1'",
        (RELEASED_OCC,),
    ).fetchone()["c"] == 0
    assert not conn.execute(
        "SELECT 1 FROM occupancy_write_gate_audit WHERE event_id='ATOMIC1'"
    ).fetchone()


def test_release_refuses_open_occupancy(conn):
    """禁止误卸：对仍开放占用直接调用 release 必须拒绝，且不写审计。"""
    with pytest.raises(write_gate.GateViolation):
        write_gate.release(conn, OPEN_OCC, event_id="X1")
    conn.rollback()
    assert write_gate.gate_of(conn, OPEN_OCC)["writable"] is True
    assert audits(conn, OPEN_OCC, write_gate.ACTION_REJECT) == []


def test_release_refuses_unknown_occupancy(conn):
    with pytest.raises(write_gate.GateViolation):
        write_gate.release(conn, "NO-SUCH-OCC", event_id="X1")
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM occupancy_write_gate_audit WHERE occupancy_id='NO-SUCH-OCC'"
    ).fetchone()["c"] == 0


def test_released_row_still_immutable_after_gate_work(client, conn):
    """写闸走独立表，已释放占用行仍不可 UPDATE（基线触发器保留）。"""
    run_reject_fixture(client)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE occupancies SET closed_at='2026-12-01T00:00:00Z' WHERE id=?",
            (RELEASED_OCC,),
        )
    conn.rollback()


def test_gate_rev_increments_on_each_release(client, conn):
    before = gate_row(conn, RELEASED_OCC)["gate_rev"]
    run_reject_fixture(client)
    after = gate_row(conn, RELEASED_OCC)["gate_rev"]
    assert after == before + 3  # R1/R2/R3 各卸一次
