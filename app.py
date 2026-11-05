import calendar
import json
import os
import secrets
import sqlite3
import time
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
            sql = sql.replace("?", "%s")
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


POSTGRES_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS requests (
        id SERIAL PRIMARY KEY,
        rank TEXT NOT NULL,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL DEFAULT '',
        type TEXT NOT NULL,
        vacation_days TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        reward INTEGER NOT NULL DEFAULT 0,
        official INTEGER NOT NULL DEFAULT 0,
        outing_round INTEGER NOT NULL DEFAULT 0,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "ALTER TABLE requests ADD COLUMN IF NOT EXISTS outing_round INTEGER NOT NULL DEFAULT 0",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS requests_dates ON requests (start_date, end_date)",
]


def init_db():
    if DATABASE_URL:
        db = Database()
        try:
            # 여러 서버 인스턴스가 동시에 시작해도 테이블 생성이 겹치지 않게 잠근다
            db.execute("SELECT pg_advisory_xact_lock(20261101)")
            for sql in POSTGRES_SCHEMA:
                db.execute(sql)
            db.commit()
        finally:
            db.close()
        return
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rank TEXT NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL DEFAULT '',
                type TEXT NOT NULL,
                vacation_days TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                reward INTEGER NOT NULL DEFAULT 0,
                official INTEGER NOT NULL DEFAULT 0,
                outing_round INTEGER NOT NULL DEFAULT 0,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # 나중에 추가된 컬럼이 없는 이전 DB 보정
        columns = [r[1] for r in db.execute("PRAGMA table_info(requests)")]
        for column, ddl in [("password_hash", "TEXT NOT NULL DEFAULT ''"),
                            ("vacation_days", "TEXT NOT NULL DEFAULT ''"),
                            ("reason", "TEXT NOT NULL DEFAULT ''"),
                            ("reward", "INTEGER NOT NULL DEFAULT 0"),
                            ("official", "INTEGER NOT NULL DEFAULT 0"),
                            ("outing_round", "INTEGER NOT NULL DEFAULT 0")]:
            if column not in columns:
                db.execute(f"ALTER TABLE requests ADD COLUMN {column} {ddl}")
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
    row = get_db().execute("SELECT value FROM settings WHERE key = 'access'").fetchone()
    return json.loads(row["value"]) if row else None


def has_access():
    access = get_access()
    return access is None or is_admin() or session.get("access") == access["version"]


# 접속 코드 없이 열 수 있는 화면: 코드 입력, 관리자 로그인, 정적 파일
ACCESS_FREE = {"static", "access_page", "enter_access", "admin_page", "admin_login", "admin_logout", "robots"}


@app.before_request
def require_access_code():
    if request.endpoint in ACCESS_FREE or has_access():
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="접속 코드를 입력하세요."), 401
    return redirect(url_for("access_page", next=request.full_path.rstrip("?")))


