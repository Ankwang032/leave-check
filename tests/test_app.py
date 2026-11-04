"""출타 신청 사이트 테스트. 기본 '오늘'은 2026-11-04(수)."""
from datetime import date

import pytest

import app as app_module
from conftest import make_request, set_strength, vacation


def post(client, data):
    return client.post("/api/requests", json=data)


# ── 출타율 ──────────────────────────────────────────────

@pytest.mark.parametrize("strength, vacation_max, total_max", [
    (0, 0, 0), (10, 2, 3), (14, 2, 4), (100, 20, 35), (7, 1, 2),
])
def test_limits_round_down(strength, vacation_max, total_max):
    lim = app_module.limits(strength)
    assert (lim["vacation_max"], lim["total_max"]) == (vacation_max, total_max)


def test_vacation_limit_warning(admin, set_today):
    set_today(date(2026, 11, 20))  # 12월 휴가 접수일
    set_strength(admin, 10)  # 휴가 최대 2, 전체 최대 3
    for _ in range(2):
        r = post(admin, vacation("2026-12-01", "2026-12-01"))
        assert r.status_code == 201 and r.json["warnings"] == []
    r = post(admin, vacation("2026-12-01", "2026-12-02"))
    assert r.status_code == 201  # 초과해도 신청은 받는다
    assert len(r.json["warnings"]) == 1
    assert "12/1 휴가 출타율 초과" in r.json["warnings"][0]
    assert "12/2" not in r.json["warnings"][0]


def test_total_limit_warning(admin):
    set_strength(admin, 10)
    for _ in range(3):
        assert post(admin, make_request()).json["warnings"] == []
    r = post(admin, make_request(type="외박", end_date="2026-11-12"))
    assert r.status_code == 201
    assert r.json["warnings"] == ["11/11 전체 출타율 초과 (최대 3명, 35%)"]


def test_official_excluded_from_limit(admin):
    set_strength(admin, 10)
    for _ in range(3):
        r = post(admin, make_request(official=True))
        assert r.json["warnings"] == [] and r.json["official"] is True
    # 공용 3명이 있어도 일반 외출은 아직 여유가 있다
    for _ in range(3):
        assert post(admin, make_request()).json["warnings"] == []
    # 공용 신청 자체는 초과 안내 대상이 아니다
    assert post(admin, make_request(official=True)).json["warnings"] == []
    assert post(admin, make_request()).json["warnings"] != []


def test_no_limit_when_strength_zero(client):
    for _ in range(5):
        assert post(client, make_request()).json["warnings"] == []


# ── 종합기간 ────────────────────────────────────────────

@pytest.mark.parametrize("today, start, ok", [
    # 오늘 11/4(수): 11/4~11/10 주(마감일)와 11/11~11/17 주(접수 중) 둘 다 가능
    (date(2026, 11, 4), "2026-11-04", True),
    (date(2026, 11, 4), "2026-11-10", True),
    (date(2026, 11, 4), "2026-11-11", True),
    (date(2026, 11, 4), "2026-11-17", True),
    (date(2026, 11, 4), "2026-11-18", False),  # 11/10부터 접수
    # 11/3(화): 접수 시작일
    (date(2026, 11, 3), "2026-11-11", True),
    (date(2026, 11, 2), "2026-11-11", False),
    # 11/12(목): 11/11~11/17 주는 마감
    (date(2026, 11, 12), "2026-11-13", False),
    (date(2026, 11, 12), "2026-11-18", True),
])
def test_outing_window(client, set_today, today, start, ok):
    set_today(today)
    r = post(client, make_request(start_date=start, end_date=start))
    assert (r.status_code == 201) is ok, r.json
    if not ok:
        assert "종합" in r.json["error"]


def test_multi_day_judged_by_start_date(client):
    # 11/17(화) 시작 외박은 11/11 주에 속하므로 11/18로 넘어가도 접수된다
    r = post(client, make_request(type="외박", start_date="2026-11-17", end_date="2026-11-19"))
    assert r.status_code == 201


