import os
import tempfile
from datetime import date

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")
# app을 import할 때 실행되는 init_db()가 프로젝트의 leave.db를 만들지 않도록 임시 경로를 쓴다
os.environ.setdefault("LEAVE_DB_PATH", os.path.join(tempfile.mkdtemp(), "import.db"))

import app as app_module  # noqa: E402

ADMIN_PW = "admin-test-pw"


# TEST_DATABASE_URL을 주면 같은 테스트를 Postgres로 실행한다 (테이블 내용을 지우므로 테스트 전용 DB만)
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 DB를 쓰는 테스트 클라이언트. 오늘은 기본으로 2026-11-04(수)."""
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(app_module, "DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.setattr(app_module, "today", lambda: date(2026, 11, 4))
    app_module.app.config["TESTING"] = True
    app_module.init_db()
    if TEST_DATABASE_URL:
        db = app_module.Database()
        db.execute("TRUNCATE requests, settings RESTART IDENTITY")
        db.commit()
        db.close()
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def set_today(monkeypatch):
    def _set(d):
        monkeypatch.setattr(app_module, "today", lambda: d)
    return _set


@pytest.fixture
def admin(client):
    """관리자로 로그인한 클라이언트."""
    assert client.post("/api/admin/login", json={"password": ADMIN_PW}).status_code == 204
    return client


def make_request(**overrides):
    data = {
        "rank": "상병", "name": "홍길동", "password": "pass1234",
        "type": "외출", "start_date": "2026-11-11", "end_date": "2026-11-11",
    }
    data.update(overrides)
    return data


def vacation(start, end, days=None, **overrides):
    n = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return make_request(type="휴가", start_date=start, end_date=end,
                        vacation_days=days if days is not None else {"연가": n}, **overrides)


def set_strength(admin_client, n):
    assert admin_client.put("/api/settings", json={"strength": n}).status_code == 200
