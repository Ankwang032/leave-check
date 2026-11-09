import calendar
import json
import os
import secrets
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("LEAVE_DB_PATH") or BASE_DIR / "leave.db")
SECRET_PATH = BASE_DIR / ".secret_key"
# 배포 환경(Vercel 등)에서는 Postgres 주소를 받고, 없으면 로컬 SQLite 파일을 쓴다
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
ON_VERCEL = bool(os.environ.get("VERCEL"))
KST = timezone(timedelta(hours=9))  # 서버 시간대(Vercel은 UTC)와 무관하게 한국 날짜로 판단한다

LEAVE_TYPES = ["휴가", "외박", "외출"]
VACATION_KINDS = ["연가", "보상", "포상", "위로"]
OUTING_ROUNDS = [1, 2]  # 외출/외박 차수 (0은 선택 안 함)
RANKS = ["이병", "일병", "상병", "병장", "하사", "중사", "상사",
         "준위", "소위", "중위", "대위"]
OUTING_MAX_NIGHTS = 6  # 외박 최대 박수 (외출/외박 대상 주 7일 안)
VACATION_LIMIT = 20  # 휴가 출타율 상한(%)
TOTAL_LIMIT = 35     # 휴가+외박+외출 출타율 상한(%)

if ON_VERCEL and not os.environ.get("ADMIN_PASSWORD"):
    raise RuntimeError("배포 환경에서는 ADMIN_PASSWORD 환경변수를 설정해야 합니다.")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "1234")

# 종합기간 기본값: 다음주 수~다다음주 화 외출/외박을 금주 화~다음주 수에, 다음 달 휴가를 이번 달 20~28일에 접수
DEFAULT_WINDOW = {
    "enabled": True,
    "outing_weekday": 2,        # 외출/외박 대상 주의 시작 요일 (월=0 … 일=6)
    "outing_open_before": 8,    # 대상 주 시작일 며칠 전부터 접수
    "outing_close_before": 0,   # 대상 주 시작일 며칠 전까지 접수
    "vacation_open_day": 20,    # 전 달 며칠부터 휴가 접수
    "vacation_close_day": 28,   # 전 달 며칠까지 휴가 접수
}
WEEKDAYS = "월화수목금토일"


def load_secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    if ON_VERCEL:  # 배포 환경은 파일을 저장할 수 없고 인스턴스마다 키가 달라지면 로그인이 풀린다
        raise RuntimeError("배포 환경에서는 SECRET_KEY 환경변수를 설정해야 합니다.")
    if not SECRET_PATH.exists():
        SECRET_PATH.write_text(secrets.token_hex(32))
    return SECRET_PATH.read_text()


app = Flask(__name__)
app.secret_key = load_secret_key()
app.config["SESSION_COOKIE_SECURE"] = ON_VERCEL
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"   # 다른 사이트에서 보낸 요청에는 로그인 쿠키를 싣지 않는다
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024     # 요청 본문 크기 제한 (신청 한 건은 1KB 남짓)
# 접속 코드를 한 번 입력하면 30일 동안 다시 묻지 않는다 (관리자 로그인은 ADMIN_SESSION_SECONDS 뒤 만료)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
ADMIN_SESSION_SECONDS = 12 * 60 * 60


def today():
    return datetime.now(KST).date()


def now():
    return datetime.now(KST).replace(tzinfo=None)


class Database:
    """로컬 SQLite와 배포용 Postgres를 같은 코드로 쓰기 위한 얇은 래퍼.
    SQL은 ? 자리표시자로 쓰고, Postgres에서는 %s로 바꿔 실행한다."""

    def __init__(self):
        self.postgres = bool(DATABASE_URL)
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            # prepare_threshold=None: Neon 연결 풀러(PgBouncer)에서 준비된 문장 충돌을 피한다
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if self.postgres:
            # psycopg에서 %는 자리표시자 문자라 리터럴 %는 %%로 바꾼 뒤 ?를 %s로 바꾼다.
            # SQL 문자열 안에 리터럴 ?는 쓰지 않는다 (자리표시자로 바뀜).
            sql = sql.replace("%", "%%").replace("?", "%s")
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def get_db():
    if "db" not in g:
        g.db = Database()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# requests 테이블 컬럼 (id 제외). SQLite와 Postgres가 같은 목록을 쓰고,
