"""Test setup. Tests use a temporary SQLite file and do not need Docker."""

import pytest
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db, get_engine, get_sessionmaker, reset_database_state
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolate_runtime_state():
    get_settings.cache_clear()
    reset_database_state()
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    reset_database_state()


@pytest.fixture
def app_context(tmp_path, monkeypatch):
    database_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{database_file}")
    monkeypatch.setenv("PROCESSING_DELAY_SECONDS", "0")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    get_settings.cache_clear()
    reset_database_state()
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    reset_database_state()
    get_settings.cache_clear()


@pytest.fixture
def session_factory(app_context):
    return get_sessionmaker()


@pytest.fixture
def client(app_context):
    def override_get_db():
        session = get_sessionmaker()()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
