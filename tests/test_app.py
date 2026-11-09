"""출타 신청 사이트 테스트. 기본 '오늘'은 2026-11-04(수)."""
from datetime import date

import pytest

import app as app_module
from conftest import make_request, set_strength, vacation


# 테스트 신청이 모두 들어가는 조회 기간 (목록 API는 기간이 필수)
LIST = "/api/requests?from=2026-11-01&to=2026-12-31"


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
    assert len(admin.get(LIST).json) == 1


def test_admin_delete_route(client):
    rid = post(client, make_request()).json["id"]
    assert client.delete(f"/api/admin/requests/{rid}").status_code == 401
    client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert client.delete(f"/api/admin/requests/{rid}").status_code == 204
    assert client.get(LIST).json == []


def test_reason_visible_only_to_admin(client, set_today):
    set_today(date(2026, 11, 20))
    post(client, vacation("2026-12-01", "2026-12-01", reason="가족 행사"))
    row = client.get(LIST).json[0]
    assert "reason" not in row
    assert "password_hash" not in row
    client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert client.get(LIST).json[0]["reason"] == "가족 행사"


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
    assert client.get(LIST).json[0]["outing_round"] == 2


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
    assert "reason" not in client.get(LIST).json[0]
    client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert client.get(LIST).json[0]["reason"] == "체력검정 특급 포상"


def test_reward_reason_dropped_without_reward(admin):
    r = post(admin, make_request(reward=False, reason="포상 아님"))
    assert r.json["reason"] == ""


def test_reward_reason_too_long(client):
    r = post(client, make_request(reward=True, reason="가" * 101))
    assert r.status_code == 400 and "포상 내용" in r.json["error"]


def test_official_reason_saved_and_admin_only(client):
    r = post(client, make_request(official=True, reason=" 국군병원 외진 "))
    assert r.status_code == 201 and "reason" not in r.json
    assert "reason" not in client.get(LIST).json[0]
    client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert client.get(LIST).json[0]["reason"] == "국군병원 외진"


def test_reward_and_official_share_reason(admin):
    r = post(admin, make_request(reward=True, official=True, reason="분대장 포상 / 외진"))
    assert r.json["reason"] == "분대장 포상 / 외진"


@pytest.mark.parametrize("flags, label", [
    ({"official": True}, "공용 세부사항"),
    ({"reward": True, "official": True}, "포상·공용 세부사항"),
])
def test_official_reason_errors_use_matching_label(client, flags, label):
    r = post(client, make_request(**flags, reason="가" * 101))
    assert r.status_code == 400 and label in r.json["error"]
    r = post(client, make_request(**flags, reason="외진‮"))
    assert r.status_code == 400 and label in r.json["error"]


def test_admin_unchecking_official_drops_reason(admin):
    rid = post(admin, make_request(official=True, reason="국군병원 외진")).json["id"]
    r = admin.put(f"/api/requests/{rid}", json=make_request(official=False, reason="국군병원 외진"))
    assert r.status_code == 200 and r.json["reason"] == ""


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


# ── 접속 코드 ───────────────────────────────────────────

ACCESS_CODE = "unit-2026"


def set_access(client, code=ACCESS_CODE):
    """관리자로 접속 코드를 정한 뒤, 세션을 비워 코드를 입력하지 않은 방문자 상태로 돌아간다."""
    client.post("/api/access", json={"code": ACCESS_CODE})  # 이미 코드가 있으면 먼저 입력 (없으면 그냥 통과)
    assert client.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 204
    assert client.put("/api/settings/access", json={"code": code}).status_code == 200
    with client.session_transaction() as s:
        s.clear()


def test_no_access_code_means_open(client):
    assert client.get("/").status_code == 200
    assert client.get(LIST).status_code == 200


def test_access_code_blocks_pages_and_api(client):
    post(client, make_request())
    set_access(client)
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].startswith("/access")
    assert client.get(LIST).status_code == 401
    assert client.get("/api/settings").status_code == 401
    assert post(client, make_request()).status_code == 401
    assert client.delete("/api/requests/1", json={"password": "pass1234"}).status_code == 401


def test_access_code_entry(client):
    set_access(client)
    assert client.post("/api/access", json={"code": "wrong-code"}).status_code == 401
    assert client.get(LIST).status_code == 401
    assert client.post("/api/access", json={"code": ACCESS_CODE}).status_code == 204
    assert client.get("/").status_code == 200
    assert client.get(LIST).status_code == 200