@pytest.mark.parametrize("today, ok", [
    (date(2026, 11, 19), False),
    (date(2026, 11, 20), True),
    (date(2026, 11, 28), True),
    (date(2026, 11, 29), False),
])
def test_vacation_window(client, set_today, today, ok):
    set_today(today)
    r = post(client, vacation("2026-12-10", "2026-12-12"))
    assert (r.status_code == 201) is ok, r.json


def test_window_error_messages(client, set_today):
    r = post(client, make_request(start_date="2026-11-18", end_date="2026-11-18"))
    assert "아직 종합 전" in r.json["error"]
    set_today(date(2026, 11, 12))
    r = post(client, make_request(start_date="2026-11-13", end_date="2026-11-13"))
    assert "마감" in r.json["error"]


def test_window_disabled(admin):
    assert admin.put("/api/settings/window", json={"enabled": False}).status_code == 200
    assert post(admin, make_request(start_date="2027-03-03", end_date="2027-03-03")).status_code == 201
    assert post(admin, vacation("2027-03-03", "2027-03-05")).status_code == 201


def test_window_status_on_wednesday(client):
    s = client.get("/api/settings").json["status"]
    assert [p["from"] for p in s["outing_open"]] == ["2026-11-04", "2026-11-11"]
    assert s["next_outing"]["from"] == "2026-11-18"
    assert s["vacation_open"] == []
    assert s["next_vacation"]["open"] == "2026-11-20"


def test_window_status_on_tuesday(client, set_today):
    set_today(date(2026, 11, 10))
    s = client.get("/api/settings").json["status"]
    assert [p["from"] for p in s["outing_open"]] == ["2026-11-11", "2026-11-18"]


def test_vacation_close_day_clamped_to_month_end():
    w = app_module.DEFAULT_WINDOW | {"vacation_close_day": 31}
    _, _, open_, close = app_module.vacation_period(date(2027, 3, 5), w)
    assert (open_, close) == (date(2027, 2, 20), date(2027, 2, 28))


@pytest.mark.parametrize("body", [
    {"outing_open_before": 3, "outing_close_before": 5},
    {"vacation_open_day": 28, "vacation_close_day": 20},
    {"outing_weekday": 7},
    {"enabled": "yes"},
])
def test_invalid_window_settings(admin, body):
    assert admin.put("/api/settings/window", json=body).status_code == 400


# ── 신청 검증 ────────────────────────────────────────────

def test_past_date_rejected(client):
    r = post(client, make_request(start_date="2026-11-03", end_date="2026-11-03"))
    assert r.status_code == 400 and "지난 날짜" in r.json["error"]


def test_past_date_rejected_even_for_admin(admin):
    r = post(admin, make_request(start_date="2026-11-03", end_date="2026-11-03"))
    assert r.status_code == 400


def test_vacation_days_must_match_period(client, set_today):
    set_today(date(2026, 11, 20))
    r = post(client, vacation("2026-12-01", "2026-12-05", {"연가": 3, "포상": 1}))
    assert r.status_code == 400 and "4일" in r.json["error"] and "5일" in r.json["error"]
    r = post(client, vacation("2026-12-01", "2026-12-05", {"연가": 3, "포상": 2}))
    assert r.status_code == 201
    assert r.json["vacation_days"] == {"연가": 3, "포상": 2}


@pytest.mark.parametrize("days", [
    {"기타": 3}, {"연가": -1, "포상": 4}, {"연가": True, "포상": 2}, {"연가": "3"}, [3],
])
def test_invalid_vacation_days(client, set_today, days):
    set_today(date(2026, 11, 20))
    assert post(client, vacation("2026-12-01", "2026-12-03", days)).status_code == 400