# 이미 있는 DB에 없는 컬럼은 서버가 시작할 때 추가한다. 컬럼은 여기에만 추가하면 된다.
REQUEST_COLUMNS = [
    ("rank", "TEXT NOT NULL"),
    ("name", "TEXT NOT NULL"),
    ("password_hash", "TEXT NOT NULL DEFAULT ''"),
    ("type", "TEXT NOT NULL"),
    ("vacation_days", "TEXT NOT NULL DEFAULT ''"),
    ("reason", "TEXT NOT NULL DEFAULT ''"),
    ("reward", "INTEGER NOT NULL DEFAULT 0"),
    ("official", "INTEGER NOT NULL DEFAULT 0"),
    ("outing_round", "INTEGER NOT NULL DEFAULT 0"),
    ("start_date", "TEXT NOT NULL"),
    ("end_date", "TEXT NOT NULL"),
    ("created_at", "TEXT NOT NULL"),
]
# 처음부터 있던 컬럼 (나중에 추가할 때 기본값이 없어도 되는 것)
BASE_COLUMNS = {"rank", "name", "type", "start_date", "end_date", "created_at"}
OTHER_TABLES = [
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, fails INTEGER NOT NULL, reset_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS requests_dates ON requests (start_date, end_date)",
]


def create_requests_sql(id_ddl):
    columns = ",\n    ".join(f"{name} {ddl}" for name, ddl in REQUEST_COLUMNS)
    return f"CREATE TABLE IF NOT EXISTS requests (\n    id {id_ddl},\n    {columns}\n)"


def init_db():
    if DATABASE_URL:
        db = Database()
        try:
            # 여러 서버 인스턴스가 동시에 시작해도 테이블 생성이 겹치지 않게 잠근다
            db.execute("SELECT pg_advisory_xact_lock(20261101)")
            db.execute(create_requests_sql("SERIAL PRIMARY KEY"))
            for name, ddl in REQUEST_COLUMNS:
                if name not in BASE_COLUMNS:
                    db.execute(f"ALTER TABLE requests ADD COLUMN IF NOT EXISTS {name} {ddl}")
            for sql in OTHER_TABLES:
                db.execute(sql)
            db.commit()
        finally:
            db.close()
        return
    with sqlite3.connect(DB_PATH) as db:
        db.execute(create_requests_sql("INTEGER PRIMARY KEY AUTOINCREMENT"))
        # 나중에 추가된 컬럼이 없는 이전 DB 보정
        columns = [r[1] for r in db.execute("PRAGMA table_info(requests)")]
        for name, ddl in REQUEST_COLUMNS:
            if name not in columns and name not in BASE_COLUMNS:
                db.execute(f"ALTER TABLE requests ADD COLUMN {name} {ddl}")
        for sql in OTHER_TABLES:
            db.execute(sql)
        # 휴가 종류를 하나만 저장하던 이전 신청은 전체 일수를 그 종류로 옮긴다
        if "vacation_kind" in columns:
            for r in db.execute("SELECT id, vacation_kind, start_date, end_date FROM requests "
                                "WHERE type = '휴가' AND vacation_kind != '' AND vacation_days = ''").fetchall():
                days = (date.fromisoformat(r[3]) - date.fromisoformat(r[2])).days + 1
                db.execute("UPDATE requests SET vacation_days = ? WHERE id = ?",
                           (json.dumps({r[1]: days}, ensure_ascii=False), r[0]))


# ── 관리자 비밀번호 ─────────────────────────────────────
# 관리자 화면에서 바꾼 비밀번호는 settings 테이블 'admin' 키에 해시로 저장한다.
# 아직 바꾸지 않았으면 환경변수 ADMIN_PASSWORD(초기 비밀번호)를 쓴다.
# 바꿀 때마다 version이 바뀌어, 다른 기기의 관리자 로그인은 모두 풀린다.

def get_admin_setting():
    if "admin_setting" not in g:
        row = get_db().execute("SELECT value FROM settings WHERE key = 'admin'").fetchone()
        g.admin_setting = json.loads(row["value"]) if row else None
    return g.admin_setting


def check_admin_password(password):
    admin = get_admin_setting()
    if admin:
        return check_password_hash(admin["hash"], password)
    return secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode())


def admin_version():
    admin = get_admin_setting()
    return admin["version"] if admin else None


def is_admin():
    # 세션에는 관리자 로그인 만료 시각(초)과 로그인할 때의 비밀번호 version을 저장한다
    until = session.get("admin")
    if not (isinstance(until, (int, float)) and not isinstance(until, bool) and until > time.time()):
        return False
    return session.get("admin_version") == admin_version()


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return jsonify(error="관리자 로그인이 필요합니다."), 401
        return view(*args, **kwargs)
    return wrapper


# ── 접속 코드 ───────────────────────────────────────────
# 관리자가 접속 코드를 정하면, 코드를 입력한 사람(또는 관리자)만 신청 화면과 API를 쓸 수 있다.
# 코드는 해시로 저장하고, 바꿀 때마다 version이 바뀌어 이전에 입력한 사람도 다시 입력해야 한다.

def get_access():
    """접속 코드 설정. 한 요청 안에서는 g에 캐시한다 (바꾸면 update_access가 갱신)."""
    if "access" not in g:
        row = get_db().execute("SELECT value FROM settings WHERE key = 'access'").fetchone()
        g.access = json.loads(row["value"]) if row else None
    return g.access