def test_changing_access_code_logs_everyone_out(client):
    set_access(client)
    client.post("/api/access", json={"code": ACCESS_CODE})
    set_access(client, "new-code-99")
    assert client.get(LIST).status_code == 401
    assert client.post("/api/access", json={"code": ACCESS_CODE}).status_code == 401
    assert client.post("/api/access", json={"code": "new-code-99"}).status_code == 204


def test_admin_requires_access_code(client):
    set_access(client)
    r = client.get("/admin")
    assert r.status_code == 302 and r.headers["Location"] == "/access?next=/admin"
    assert client.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 401
    client.post("/api/access", json={"code": ACCESS_CODE})
    assert client.get("/admin").status_code == 200
    assert client.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 204


def test_admin_session_does_not_bypass_changed_code(client):
    other = app_module.app.test_client()
    set_access(client)
    for c in (client, other):
        c.post("/api/access", json={"code": ACCESS_CODE})
        assert c.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 204
    assert client.put("/api/settings/access", json={"code": "new-code-99"}).status_code == 200
    assert client.put("/api/settings", json={"strength": 5}).status_code == 200   # 바꾼 사람은 유지
    assert other.put("/api/settings", json={"strength": 5}).status_code == 401    # 다른 관리자는 새 코드 필요
    assert other.get("/admin").status_code == 302


def test_logout_works_without_access(client):
    set_access(client)
    assert client.post("/api/admin/logout").status_code == 204


def test_clear_access_code(client):
    set_access(client)
    client.post("/api/access", json={"code": ACCESS_CODE})
    client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert client.put("/api/settings/access", json={}).status_code == 400  # 실수로 해제되지 않게 빈 코드만 허용
    assert client.put("/api/settings/access", json={"code": ""}).json == {"enabled": False}
    client.post("/api/admin/logout")
    assert client.get(LIST).status_code == 200


@pytest.mark.parametrize("code", ["short", "x" * 31, 123456])
def test_invalid_access_code(admin, code):
    assert admin.put("/api/settings/access", json={"code": code}).status_code == 400


def test_access_code_requires_admin(client):
    assert client.put("/api/settings/access", json={"code": ACCESS_CODE}).status_code == 401


def test_access_code_stored_as_hash(client):
    set_access(client)
    db = app_module.Database()
    value = db.execute("SELECT value FROM settings WHERE key = 'access'").fetchone()["value"]
    db.close()
    assert ACCESS_CODE not in value


@pytest.mark.parametrize("next_url, expected", [
    ("/admin", "/admin"),
    ("//evil.example", "/"),
    ("https://evil.example", "/"),
    ("/\\evil.example", "/"),
    ("/\t/evil.example", "/"),
])
def test_access_redirect_stays_on_site(client, next_url, expected):
    # 코드가 없으면 접속 화면은 바로 next로 보낸다
    r = client.get("/access", query_string={"next": next_url})
    assert r.status_code == 302 and r.headers["Location"] == expected


def test_admin_session_expires(client, monkeypatch):
    client.post("/api/admin/login", json={"password": "admin-test-pw"})
    later = app_module.time.time() + app_module.ADMIN_SESSION_SECONDS + 1
    monkeypatch.setattr(app_module.time, "time", lambda: later)
    assert client.put("/api/settings", json={"strength": 5}).status_code == 401


def test_noindex(client):
    assert client.get("/").headers["X-Robots-Tag"] == "noindex, nofollow"
    assert "Disallow: /" in client.get("/robots.txt").get_data(as_text=True)


def test_access_page_renders_when_code_set(client):
    set_access(client)
    r = client.get("/access?next=/admin")
    assert r.status_code == 200 and "접속 코드" in r.get_data(as_text=True)


# ── 관리자 비밀번호 변경 ────────────────────────────────

def change_pw(client, current, new):
    return client.put("/api/settings/admin-password", json={"current": current, "new": new})


def test_change_admin_password(admin):
    assert change_pw(admin, "admin-test-pw", "new-admin-pw").status_code == 200
    assert admin.put("/api/settings", json={"strength": 5}).status_code == 200  # 바꾼 사람은 로그인 유지
    admin.post("/api/admin/logout")
    assert admin.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 401
    assert admin.post("/api/admin/login", json={"password": "new-admin-pw"}).status_code == 204


def test_change_admin_password_logs_out_other_admins(client):
    other = app_module.app.test_client()
    for c in (client, other):
        assert c.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 204
    change_pw(client, "admin-test-pw", "new-admin-pw")
    assert other.put("/api/settings", json={"strength": 5}).status_code == 401
    assert client.put("/api/settings", json={"strength": 5}).status_code == 200


