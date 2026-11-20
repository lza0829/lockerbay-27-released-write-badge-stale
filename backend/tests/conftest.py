import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
FIXTURES = BACKEND.parent / "fixtures"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """每个用例一份全新的 SQLite 库；夹具目录只读复用。"""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("DATA_DIR", str(d))
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES))

    from app import db

    monkeypatch.setattr(db, "DATA_DIR", d)
    monkeypatch.setattr(db, "DB_PATH", d / "lockerbay.db")
    monkeypatch.setattr(db, "FIXTURES_DIR", FIXTURES)

    from app import seed

    monkeypatch.setattr(seed, "FIXTURES_DIR", FIXTURES)
    seed.init_db()

    from app.services import ingest

    monkeypatch.setattr(ingest, "FIXTURES_DIR", FIXTURES)
    return d


@pytest.fixture()
def conn(data_dir):
    from app.db import connect

    c = connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def client(data_dir):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
