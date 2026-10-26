from pathlib import Path

import pytest

from mih import db
from mih.config import Settings
from mih.sources.service import create_source

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


@pytest.fixture
def conn(settings: Settings):
    c = db.connect(settings.db_path)
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def make_source(conn, settings):
    def _make(name: str) -> str:
        return create_source(conn, settings, name=name, url=None)

    return _make


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