@pytest.mark.parametrize("current, new, status", [
    ("wrong", "new-admin-pw", 403),
    ("admin-test-pw", "short", 400),
    ("admin-test-pw", "x" * 65, 400),
    ("admin-test-pw", None, 400),
])
def test_change_admin_password_invalid(admin, current, new, status):
    assert change_pw(admin, current, new).status_code == status
    admin.post("/api/admin/logout")
    assert admin.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 204


def test_change_admin_password_requires_login(client):
    assert change_pw(client, "admin-test-pw", "new-admin-pw").status_code == 401


def test_admin_password_stored_as_hash(admin):
    change_pw(admin, "admin-test-pw", "new-admin-pw")
    db = app_module.Database()
    value = db.execute("SELECT value FROM settings WHERE key = 'admin'").fetchone()["value"]
    db.close()
    assert "new-admin-pw" not in value


def test_admin_login_non_ascii_password(client):
    assert client.post("/api/admin/login", json={"password": "비밀번호"}).status_code == 401


def test_header_day_tile_uses_korea_today(client):
    html = client.get("/").get_data(as_text=True)
    assert 'aria-label="오늘 11월 4일 수요일"' in html


def test_admin_header_day_tile(admin):
    assert 'aria-label="오늘 11월 4일 수요일"' in admin.get("/admin").get_data(as_text=True)


# ── 보안: 입력 타입·제어문자·크기 ───────────────────────

@pytest.mark.parametrize("body", [
    [1, 2], "문자열", make_request(name=123), make_request(rank={"a": 1}),
    make_request(password=["a", "b", "c", "d"]), make_request(password=12345),
    make_request(start_date=20261111), make_request(reward=True, reason=5),
])
def test_wrong_types_are_rejected_not_500(client, body):
    assert post(client, body).status_code == 400


@pytest.mark.parametrize("path, body", [
    ("/api/access", {"code": 123456}),
    ("/api/admin/login", {"password": ["x"]}),
])
def test_wrong_types_on_login_routes(client, path, body):
    assert client.post(path, json=body).status_code in (204, 401)


def test_wrong_type_cancel_password(client):
    rid = post(client, make_request()).json["id"]
    assert client.delete(f"/api/requests/{rid}", json={"password": 1234}).status_code == 403


def test_window_settings_array_body(admin):
    assert admin.put("/api/settings/window", json=["enabled"]).status_code == 200


@pytest.mark.parametrize("name", ["가‮나", "가\u0000나", "가​나", "가\n나"])
def test_control_chars_in_name_rejected(client, name):
    assert post(client, make_request(name=name)).status_code == 400


def test_reason_newlines_become_spaces(admin):
    rid = post(admin, make_request(reward=True, reason="체력\n특급")).json["id"]
    row = [r for r in admin.get(LIST).json if r["id"] == rid][0]
    assert row["reason"] == "체력 특급"


def test_reason_control_chars_rejected(client):
    assert post(client, make_request(reward=True, reason="포상‮")).status_code == 400


def test_large_body_rejected(client):
    r = client.post("/api/requests", data="x" * (100 * 1024), content_type="application/json")
    assert r.status_code == 413


# ── 보안: 연속 오입력 제한 ───────────────────────────────

def test_admin_login_lockout(client):
    for _ in range(5):
        assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    r = client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert r.status_code == 429 and "분 뒤" in r.json["error"]


def test_admin_login_success_resets_failures(client):
    for _ in range(4):
        client.post("/api/admin/login", json={"password": "wrong"})
    assert client.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 204
    for _ in range(4):
        assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401


def test_lockout_expires(client, monkeypatch):
    for _ in range(5):
        client.post("/api/admin/login", json={"password": "wrong"})
    later = app_module.now() + app_module.timedelta(minutes=16)
    monkeypatch.setattr(app_module, "now", lambda: later)
    assert client.post("/api/admin/login", json={"password": "admin-test-pw"}).status_code == 204


def test_access_code_lockout(client):
    set_access(client)
    for _ in range(10):
        assert client.post("/api/access", json={"code": "wrong-code"}).status_code == 401
    assert client.post("/api/access", json={"code": ACCESS_CODE}).status_code == 429


def test_cancel_password_lockout(client):
    rid = post(client, make_request(password="1234")).json["id"]
    for i in range(10):
        assert client.delete(f"/api/requests/{rid}", json={"password": f"{i:04d}x"}).status_code == 403
    assert client.delete(f"/api/requests/{rid}", json={"password": "1234"}).status_code == 429


def test_cancel_lockout_per_request_across_ips(client):
    rid = post(client, make_request(password="1234")).json["id"]
    for i in range(10):
        env = {"REMOTE_ADDR": f"10.0.0.{i}"}
        assert client.delete(f"/api/requests/{rid}", json={"password": "nope"}, environ_base=env).status_code == 403
    r = client.delete(f"/api/requests/{rid}", json={"password": "1234"}, environ_base={"REMOTE_ADDR": "10.0.1.1"})
    assert r.status_code == 429


