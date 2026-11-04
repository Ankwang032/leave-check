# 출타 신청 웹사이트 (leave-check)

부대 출타율 조정을 위한 휴가·외박·외출 신청 사이트입니다.
장병은 로그인 없이 달력에서 날짜를 골라 신청하고, 관리자는 총원과 종합기간을 설정하고 신청 목록과 출타 명단을 관리합니다.

- 기술: Python 3.12+, Flask 3, SQLite(로컬) / Postgres(배포, Neon)
- 배포: Vercel (GitHub `develop` → 프리뷰, `main` → production)
- 개발 기록과 설계상 결정은 [Develop-memo.md](Develop-memo.md)에 있습니다.

## 주요 기능

### 신청 화면 (`/`)
- **출타 종류**: 휴가 · 외박 · 외출
  - 휴가·외박은 시작일과 종료일을 차례로 클릭하고, 외출은 하루만 고릅니다.
- **휴가**
  - 연가 · 보상 · 포상 · 위로 종류별로 일수를 입력하며, 합계가 신청 기간(일수)과 같아야 합니다.
  - 세부사유(선택, 100자)를 적을 수 있고, 관리자만 볼 수 있습니다.
- **외출/외박**
  - 차수(1차 · 2차)를 고릅니다(선택).
  - 포상 · 공용에 체크할 수 있습니다.
  - 포상에 체크하면 포상 내용(예: 체력검정 특급 포상)을 적는 칸이 나타나며, 관리자만 볼 수 있습니다.
- **달력**: 날짜마다 종류별 인원과 `계 n/최대`를 보여줍니다. 출타율을 넘은 날은 붉게, 접수하지 않는 날은 흐리게 표시합니다.
- **명단·취소**: 날짜를 누르면 그날 출타자 명단이 나옵니다. 신청할 때 정한 비밀번호로 본인 신청을 취소합니다.

### 관리자 화면 (`/admin`)
- 비밀번호로 로그인합니다(환경변수 `ADMIN_PASSWORD`).
- 부대 총원과 종합기간을 설정합니다.
- 월별 신청 목록에서 신청을 수정·삭제합니다. 목록은 머리글을 눌러 정렬할 수 있습니다.
- 그 달에 출타율을 넘은 날짜를 확인합니다.
- 출타 명단을 인쇄합니다(A4, PDF 저장 가능). 전체 · 휴가 · 외출/외박 중에서 고르고 기간을 정합니다.

### 규칙
| 항목 | 내용 |
|---|---|
| 출타율 | 하루 기준 휴가 ≤ 총원의 20%, 휴가+외박+외출 ≤ 35% (소수점 버림). 공용 외출/외박은 제외 |
| 초과 시 | 신청은 받고, 신청 전후에 초과 사실을 안내 |
| 외출/외박 종합기간 | 수요일부터 7일간 출타분을 8일 전(화)부터 당일(수)까지 접수 |
| 휴가 종합기간 | 다음 달 휴가를 이번 달 20~28일에 접수 |
| 기준일 | 여러 날에 걸친 신청은 시작일 기준. 날짜는 한국 시간(KST)으로 판단 |

종합기간의 요일과 접수일은 관리자 화면에서 바꿀 수 있습니다.

## 파일 구성

```
app.py               Flask 서버 (DB, 검증, 출타율·종합기간 계산, API)
templates/
  index.html         신청 화면
  admin.html         관리자 화면
  print.html         인쇄용 출타 명단
static/base.css      공통 스타일 (색상 토큰, 다크 모드)
tests/               pytest 테스트
vercel.json          Vercel 설정 (Flask)
.vercelignore        배포에서 제외할 파일
requirements.txt     실행에 필요한 패키지
requirements-dev.txt 테스트용 패키지 (pytest)
```

## 로컬 실행

```bash
pip install -r requirements.txt
ADMIN_PASSWORD='관리자비밀번호' python app.py
```