@pytest.mark.parametrize("overrides", [
    {"rank": "대령"},
    {"name": ""},
    {"name": "가" * 21},
    {"type": "출장"},
    {"start_date": "2026-11-12", "end_date": "2026-11-11"},
    {"end_date": "2026-11-12"},  # 외출은 하루만
    {"start_date": "잘못된 날짜"},
    {"password": "123"},
    {"password": "x" * 31},
])
def test_invalid_request(client, overrides):
    assert post(client, make_request(**overrides)).status_code == 400


def test_reason_too_long(client, set_today):
    set_today(date(2026, 11, 20))
    r = post(client, vacation("2026-12-01", "2026-12-01", reason="가" * 101))
    assert r.status_code == 400


# ── 권한 · 개인정보 ──────────────────────────────────────

def test_cancel_requires_password(client):
    rid = post(client, make_request()).json["id"]
    assert client.delete(f"/api/requests/{rid}", json={"password": "wrong"}).status_code == 403
    assert client.delete(f"/api/requests/{rid}", json={}).status_code == 403
    assert client.delete(f"/api/requests/{rid}", json={"password": "pass1234"}).status_code == 204
    assert client.delete(f"/api/requests/{rid}", json={"password": "pass1234"}).status_code == 404


def test_admin_session_does_not_skip_cancel_password(admin):
    """관리자로 로그인돼 있어도 신청 화면 취소에는 비밀번호가 필요하다 (예전 버그)."""
    rid = post(admin, make_request()).json["id"]
    assert admin.delete(f"/api/requests/{rid}").status_code == 403
    assert admin.delete(f"/api/requests/{rid}", json={"password": "wrong"}).status_code == 403
    assert len(admin.get("/api/requests").json) == 1


def test_admin_delete_route(client):
    rid = post(client, make_request()).json["id"]
    assert client.delete(f"/api/admin/requests/{rid}").status_code == 401
    client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert client.delete(f"/api/admin/requests/{rid}").status_code == 204
    assert client.get("/api/requests").json == []


def test_reason_visible_only_to_admin(client, set_today):
    set_today(date(2026, 11, 20))
    post(client, vacation("2026-12-01", "2026-12-01", reason="가족 행사"))
    row = client.get("/api/requests").json[0]
    assert "reason" not in row
    assert "password_hash" not in row
    client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert client.get("/api/requests").json[0]["reason"] == "가족 행사"


def test_create_response_hides_secrets(client):
    r = post(client, make_request())
    assert "password_hash" not in r.json and "password" not in r.json


@pytest.mark.parametrize("method, path, body", [
    ("put", "/api/settings", {"strength": 10}),
    ("put", "/api/settings/window", {"enabled": False}),
    ("put", "/api/requests/1", make_request()),
    ("delete", "/api/admin/requests/1", None),
])
def test_admin_endpoints_require_login(client, method, path, body):
    assert getattr(client, method)(path, json=body).status_code == 401


def test_admin_login_logout(client):
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 204
    assert client.put("/api/settings", json={"strength": 5}).status_code == 200
    client.post("/api/admin/logout")
    assert client.put("/api/settings", json={"strength": 5}).status_code == 401


def test_print_requires_admin(client):
    r = client.get("/admin/print?from=2026-11-01&to=2026-11-30")
    assert r.status_code == 302


# ── 관리자 수정 ──────────────────────────────────────────

def test_admin_update_ignores_window(admin):
    rid = post(admin, make_request()).json["id"]
    # 접수 기간이 아닌 날짜로도 관리자는 수정할 수 있다
    r = admin.put(f"/api/requests/{rid}", json=make_request(name="김철수", start_date="2027-01-06",
                                                           end_date="2027-01-06"))
    assert r.status_code == 200
    assert r.json["name"] == "김철수" and r.json["start_date"] == "2027-01-06"


def test_admin_update_excludes_itself_from_limit(admin):
    set_strength(admin, 10)  # 전체 최대 3
    ids = [post(admin, make_request()).json["id"] for _ in range(3)]
    r = admin.put(f"/api/requests/{ids[0]}", json=make_request(name="수정"))
    assert r.status_code == 200 and r.json["warnings"] == []


