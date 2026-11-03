import os

import pytest
from sqlalchemy import text

from app import db

PG_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def engine(request, tmp_path):
    if request.param == "sqlite":
        eng = db.make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    else:
        eng = db.make_engine(PG_URL)
        with eng.begin() as conn:
            for t in reversed(db.metadata.sorted_tables):
                conn.execute(text(f'DROP TABLE IF EXISTS "{t.name}" CASCADE'))
    db.init_db(eng)
    yield eng
    eng.dispose()