브라우저에서 http://localhost:5000 을 엽니다. 관리자 화면은 http://localhost:5000/admin 입니다.

- 데이터는 `leave.db`(SQLite)에 저장됩니다. 이 파일은 처음 실행할 때 자동으로 만들어지며 git에서 제외됩니다.
- `ADMIN_PASSWORD`를 주지 않으면 로컬에서만 기본값 `1234`를 씁니다.
- 세션 키는 `SECRET_KEY` 환경변수를 쓰고, 없으면 `.secret_key` 파일을 자동으로 만듭니다(git 제외).

### 환경변수

| 이름 | 로컬 | 배포 | 설명 |
|---|---|---|---|
| `ADMIN_PASSWORD` | 선택 (기본 `1234`) | **필수** | 관리자 비밀번호 |
| `SECRET_KEY` | 선택 (`.secret_key` 자동 생성) | **필수** | 로그인 세션 서명 키 |
| `DATABASE_URL` | 선택 | **필수** (Neon 연결 시 자동) | 있으면 Postgres, 없으면 SQLite 사용 |
| `LEAVE_DB_PATH` | 선택 | - | SQLite 파일 경로 (기본 `leave.db`) |

배포 환경(Vercel)에서는 `ADMIN_PASSWORD`나 `SECRET_KEY`가 없으면 앱이 시작하지 않습니다. 기본 비밀번호로 공개되는 것을 막기 위해서입니다.

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest
```

출타율, 종합기간(화·수요일 겹침 포함), 신청 검증, 비밀번호 취소, 관리자 권한, 세부사유·포상 내용 비공개, 한국 시간 처리, 이전 DB 보정을 확인합니다.
테스트는 임시 DB를 쓰므로 `leave.db`에 영향을 주지 않습니다.

Postgres에서도 같은 테스트를 돌릴 수 있습니다. 테스트가 테이블 내용을 지우므로 **테스트 전용 DB**만 쓰세요.

```bash
docker run -d --rm --name leave-pg -e POSTGRES_PASSWORD=pg -p 5432:5432 postgres:17-alpine
TEST_DATABASE_URL=postgresql://postgres:pg@localhost:5432/postgres python -m pytest
docker rm -f leave-pg
```

## 배포 (Vercel)

GitHub 저장소 `Ankwang032/leave-check`가 Vercel 프로젝트 `leave-check`에 연결되어 있습니다.

| 브랜치 | 배포 | 접속 |
|---|---|---|
| `develop` 등 `main` 외 브랜치 | 프리뷰 | Vercel 로그인한 사람만 (Deployment Protection) |
| `main` | production | **누구나** |

- push하면 자동으로 배포됩니다. 브랜치마다 고정 주소가 생깁니다(예: `leave-check-git-develop-…vercel.app`).
- DB는 Neon Postgres(무료 플랜)를 씁니다. 프리뷰와 production이 **같은 DB**를 쓰므로, 공개 전에 테스트 데이터를 지우세요.
- 테이블은 서버가 시작할 때 자동으로 만들어지고, 새 컬럼도 자동으로 추가됩니다.

### production 공개 전 확인
1. 이름과 출타 일정이 공개되므로 **부대 보안 규정**을 확인합니다.
2. `ADMIN_PASSWORD`와 `SECRET_KEY`를 Vercel의 **Production** 환경에도 추가합니다.
3. 프리뷰에서 넣은 테스트 신청을 관리자 화면에서 지웁니다.
4. `develop`을 `main`에 합칩니다.

## 보안·개인정보
- 신청 비밀번호는 해시로 저장해서 관리자도 볼 수 없습니다.
- 세부사유와 포상 내용은 관리자만 볼 수 있고, 일반 API 응답에서는 빠집니다.
- 관리자로 로그인해 있어도 신청 화면에서 취소할 때는 신청 비밀번호가 필요합니다.