def has_access():
    """접속 코드가 없거나, 지금 코드를 입력한 세션인지. 관리자도 예외 없이 코드를 입력해야 한다."""
    access = get_access()
    return access is None or session.get("access") == access["version"]


# ── 입력·요청 공통 검사 ─────────────────────────────────

def json_body():
    """JSON 객체 본문만 받는다. 배열·문자열 등 다른 모양이면 빈 dict로 본다."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def text_field(data, key):
    """문자열 칸만 받아 앞뒤 공백을 지운다. 문자열이 아니면 None."""
    value = data.get(key)
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else None


def has_control_chars(text):
    """화면을 속일 수 있는 제어문자(줄바꿈, NUL, 글자 방향 뒤집기, 폭 없는 문자 등)가 있는지."""
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in text)


# 연속 오입력 제한: 키마다 window_min분 안에 limit번 틀리면 그 시간이 끝날 때까지 막는다.
# 서버리스(Vercel)는 요청마다 다른 인스턴스가 받을 수 있어 DB에 기록한다.
LOGIN_RULES = {
    "admin": (5, 15),    # 관리자 로그인: 15분에 5번
    "access": (10, 10),  # 접속 코드: 10분에 10번 (같은 와이파이를 쓰는 여러 명을 고려)
    "cancel": (10, 10),  # 신청 취소 비밀번호: IP당 10분에 10번
    "cancel-id": (10, 30),  # 같은 신청에 대해서는 여러 IP를 합쳐 30분에 10번
}


def client_ip():
    if ON_VERCEL:  # Vercel이 넣어 주는 실제 접속 IP (클라이언트가 보낸 값은 덮어쓴다)
        return (request.headers.get("x-real-ip")
                or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or request.remote_addr or "-")
    return request.remote_addr or "-"


def attempt_keys(kind, target=None):
    keys = [(f"{kind}:{client_ip()}", *LOGIN_RULES[kind])]
    if target is not None:
        keys.append((f"{kind}-id:{target}", *LOGIN_RULES[f"{kind}-id"]))
    return keys


def locked_minutes(keys):
    """막혀 있으면 남은 분, 아니면 0."""
    db, current = get_db(), now().isoformat(timespec="seconds")
    for key, limit, _ in keys:
        row = db.execute("SELECT fails, reset_at FROM attempts WHERE key = ?", (key,)).fetchone()
        if row and row["reset_at"] > current and row["fails"] >= limit:
            left = datetime.fromisoformat(row["reset_at"]) - now()
            return max(1, -(-int(left.total_seconds()) // 60))
    return 0


def record_failure(keys):
    db, current = get_db(), now()
    stamp = current.isoformat(timespec="seconds")
    db.execute("DELETE FROM attempts WHERE reset_at <= ?", (stamp,))
    for key, _, window in keys:
        reset_at = (current + timedelta(minutes=window)).isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO attempts (key, fails, reset_at) VALUES (?, 1, ?) "
            "ON CONFLICT (key) DO UPDATE SET "
            "fails = CASE WHEN attempts.reset_at <= ? THEN 1 ELSE attempts.fails + 1 END, "
            "reset_at = CASE WHEN attempts.reset_at <= ? THEN excluded.reset_at ELSE attempts.reset_at END",
            (key, reset_at, stamp, stamp))
    db.commit()


def clear_failures(keys):
    db = get_db()
    for key, _, _ in keys:
        db.execute("DELETE FROM attempts WHERE key = ?", (key,))
    db.commit()


def too_many(minutes):
    return jsonify(error=f"잘못 입력한 횟수가 너무 많습니다. {minutes}분 뒤에 다시 시도하세요."), 429


@app.before_request
def block_cross_site_writes():
    """다른 사이트가 사용자의 브라우저를 이용해 보내는 변경 요청(CSRF)을 막는다.
    브라우저가 붙이는 Sec-Fetch-Site는 페이지의 스크립트가 바꿀 수 없다."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and \
            request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify(error="다른 사이트에서 보낸 요청은 처리하지 않습니다."), 403
    g.csp_nonce = secrets.token_urlsafe(16)
    return None


@app.context_processor
def inject_nonce():
    return {"csp_nonce": g.get("csp_nonce", "")}


# 접속 코드 없이 열 수 있는 것: 코드 입력, 정적 파일, robots.txt, 로그아웃.
# 관리자 화면과 관리자 로그인도 접속 코드를 먼저 입력해야 열린다.
ACCESS_FREE = {"static", "access_page", "enter_access", "admin_logout", "robots"}


@app.before_request
def require_access_code():
    if request.endpoint in ACCESS_FREE or has_access():
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="접속 코드를 입력하세요."), 401
    return redirect(url_for("access_page", next=request.full_path.rstrip("?")))


