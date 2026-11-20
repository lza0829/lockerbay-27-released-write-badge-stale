"""已释放占用拒收追加：核心不变量。

定死的形态：搁置表 quarantine + reason=append_rejected；
失败闭路径：入口识别分流（released 即转搁置，不进物化）。
"""

import json
import sqlite3
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"


def released_occupancy(conn):
    row = conn.execute(
        "SELECT * FROM occupancies WHERE status='released' ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None, "种子数据应产出一条已释放占用"
    return dict(row)


def snapshot(conn, occupancy_id):
    return dict(
        conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    )


def rejects(conn, occupancy_id=None):
    if occupancy_id is None:
        rows = conn.execute(
            "SELECT * FROM quarantine WHERE reason='append_rejected' ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM quarantine WHERE reason='append_rejected' AND occupancy_id=? ORDER BY id",
            (occupancy_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def test_seed_produces_released_occupancy(conn):
    occ = released_occupancy(conn)
    assert occ["id"] == "OCC-CAB-A-01-E1"
    assert occ["closed_at"] == "2026-11-18T12:00:00Z"


def test_explicit_occupancy_id_goes_to_quarantine_and_row_unchanged(client, conn):
    before = snapshot(conn, "OCC-CAB-A-01-E1")

    r = client.post("/api/ingest/run", json={"fixture": "released_append_reject.json"})
    assert r.status_code == 200
    report = r.json()
    assert report["rejected"] == 3
    assert report["total"] == 4

    after = snapshot(conn, "OCC-CAB-A-01-E1")
    assert after == before  # 历史行逐字段不变

    stored = rejects(conn, "OCC-CAB-A-01-E1")
    assert len(stored) == 3
    assert {row["event_id"] for row in stored} == {"R1", "R2", "R3"}
    assert all(row["reason"] == "append_rejected" for row in stored)

    # 原始载荷原样保留
    by_event = {row["event_id"]: json.loads(row["member_json"]) for row in stored}
    assert by_event["R1"]["payload"]["reason"] == "late_pickup_scan"
    assert by_event["R2"]["payload"]["new_tracking_no"] == "TN9999"
    assert by_event["R3"]["payload"] == {"occupancy": {"id": "OCC-CAB-A-01-E1"}}
    assert by_event["R1"]["occupancy_status"] == "released"
    assert by_event["R1"]["event"]["event_ts"] == "2026-11-19T04:00:00Z"

    # 被拒事件不得进入主通道
    for eid in ("R1", "R2", "R3"):
        assert conn.execute("SELECT 1 FROM parcel_events WHERE id=?", (eid,)).fetchone() is None


def test_released_never_reopened(client, conn):
    client.post("/api/ingest/run", json={"fixture": "released_append_reject.json"})
    row = snapshot(conn, "OCC-CAB-A-01-E1")
    assert row["status"] == "released"
    assert not conn.execute(
        "SELECT 1 FROM occupancies WHERE status='occupied' AND cell_id='CAB-A-01' AND id='OCC-CAB-A-01-E1'"
    ).fetchone()


def test_cell_state_not_disturbed_by_rejected_events(client, conn):
    """被拒事件不应改动格口/其他占用。R0 是正常二次投柜，应被受理。"""
    client.post("/api/ingest/run", json={"fixture": "released_append_reject.json"})
    row = conn.execute(
        "SELECT * FROM occupancies WHERE id='OCC-CAB-A-01-R0'"
    ).fetchone()
    assert row is not None and row["status"] == "occupied"
    assert row["cell_id"] == "CAB-A-01"


def test_implicit_repeat_pickup_on_released_is_rejected(client, conn):
    """无 occupancy_id，但对无开放占用的格口按同运单取件：视为指向已关闭占用。"""
    before = snapshot(conn, "OCC-CAB-A-01-E1")
    fixture = [
        {
            "id": "LATE1",
            "cabinet_id": "CAB-A",
            "cell_id": "CAB-A-01",
            "event_type": "pickup",
            "event_ts": "2026-11-19T07:00:00Z",
            "tracking_no": "TN1001",
            "payload": {},
        }
    ]
    from app.services import ingest as ingest_svc

    conn.execute("DELETE FROM parcel_events WHERE id='R0'")  # 让该格口无开放占用
    conn.commit()
    accepted, rejected, dup = ingest_svc.ingest_events(conn, fixture)
    conn.commit()

    assert (accepted, rejected) == (0, 1)
    assert snapshot(conn, "OCC-CAB-A-01-E1") == before
    assert [row["event_id"] for row in rejects(conn, "OCC-CAB-A-01-E1")] == ["LATE1"]


def test_normal_pickup_still_accepted(client, conn):
    """正常取件（指向当前开放占用）不受影响。"""
    fixture = [
        {
            "id": "OK1",
            "cabinet_id": "CAB-A",
            "cell_id": "CAB-A-02",
            "event_type": "pickup",
            "event_ts": "2026-11-19T08:00:00Z",
            "tracking_no": "TN1002",
            "payload": {},
        }
    ]
    from app.services import ingest as ingest_svc

    accepted, rejected, dup = ingest_svc.ingest_events(conn, fixture)
    conn.commit()
    assert (accepted, rejected) == (1, 0)
    row = conn.execute(
        "SELECT * FROM occupancies WHERE cell_id='CAB-A-02'"
    ).fetchone()
    assert row["status"] == "released"
    assert row["closed_at"] == "2026-11-19T08:00:00Z"


def test_redelivery_of_same_fixture_does_not_duplicate(conn):
    from app.services import ingest as ingest_svc

    events = json.loads(
        (FIXTURES / "released_append_reject.json").read_text(encoding="utf-8")
    )["events"]
    first = ingest_svc.ingest_events(conn, events)
    conn.commit()
    second = ingest_svc.ingest_events(conn, events)
    conn.commit()
    assert first[1] == 3
    assert second[0] == 0 and second[1] == 0
    assert second[2] == 4  # 3 条拒收重复 + R0 主通道重复
    assert len(rejects(conn, "OCC-CAB-A-01-E1")) == 3


def test_immutability_trigger_blocks_direct_update(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE occupancies SET closed_at='2026-11-20T00:00:00Z' WHERE id='OCC-CAB-A-01-E1'"
        )
    conn.rollback()
    assert snapshot(conn, "OCC-CAB-A-01-E1")["closed_at"] == "2026-11-18T12:00:00Z"


def test_immutability_trigger_blocks_reopen(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE occupancies SET status='occupied' WHERE id='OCC-CAB-A-01-E1'"
        )
    conn.rollback()
    assert snapshot(conn, "OCC-CAB-A-01-E1")["status"] == "released"


def test_readonly_reject_proof(client, conn):
    client.post("/api/ingest/run", json={"fixture": "released_append_reject.json"})
    before = snapshot(conn, "OCC-CAB-A-01-E1")

    r = client.get("/api/occupancies/OCC-CAB-A-01-E1/reject-proof")
    assert r.status_code == 200
    body = r.json()
    assert body["read_only"] is True
    assert body["immutable"] is True
    assert body["rejected_count"] == 3
    assert body["occupancy"]["closed_at"] == before["closed_at"]
    assert [x["event_id"] for x in body["rejections"]] == ["R1", "R2", "R3"]

    after = snapshot(conn, "OCC-CAB-A-01-E1")
    assert after == before  # 只读接口不改账


def test_reject_proof_404(client):
    assert client.get("/api/occupancies/NOPE/reject-proof").status_code == 404


def test_list_rejects_endpoint_filters(client):
    client.post("/api/ingest/run", json={"fixture": "released_append_reject.json"})
    all_rows = client.get("/api/released-rejects").json()
    assert len(all_rows) == 3
    filtered = client.get(
        "/api/released-rejects", params={"occupancy_id": "OCC-CAB-A-01-E1"}
    ).json()
    assert len(filtered) == 3
    assert client.get(
        "/api/released-rejects", params={"occupancy_id": "OTHER"}
    ).json() == []


def test_quarantine_endpoint_exposes_ids(client):
    client.post("/api/ingest/run", json={"fixture": "released_append_reject.json"})
    rows = client.get("/api/quarantine").json()
    row = next(r for r in rows if r["reason"] == "append_rejected")
    assert row["occupancy_id"] == "OCC-CAB-A-01-E1"
    assert row["event_id"].startswith("R")


def test_explicit_id_to_open_occupancy_not_rejected(conn):
    """指向仍开放的占用不拒收（只拒已 released/closed）。"""
    from app.modules import released_occ_append_reject as mod

    ev = {
        "id": "OPEN1",
        "cabinet_id": "CAB-A",
        "cell_id": "CAB-A-02",
        "event_type": "pickup",
        "event_ts": "2026-11-19T09:00:00Z",
        "tracking_no": "TN1002",
        "payload": {"occupancy_id": "OCC-CAB-A-02-E2"},
    }
    assert mod.classify(conn, ev) is None


def test_record_reject_dedupes_when_event_has_no_id(conn):
    """无事件 id 时按稳定键去重，不重复落搁置。"""
    from app.modules import released_occ_append_reject as mod

    occ = released_occupancy(conn)
    ev = {
        "cabinet_id": "CAB-A",
        "cell_id": "CAB-A-01",
        "event_type": "pickup",
        "event_ts": "2026-11-19T10:00:00Z",
        "tracking_no": "TN1001",
        "payload": {"occupancy_id": occ["id"]},
    }
    rejection = mod.classify(conn, ev)
    assert rejection is not None
    assert mod.record_reject(conn, rejection) is True
    assert mod.record_reject(conn, rejection) is False
    assert len(rejects(conn, occ["id"])) == 1


def test_batch_report_counts(client):
    r = client.post("/api/ingest/run", json={"fixture": "released_append_reject.json"})
    body = r.json()
    assert body["accepted"] == 1  # R0 正常二次投柜
    assert body["rejected"] == 3  # R1/R2/R3 指向已释放占用
    assert body["total"] == 4

    conn_rows = client.get("/api/occupancies").json()
    occ = next(x for x in conn_rows if x["id"] == "OCC-CAB-A-01-E1")
    assert occ["status"] == "released"
    assert occ["closed_at"] == "2026-11-18T12:00:00Z"


def test_triggers_fire_during_ingest_rolls_back_whole_batch(client, conn, monkeypatch):
    """失败闭：万一有写入试图改 released 行，触发器 ABORT，整趟回滚。"""
    from app.services import ingest as ingest_svc

    before = snapshot(conn, "OCC-CAB-A-01-E1")
    before_events = conn.execute("SELECT COUNT(*) AS c FROM parcel_events").fetchone()["c"]
    before_batches = conn.execute("SELECT COUNT(*) AS c FROM ingest_batches").fetchone()["c"]

    original = ingest_svc.apply_event

    def poisoned(conn_, ev):
        ok = original(conn_, ev)
        if ev["id"] == "R0":
            conn_.execute(
                "UPDATE occupancies SET status='occupied' WHERE id='OCC-CAB-A-01-E1'"
            )
        return ok

    monkeypatch.setattr(ingest_svc, "apply_event", poisoned)
    with pytest.raises(sqlite3.IntegrityError):
        ingest_svc.run_batch("released_append_reject.json")

    assert snapshot(conn, "OCC-CAB-A-01-E1") == before
    assert conn.execute("SELECT COUNT(*) AS c FROM parcel_events").fetchone()["c"] == before_events
    assert conn.execute("SELECT COUNT(*) AS c FROM ingest_batches").fetchone()["c"] == before_batches
    assert rejects(conn) == []