def test_admin_update_not_found(admin):
    assert admin.put("/api/requests/999", json=make_request()).status_code == 404


# ── 화면 ────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/admin"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_print_page(admin):
    post(admin, make_request(name="인쇄확인"))
    r = admin.get("/admin/print?kind=outing&from=2026-11-01&to=2026-11-30")
    assert r.status_code == 200 and "인쇄확인" in r.get_data(as_text=True)


# ── 시간대 ──────────────────────────────────────────────

def test_today_uses_korea_time(monkeypatch):
    """Vercel 서버는 UTC라서 한국 날짜로 바꿔 계산해야 한다 (UTC 11/3 16시 = KST 11/4 01시)."""
    from datetime import datetime, timezone

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 3, 16, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(app_module, "datetime", FakeDateTime)
    assert app_module.today() == date(2026, 11, 4)
    assert app_module.now() == datetime(2026, 11, 4, 1, 0)


# ── 외출/외박 차수 · 포상 내용 ───────────────────────────

def test_outing_round_saved_and_public(client):
    r = post(client, make_request(outing_round=2))
    assert r.status_code == 201 and r.json["outing_round"] == 2
    assert client.get("/api/requests").json[0]["outing_round"] == 2


def test_outing_round_defaults_to_none(client):
    assert post(client, make_request()).json["outing_round"] == 0


@pytest.mark.parametrize("value", [3, -1, "1", True, 1.5])
def test_invalid_outing_round(client, value):
    r = post(client, make_request(outing_round=value))
    assert r.status_code == 400 and "차수" in r.json["error"]


def test_vacation_ignores_outing_round(client, set_today):
    set_today(date(2026, 11, 20))
    r = post(client, vacation("2026-12-01", "2026-12-01", outing_round=1))
    assert r.status_code == 201 and r.json["outing_round"] == 0


def test_reward_reason_visible_only_to_admin(client):
    post(client, make_request(type="외박", end_date="2026-11-12", reward=True, reason=" 체력검정 특급 포상 "))
    assert "reason" not in client.get("/api/requests").json[0]
    client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert client.get("/api/requests").json[0]["reason"] == "체력검정 특급 포상"


def test_reward_reason_dropped_without_reward(admin):
    r = post(admin, make_request(reward=False, reason="포상 아님"))
    assert r.json["reason"] == ""


def test_reward_reason_too_long(client):
    r = post(client, make_request(reward=True, reason="가" * 101))
    assert r.status_code == 400 and "포상 내용" in r.json["error"]


def test_admin_updates_round_and_reward_reason(admin):
    rid = post(admin, make_request()).json["id"]
    r = admin.put(f"/api/requests/{rid}", json=make_request(outing_round=1, reward=True, reason="분대장 포상"))
    assert r.status_code == 200
    assert (r.json["outing_round"], r.json["reward"], r.json["reason"]) == (1, True, "분대장 포상")


def test_print_shows_round_and_reward_reason(admin):
    post(admin, make_request(outing_round=1, reward=True, reason="분대장 포상"))
    html = admin.get("/admin/print?kind=outing&from=2026-11-01&to=2026-11-30").get_data(as_text=True)
    assert "1차·포상" in html and "분대장 포상" in html


def test_old_sqlite_db_gets_outing_round_column(tmp_path, monkeypatch):
    """outing_round 컬럼이 없던 이전 DB도 서버 시작 시 보정된다."""
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, rank TEXT NOT NULL, "
                   "name TEXT NOT NULL, type TEXT NOT NULL, start_date TEXT NOT NULL, "
                   "end_date TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.execute("INSERT INTO requests (rank, name, type, start_date, end_date, created_at) "
                   "VALUES ('상병', '홍길동', '외출', '2026-11-11', '2026-11-11', '2026-11-01T00:00:00')")
    monkeypatch.setattr(app_module, "DB_PATH", path)
    monkeypatch.setattr(app_module, "DATABASE_URL", None)
    app_module.init_db()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT outing_round FROM requests").fetchone() == (0,)