@app.after_request
def no_index(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"  # 검색엔진에 노출하지 않는다
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
    code = (request.get_json(silent=True) or {}).get("code") or ""
    if access and not check_password_hash(access["hash"], code):
        return jsonify(error="접속 코드가 틀렸습니다."), 401
    if access:
        session["access"] = access["version"]
        session.permanent = True
    return "", 204


@app.put("/api/settings/access")
@admin_required
def update_access():
    code = (request.get_json(silent=True) or {}).get("code")
    if not isinstance(code, str):
        return jsonify(error="접속 코드를 입력하세요."), 400
    db = get_db()
    if code == "":  # 빈 코드를 보내면 접속 코드를 해제한다
        db.execute("DELETE FROM settings WHERE key = 'access'")
        db.commit()
        return jsonify(enabled=False)
    if not 6 <= len(code) <= 30:
        return jsonify(error="접속 코드를 6~30자로 입력하세요."), 400
    access = {"hash": generate_password_hash(code), "version": secrets.token_hex(8),
              "changed_at": now().isoformat(timespec="seconds")}
    db.execute("INSERT INTO settings (key, value) VALUES ('access', ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(access),))
    db.commit()
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


def parse_request(data):
    """신청 데이터를 검증해 (값 dict, 오류 메시지)를 반환한다."""
    rank = (data.get("rank") or "").strip()
    name = (data.get("name") or "").strip()
    leave_type = data.get("type")

    if rank not in RANKS:
        return None, "계급을 선택하세요."
    if not name or len(name) > 20:
        return None, "이름을 1~20자로 입력하세요."
    if leave_type not in LEAVE_TYPES:
        return None, "출타 종류를 선택하세요."
    try:
        start = date.fromisoformat(data.get("start_date", ""))
        end = date.fromisoformat(data.get("end_date", ""))
    except ValueError:
        return None, "날짜를 선택하세요."
    if end < start:
        return None, "종료일이 시작일보다 빠릅니다."
    if leave_type == "외출" and start != end:
        return None, "외출은 하루만 선택할 수 있습니다."

    # 휴가는 종류별 일수와 세부사유, 외출/외박은 차수·포상·공용과 포상 내용을 저장한다
    # (포상 내용은 세부사유와 같은 reason 칸에 저장해 관리자만 본다)
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
        reason = (data.get("reason") or "").strip()
        if len(reason) > 100:
            return None, "세부사유는 100자 이내로 입력하세요."
    else:
        reward, official = data.get("reward") is True, data.get("official") is True
        outing_round = data.get("outing_round") or 0
        if isinstance(outing_round, bool) or outing_round not in [0, *OUTING_ROUNDS]:
            return None, "차수를 선택하세요."
        if reward:
            reason = (data.get("reason") or "").strip()
            if len(reason) > 100:
                return None, "포상 내용은 100자 이내로 입력하세요."

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
    return {"month": t.month, "day": t.day, "dow": WEEKDAYS[t.weekday()]}


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
    data = request.get_json(silent=True) or {}
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
    strength = (request.get_json(silent=True) or {}).get("strength")
    if not isinstance(strength, int) or not 0 <= strength <= 10000:
        return jsonify(error="총원을 0~10000 사이 숫자로 입력하세요."), 400
    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES ('strength', ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(strength),))
    db.commit()
    return jsonify(limits(strength))


@app.get("/api/requests")
def list_requests():
    # ?from=YYYY-MM-DD&to=YYYY-MM-DD 기간과 겹치는 신청만 반환
    start = request.args.get("from", "0000-01-01")
    end = request.args.get("to", "9999-12-31")
    rows = get_db().execute(
        "SELECT * FROM requests WHERE start_date <= ? AND end_date >= ? "
        "ORDER BY start_date, id",
        (end, start),
    ).fetchall()
    return jsonify([public_row(r) for r in rows])


@app.post("/api/requests")
def create_request():
    data = request.get_json(silent=True) or {}
    req, error = parse_request(data)
    if error:
        return jsonify(error=error), 400
    password = data.get("password") or ""
    if not 4 <= len(password) <= 30:
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
    req, error = parse_request(request.get_json(silent=True) or {})
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
    password = (request.get_json(silent=True) or {}).get("password") or ""
    if not row["password_hash"] or not check_password_hash(row["password_hash"], password):
        return jsonify(error="비밀번호가 일치하지 않습니다."), 403
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
    password = (request.get_json(silent=True) or {}).get("password", "")
    if not isinstance(password, str) or not check_admin_password(password):
        return jsonify(error="비밀번호가 틀렸습니다."), 401
    session["admin"] = time.time() + ADMIN_SESSION_SECONDS
    session["admin_version"] = admin_version()
    return "", 204


@app.put("/api/settings/admin-password")
@admin_required
def change_admin_password():
    data = request.get_json(silent=True) or {}
    current, new = data.get("current"), data.get("new")
    if not isinstance(current, str) or not check_admin_password(current):
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
    app.run(host="0.0.0.0", port=5000, debug=True)