def test_change_password_counts_as_admin_failure(admin):
    for _ in range(5):
        assert change_pw(admin, "wrong", "new-admin-pw").status_code == 403
    assert change_pw(admin, "admin-test-pw", "new-admin-pw").status_code == 429


# ── 보안: CSRF·헤더·쿠키 ────────────────────────────────

def test_cross_site_write_blocked(admin):
    r = admin.put("/api/settings", json={"strength": 3}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert admin.put("/api/settings", json={"strength": 3}, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_form_post_not_accepted_as_json(admin):
    r = admin.put("/api/settings", data='{"strength": 3}', content_type="text/plain")
    assert r.status_code == 400


def test_security_headers(client):
    r = client.get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "'nonce-" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"


def test_scripts_carry_nonce(admin):
    for path in ["/", "/admin", "/admin/print?from=2026-11-01&to=2026-11-30"]:
        r = admin.get(path)
        nonce = r.headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
        html = r.get_data(as_text=True)
        assert html.count("<script") == html.count(f'<script nonce="{nonce}"')


def test_session_cookie_samesite(client):
    r = client.post("/api/admin/login", json={"password": "admin-test-pw"})
    assert "SameSite=Lax" in r.headers["Set-Cookie"] and "HttpOnly" in r.headers["Set-Cookie"]


def test_xss_name_escaped_in_print(admin):
    post(admin, make_request(name="<b>x</b>", reward=True, reason="<script>alert(1)</script>"))
    html = admin.get("/admin/print?from=2026-11-01&to=2026-11-30").get_data(as_text=True)
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<script>alert(1)" not in html


# ── 리뷰 수정: 외박 기간, 목록 조회 기간, 총원 타입, 오늘 날짜 ─────────

@pytest.mark.parametrize("end, ok", [
    ("2026-11-11", False),   # 0박
    ("2026-11-12", True),    # 1박
    ("2026-11-17", True),    # 6박
    ("2026-11-18", False),   # 7박
    ("9999-12-31", False),   # 끝없는 외박
])
def test_overnight_length(client, end, ok):
    r = post(client, make_request(type="외박", end_date=end))
    assert r.status_code == (201 if ok else 400)
    if not ok:
        assert "외박은 1박부터" in r.json["error"]


def test_admin_update_also_checks_overnight_length(admin):
    rid = post(admin, make_request(type="외박", end_date="2026-11-12")).json["id"]
    r = admin.put(f"/api/requests/{rid}", json=make_request(type="외박", end_date="2027-11-12"))
    assert r.status_code == 400


@pytest.mark.parametrize("query", [
    "",                                   # 기간 없음
    "?from=2026-11-01",                   # to 없음
    "?from=abc&to=2026-11-30",            # 날짜 형식 아님
    "?from=2026-11-30&to=2026-11-01",     # 거꾸로
    "?from=2026-01-01&to=2026-12-31",     # 너무 김
])
def test_list_requires_valid_range(client, query):
    assert client.get("/api/requests" + query).status_code == 400


def test_list_range_returns_overlapping_only(client):
    post(client, make_request(start_date="2026-11-11", end_date="2026-11-11"))
    assert len(client.get("/api/requests?from=2026-11-11&to=2026-11-11").json) == 1
    assert client.get("/api/requests?from=2026-11-12&to=2026-11-30").json == []


@pytest.mark.parametrize("value", [True, False])
def test_strength_rejects_bool(admin, value):
    assert admin.put("/api/settings", json={"strength": value}).status_code == 400


def test_page_uses_server_today(client, set_today):
    set_today(date(2026, 11, 5))
    assert b'const todayIso = "2026-11-05"' in client.get("/").data


def test_postgres_placeholder_conversion_keeps_percent_literal():
    """Postgres 모드의 Database.execute가 리터럴 %를 보존하고 ?만 자리표시자로 바꾸는지."""
    from psycopg._queries import PostgresQuery
    from psycopg.adapt import Transformer

    sent = []
    db = app_module.Database.__new__(app_module.Database)
    db.postgres = True
    db.conn = type("Conn", (), {"execute": lambda self, sql, params: sent.append((sql, params))})()
    db.execute("SELECT * FROM requests WHERE name LIKE '%a%' AND id = ?", (1,))
    q = PostgresQuery(Transformer())
    q.convert(*sent[0])
    assert q.query == b"SELECT * FROM requests WHERE name LIKE '%a%' AND id = $1"