@app.after_request
def security_headers(response):
    h = response.headers
    h["X-Robots-Tag"] = "noindex, nofollow"  # 검색엔진에 노출하지 않는다
    # 이 서버가 넣은 스크립트(nonce)만 실행하고, 다른 사이트가 이 화면을 틀(iframe)에 넣지 못하게 한다
    h["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'self' 'nonce-{g.get('csp_nonce', '')}'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; font-src 'self' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'")
    h["X-Frame-Options"] = "DENY"
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "same-origin"
    if ON_VERCEL:
        h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    if request.endpoint != "static":  # 명단이 담긴 응답은 브라우저·중간 서버에 저장하지 않는다
        h["Cache-Control"] = "no-store"
    return response


@app.get("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}


def safe_next(url):
    """다른 사이트로 넘어가지 않도록 이 사이트 안의 경로만 허용한다."""
    if (url and url.startswith("/") and not url.startswith("//") and "\\" not in url
            and url.isprintable() and " " not in url):
        return url
    return "/"


@app.get("/access")
def access_page():
    next_url = safe_next(request.args.get("next"))
    if has_access():
        return redirect(next_url)
    return render_template("access.html", next_url=next_url)


@app.post("/api/access")
def enter_access():
    access = get_access()
    if not access:
        return "", 204
    keys = attempt_keys("access")
    if minutes := locked_minutes(keys):
        return too_many(minutes)
    code = json_body().get("code")
    if not isinstance(code, str) or not check_password_hash(access["hash"], code):
        record_failure(keys)
        return jsonify(error="접속 코드가 틀렸습니다."), 401
    clear_failures(keys)
    session["access"] = access["version"]
    session.permanent = True
    return "", 204


@app.put("/api/settings/access")
@admin_required
def update_access():
    code = json_body().get("code")
    if not isinstance(code, str):
        return jsonify(error="접속 코드를 입력하세요."), 400
    db = get_db()
    if code == "":  # 빈 코드를 보내면 접속 코드를 해제한다
        db.execute("DELETE FROM settings WHERE key = 'access'")
        db.commit()
        g.access = None
        return jsonify(enabled=False)
    if not 6 <= len(code) <= 30:
        return jsonify(error="접속 코드를 6~30자로 입력하세요."), 400
    access = {"hash": generate_password_hash(code), "version": secrets.token_hex(8),
              "changed_at": now().isoformat(timespec="seconds")}
    db.execute("INSERT INTO settings (key, value) VALUES ('access', ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(access),))
    db.commit()
    g.access = access
    session["access"] = access["version"]  # 바꾼 관리자는 새 코드로 접속을 유지한다
    session.permanent = True
    return jsonify(enabled=True, changed_at=access["changed_at"])


def get_strength():
    row = get_db().execute("SELECT value FROM settings WHERE key = 'strength'").fetchone()
    return int(row["value"]) if row else 0


def limits(strength):
    return {
        "strength": strength,
        "vacation_max": strength * VACATION_LIMIT // 100,
        "total_max": strength * TOTAL_LIMIT // 100,
        "vacation_limit": VACATION_LIMIT,
        "total_limit": TOTAL_LIMIT,
    }


def get_window():
    row = get_db().execute("SELECT value FROM settings WHERE key = 'window'").fetchone()
    return DEFAULT_WINDOW | (json.loads(row["value"]) if row else {})


def label(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def month_day(year, month, day):
    """그 달에 없는 날짜(예: 2월 30일)는 말일로 맞춘다."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def outing_period(day, w):
    """day가 속한 외출/외박 대상 주 (시작일, 종료일, 접수 시작일, 접수 마감일)."""
    start = day - timedelta(days=(day.weekday() - w["outing_weekday"]) % 7)
    return (start, start + timedelta(days=6),
            start - timedelta(days=w["outing_open_before"]),
            start - timedelta(days=w["outing_close_before"]))


def vacation_period(day, w):
    """day가 속한 달의 휴가 (시작일, 종료일, 접수 시작일, 접수 마감일)."""
    first = day.replace(day=1)
    prev = add_months(first, -1)
    return (first, month_day(first.year, first.month, 31),
            month_day(prev.year, prev.month, w["vacation_open_day"]),
            month_day(prev.year, prev.month, w["vacation_close_day"]))


def period_dict(p):
    return dict(zip(("from", "to", "open", "close"), (d.isoformat() for d in p)))


def window_status(today, w):
    """오늘 접수 중인 대상 기간과 다음 접수 일정."""
    outing = []
    week = outing_period(today, w)[0]
    for k in range(-1, w["outing_open_before"] // 7 + 3):
        p = outing_period(week + timedelta(weeks=k), w)
        if p[2] <= today <= p[3]:
            outing.append(p)
    k = 0
    while (next_outing := outing_period(week + timedelta(weeks=k), w))[2] <= today:
        k += 1

    vacation = [p for p in [vacation_period(add_months(today, 1), w)] if p[2] <= today <= p[3]]
    next_vacation = vacation_period(add_months(today, 1), w)
    if next_vacation[2] <= today:
        next_vacation = vacation_period(add_months(today, 2), w)

    return {
        "outing_open": [period_dict(p) for p in outing],
        "vacation_open": [period_dict(p) for p in vacation],
        "next_outing": period_dict(next_outing),
        "next_vacation": period_dict(next_vacation),
    }


def window_error(req, today):
    """신청 시작일이 오늘 접수 가능한 종합기간이 아니면 오류 문구를 반환한다."""
    w = get_window()
    if not w["enabled"]:
        return None
    start = req["start_date"]
    if req["type"] == "휴가":
        _, _, open_, close = vacation_period(start, w)
        what = f"{start.month}월 휴가"
    else:
        week_start, week_end, open_, close = outing_period(start, w)
        what = f"{label(week_start)}~{label(week_end)} 외출/외박"
    if open_ <= today <= close:
        return None
    state = "아직 종합 전입니다" if today < open_ else "종합이 마감되었습니다"
    return f"{what} 신청은 {label(open_)}~{label(close)}에 종합합니다. ({state})"


def public_row(row):
    data = dict(row)
    data.pop("password_hash", None)
    data.pop("service_number", None)  # 이전 DB에 남아 있을 수 있는 컬럼
    if not is_admin():
        data.pop("reason", None)  # 세부사유는 관리자만 본다
    data.pop("vacation_kind", None)  # vacation_days로 옮겨진 이전 컬럼
    data["vacation_days"] = json.loads(data["vacation_days"] or "{}")
    data["reward"], data["official"] = bool(data["reward"]), bool(data["official"])
    return data


def detail_label(row):
    """휴가 종류별 일수(예: 연가 3·포상 2) 또는 외출/외박의 차수·포상·공용(예: 1차·포상) 표시."""
    if row["type"] == "휴가":
        days = json.loads(row["vacation_days"] or "{}")
        return "·".join(f"{k} {days[k]}" for k in VACATION_KINDS if k in days)
    parts = [f"{row['outing_round']}차"] if row["outing_round"] else []
    parts += [t for t, on in [("포상", row["reward"]), ("공용", row["official"])] if on]
    return "·".join(parts)


def outing_detail_label(reward, official):
    """외출/외박 세부사항 칸 이름. 포상만이면 '포상 내용', 공용이 들어가면 '공용 세부사항'·'포상·공용 세부사항'.
    신청 화면(index.html detailLabel)과 같은 이름을 쓴다."""
    if not official:
        return "포상 내용"
    return "포상·공용 세부사항" if reward else "공용 세부사항"


def clean_reason(data):
    """세부사유·세부사항: 줄바꿈은 공백으로 바꾸고, 다른 제어문자가 있으면 None."""
    reason = text_field(data, "reason")
    if reason is None:
        return None
    reason = " ".join(reason.splitlines())
    return None if has_control_chars(reason) else reason


def parse_request(data):
    """신청 데이터를 검증해 (값 dict, 오류 메시지)를 반환한다."""
    rank, name = text_field(data, "rank"), text_field(data, "name")
    leave_type = data.get("type")

    if rank not in RANKS:
        return None, "계급을 선택하세요."
    if not name or len(name) > 20:
        return None, "이름을 1~20자로 입력하세요."
    if has_control_chars(name):
        return None, "이름에 쓸 수 없는 문자가 들어 있습니다."
    if leave_type not in LEAVE_TYPES:
        return None, "출타 종류를 선택하세요."
    try:
        start = date.fromisoformat(text_field(data, "start_date") or "")
        end = date.fromisoformat(text_field(data, "end_date") or "")
    except ValueError:
        return None, "날짜를 선택하세요."
    if end < start:
        return None, "종료일이 시작일보다 빠릅니다."
    if leave_type == "외출" and start != end:
        return None, "외출은 하루만 선택할 수 있습니다."
    if leave_type == "외박" and not 1 <= (end - start).days <= OUTING_MAX_NIGHTS:
        return None, f"외박은 1박부터 {OUTING_MAX_NIGHTS}박까지 신청할 수 있습니다. 종료일을 확인하세요."

    # 휴가는 종류별 일수와 세부사유, 외출/외박은 차수·포상·공용과 포상·공용 세부사항을 저장한다
    # (세부사항은 휴가 세부사유와 같은 reason 칸에 저장해 관리자만 본다)
    vacation_days, reason = {}, ""
    reward = official = False
    outing_round = 0
    if leave_type == "휴가":
        raw = data.get("vacation_days")
        if not isinstance(raw, dict) or any(k not in VACATION_KINDS for k in raw):
            return None, "휴가 종류별 일수를 입력하세요."
        for kind in VACATION_KINDS:
            n = raw.get(kind) or 0
            if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 100:
                return None, f"{kind} 일수를 0 이상의 숫자로 입력하세요."
            if n:
                vacation_days[kind] = n
        total_days = (end - start).days + 1
        if sum(vacation_days.values()) != total_days:
            return None, (f"휴가 일수 합계({sum(vacation_days.values())}일)가 "
                          f"신청 기간({total_days}일)과 맞지 않습니다.")
        reason = clean_reason(data)
        if reason is None:
            return None, "세부사유에 쓸 수 없는 문자가 들어 있습니다."
        if len(reason) > 100:
            return None, "세부사유는 100자 이내로 입력하세요."
    else:
        reward, official = data.get("reward") is True, data.get("official") is True
        outing_round = data.get("outing_round") or 0
        if isinstance(outing_round, bool) or outing_round not in [0, *OUTING_ROUNDS]:
            return None, "차수를 선택하세요."
        if reward or official:
            what = outing_detail_label(reward, official)
            reason = clean_reason(data)
            if reason is None:
                return None, f"{what}에 쓸 수 없는 문자가 들어 있습니다."
            if len(reason) > 100:
                return None, f"{what}은 100자 이내로 입력하세요."

    return {"rank": rank, "name": name, "type": leave_type,
            "vacation_days": json.dumps(vacation_days, ensure_ascii=False), "reason": reason,
            "reward": int(reward), "official": int(official), "outing_round": outing_round,
            "start_date": start, "end_date": end}, None


def capacity_warnings(req, exclude_id=None):
    """이 신청이 들어가면 출타율 상한을 넘는 날짜를 경고 문구 목록으로 반환한다.
    공용 외출/외박은 출타율에 포함하지 않는다."""
    lim = limits(get_strength())
    if not lim["strength"] or req["official"]:
        return []
    start, end = req["start_date"].isoformat(), req["end_date"].isoformat()
    rows = get_db().execute(
        "SELECT start_date, end_date, type FROM requests "
        "WHERE start_date <= ? AND end_date >= ? AND id != ? AND official = 0",
        (end, start, exclude_id or -1),
    ).fetchall()

    vacation_over, total_over = [], []
    day = req["start_date"]
    while day <= req["end_date"]:
        ds = day.isoformat()
        on_day = [r for r in rows if r["start_date"] <= ds <= r["end_date"]]
        vacation = sum(r["type"] == "휴가" for r in on_day) + (req["type"] == "휴가")
        label = f"{day.month}/{day.day}"
        if req["type"] == "휴가" and vacation > lim["vacation_max"]:
            vacation_over.append(label)
        if len(on_day) + 1 > lim["total_max"]:
            total_over.append(label)
        day += timedelta(days=1)

    warnings = []
    if vacation_over:
        warnings.append(f"{', '.join(vacation_over)} 휴가 출타율 초과 "
                        f"(최대 {lim['vacation_max']}명, {VACATION_LIMIT}%)")
    if total_over:
        warnings.append(f"{', '.join(total_over)} 전체 출타율 초과 "
                        f"(최대 {lim['total_max']}명, {TOTAL_LIMIT}%)")
    return warnings


def today_tile():
    """상단 바 일력에 쓰는 오늘(한국 날짜)."""
    t = today()
    return {"month": t.month, "day": t.day, "dow": WEEKDAYS[t.weekday()], "iso": t.isoformat()}


@app.get("/")
def index():
    return render_template("index.html", leave_types=LEAVE_TYPES, ranks=RANKS, vacation_kinds=VACATION_KINDS,
                           outing_rounds=OUTING_ROUNDS, today=today_tile())


@app.get("/admin")
def admin_page():
    logged_in = is_admin()
    access = get_access() if logged_in else None
    admin = get_admin_setting() if logged_in else None
    return render_template("admin.html", leave_types=LEAVE_TYPES, ranks=RANKS,
                           vacation_kinds=VACATION_KINDS, outing_rounds=OUTING_ROUNDS, logged_in=logged_in,
                           access_changed_at=access and access["changed_at"].replace("T", " "),
                           admin_changed_at=admin and admin["changed_at"].replace("T", " "), today=today_tile())


PRINT_KINDS = {"all": ("전체", LEAVE_TYPES), "vacation": ("휴가", ["휴가"]), "outing": ("외출/외박", ["외박", "외출"])}


@app.get("/admin/print")
def admin_print():
    if not is_admin():
        return redirect("/admin")
    kind = request.args.get("kind", "all")
    title, types = PRINT_KINDS.get(kind, PRINT_KINDS["all"])
    try:
        start = date.fromisoformat(request.args.get("from", ""))
        end = date.fromisoformat(request.args.get("to", ""))
    except ValueError:
        return "기간을 올바르게 선택하세요.", 400
    if end < start:
        start, end = end, start

    rows = get_db().execute(
        f"SELECT * FROM requests WHERE start_date <= ? AND end_date >= ? "
        f"AND type IN ({','.join('?' * len(types))}) ORDER BY start_date, end_date, id",
        (end.isoformat(), start.isoformat(), *types),
    ).fetchall()
    items = []
    for r in rows:
        s, e = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
        items.append({
            "type": r["type"], "detail": detail_label(r), "reason": r["reason"],
            "rank": r["rank"], "name": r["name"],
            "period": label(s) if s == e else f"{label(s)} ~ {label(e)}",
            "days": "당일" if s == e else f"{(e - s).days}박 {(e - s).days + 1}일",
        })
    counts = {t: sum(i["type"] == t for i in items) for t in types}
    return render_template(
        "print.html", title=title, items=items, counts=counts,
        start=label(start), end=label(end), year=start.year,
        printed=now().strftime("%Y.%m.%d %H:%M"),
    )


@app.get("/api/settings")
def get_settings():
    w = get_window()
    return jsonify(limits(get_strength()) | {"window": w, "status": window_status(today(), w)})


@app.put("/api/settings/window")
@admin_required
def update_window():
    data = json_body()
    w = get_window()
    for key in DEFAULT_WINDOW:
        if key in data:
            w[key] = data[key]

    def is_int(v, lo, hi):
        return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi

    if not isinstance(w["enabled"], bool):
        return jsonify(error="종합기간 적용 여부가 올바르지 않습니다."), 400
    if not is_int(w["outing_weekday"], 0, 6):
        return jsonify(error="요일을 선택하세요."), 400
    if not is_int(w["outing_open_before"], 0, 60) or not is_int(w["outing_close_before"], 0, 60):
        return jsonify(error="외출/외박 접수일은 0~60일 전으로 입력하세요."), 400
    if w["outing_close_before"] > w["outing_open_before"]:
        return jsonify(error="외출/외박 접수 마감이 접수 시작보다 빠릅니다."), 400
    if not is_int(w["vacation_open_day"], 1, 31) or not is_int(w["vacation_close_day"], 1, 31):
        return jsonify(error="휴가 접수일은 1~31일로 입력하세요."), 400
    if w["vacation_close_day"] < w["vacation_open_day"]:
        return jsonify(error="휴가 접수 마감일이 시작일보다 빠릅니다."), 400

    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES ('window', ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(w),))
    db.commit()
    return jsonify(window=w, status=window_status(today(), w))


@app.put("/api/settings")
@admin_required
def update_settings():
    strength = json_body().get("strength")
    if not isinstance(strength, int) or isinstance(strength, bool) or not 0 <= strength <= 10000:
        return jsonify(error="총원을 0~10000 사이 숫자로 입력하세요."), 400
    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES ('strength', ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(strength),))
    db.commit()
    return jsonify(limits(strength))


LIST_MAX_DAYS = 62  # 신청 목록 한 번에 조회할 수 있는 최대 일수 (화면은 한 달·한 주 단위로 조회)


@app.get("/api/requests")
def list_requests():
    # ?from=YYYY-MM-DD&to=YYYY-MM-DD 기간과 겹치는 신청만 반환 (기간은 필수, 최대 LIST_MAX_DAYS일)
    try:
        start = date.fromisoformat(request.args.get("from", ""))
        end = date.fromisoformat(request.args.get("to", ""))
    except ValueError:
        return jsonify(error="조회 기간(from, to)을 YYYY-MM-DD로 입력하세요."), 400
    if not 0 <= (end - start).days < LIST_MAX_DAYS:
        return jsonify(error=f"조회 기간은 {LIST_MAX_DAYS}일 이내로 입력하세요."), 400
    rows = get_db().execute(
        "SELECT * FROM requests WHERE start_date <= ? AND end_date >= ? "
        "ORDER BY start_date, id",
        (end.isoformat(), start.isoformat()),
    ).fetchall()
    return jsonify([public_row(r) for r in rows])


@app.post("/api/requests")
def create_request():
    data = json_body()
    req, error = parse_request(data)
    if error:
        return jsonify(error=error), 400
    password = data.get("password")
    if not isinstance(password, str) or not 4 <= len(password) <= 30:
        return jsonify(error="비밀번호를 4~30자로 입력하세요."), 400
    # 신청 화면은 관리자로 로그인돼 있어도 같은 규칙을 적용한다
    if req["start_date"] < today():
        return jsonify(error="지난 날짜는 신청할 수 없습니다."), 400
    error = window_error(req, today())
    if error:
        return jsonify(error=error), 400
    # 출타율을 넘어도 신청은 받고, 초과 사실을 함께 알려준다
    warnings = capacity_warnings(req)

    db = get_db()
    cur = db.execute(
        "INSERT INTO requests (rank, name, password_hash, type, vacation_days, reason, reward, official, "
        "outing_round, start_date, end_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (req["rank"], req["name"], generate_password_hash(password), req["type"],
         req["vacation_days"], req["reason"], req["reward"], req["official"], req["outing_round"],
         req["start_date"].isoformat(), req["end_date"].isoformat(),
         now().isoformat(timespec="seconds")),
    )
    new_id = cur.fetchone()["id"]
    db.commit()
    row = db.execute("SELECT * FROM requests WHERE id = ?", (new_id,)).fetchone()
    return jsonify(public_row(row) | {"warnings": warnings}), 201


@app.put("/api/requests/<int:request_id>")
@admin_required
def update_request(request_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM requests WHERE id = ?", (request_id,)).fetchone():
        return jsonify(error="신청을 찾을 수 없습니다."), 404
    req, error = parse_request(json_body())
    if error:
        return jsonify(error=error), 400
    warnings = capacity_warnings(req, exclude_id=request_id)

    db.execute(
        "UPDATE requests SET rank = ?, name = ?, type = ?, vacation_days = ?, reason = ?, "
        "reward = ?, official = ?, outing_round = ?, start_date = ?, end_date = ? WHERE id = ?",
        (req["rank"], req["name"], req["type"], req["vacation_days"], req["reason"],
         req["reward"], req["official"], req["outing_round"], req["start_date"].isoformat(), req["end_date"].isoformat(), request_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    return jsonify(public_row(row) | {"warnings": warnings})


@app.delete("/api/requests/<int:request_id>")
def delete_request(request_id):
    # 신청 화면의 취소는 관리자로 로그인돼 있어도 항상 비밀번호를 확인한다
    db = get_db()
    row = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if not row:
        return jsonify(error="신청을 찾을 수 없습니다."), 404
    keys = attempt_keys("cancel", target=request_id)
    if minutes := locked_minutes(keys):
        return too_many(minutes)
    password = json_body().get("password")
    if (not isinstance(password, str) or not row["password_hash"]
            or not check_password_hash(row["password_hash"], password)):
        record_failure(keys)
        return jsonify(error="비밀번호가 일치하지 않습니다."), 403
    clear_failures(keys)
    db.execute("DELETE FROM requests WHERE id = ?", (request_id,))
    db.commit()
    return "", 204


@app.delete("/api/admin/requests/<int:request_id>")
@admin_required
def admin_delete_request(request_id):
    db = get_db()
    db.execute("DELETE FROM requests WHERE id = ?", (request_id,))
    db.commit()
    return "", 204


@app.post("/api/admin/login")
def admin_login():
    keys = attempt_keys("admin")
    if minutes := locked_minutes(keys):
        return too_many(minutes)
    password = json_body().get("password")
    if not isinstance(password, str) or not check_admin_password(password):
        record_failure(keys)
        return jsonify(error="비밀번호가 틀렸습니다."), 401
    clear_failures(keys)
    session["admin"] = time.time() + ADMIN_SESSION_SECONDS
    session["admin_version"] = admin_version()
    return "", 204


@app.put("/api/settings/admin-password")
@admin_required
def change_admin_password():
    data = json_body()
    current, new = data.get("current"), data.get("new")
    keys = attempt_keys("admin")
    if minutes := locked_minutes(keys):
        return too_many(minutes)
    if not isinstance(current, str) or not check_admin_password(current):
        record_failure(keys)
        return jsonify(error="현재 비밀번호가 틀렸습니다."), 403
    if not isinstance(new, str) or not 8 <= len(new) <= 64:
        return jsonify(error="새 비밀번호를 8~64자로 입력하세요."), 400
    admin = {"hash": generate_password_hash(new), "version": secrets.token_hex(8),
             "changed_at": now().isoformat(timespec="seconds")}
    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES ('admin', ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(admin),))
    db.commit()
    g.admin_setting = admin
    session["admin_version"] = admin["version"]  # 바꾼 사람은 로그인을 유지한다
    return jsonify(changed_at=admin["changed_at"])


@app.post("/api/admin/logout")
def admin_logout():
    session.pop("admin", None)
    session.pop("admin_version", None)
    return "", 204


init_db()

if __name__ == "__main__":
    if "ADMIN_PASSWORD" not in os.environ:
        print("⚠ ADMIN_PASSWORD가 설정되지 않아 기본 비밀번호 '1234'를 사용합니다.")
    # 디버그 모드(코드 자동 반영·오류 화면)는 FLASK_DEBUG=1일 때만 켠다.
    # 디버거 화면은 서버에서 코드를 실행할 수 있어 외부에 열리면 위험하다.
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
