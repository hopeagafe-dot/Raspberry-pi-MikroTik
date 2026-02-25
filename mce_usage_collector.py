#!/usr/bin/env python3
"""
mce_usage_collector.py

- MikroTik Hotspot 세션 사용량 + IoT(MCE_IoT_*) Hosts bytes
- 사용자 이름 기준으로 합산
- 증가분만 누적해서 ~/mcepi_data/usage_public.json 에 저장

추가 기능 (이 버전):

1) 월초(지정일) 자동 롤오버 & 과월 통계 저장
   - period_start_utc 기준 month가 바뀌면,
     이전 period의 usage_public 스냅샷을
     ~/mcepi_data/monthly_archives/usage_total_YYYYMMDD_HHMMSS.json 으로 저장
   - period_start_utc 를 지정일 00:00:00Z 로 갱신
   - 각 user 의 total_bytes 는 0 으로 리셋
   - _last_raw_bytes 는 그대로 유지 → delta 계산은 계속 정상

2) 일별 사용량 누적 파일 usage_daily.json 생성
   - delta bytes 기준으로 UTC 날짜별 per-user bytes 누적
   - ~/mcepi_data/usage_daily.json

3) RouterOS Hotspot USER 기준으로 존재하지 않는 사용자 자동 삭제
   - (※ 이전 버전 설명. 현재는 usage_public.json 에서 자동 삭제하지 않고,
      Hotspot USER 에 없는 이름은 집계 대상에서만 제외함.)
"""

from librouteros import connect
from datetime import datetime, timezone
import json
import os
from typing import Dict, Any, Tuple, Set, Optional


# =========================
# 설정 영역
# =========================
ROUTER_HOST = "192.168.88.1"
ROUTER_USER = "admin"        # 필요하면 API 전용 계정으로 변경

def _load_router_password() -> str:
    """~/router_password.txt 에서 RouterOS 비밀번호를 읽어온다."""
    try:
        p = os.path.expanduser("~/router_password.txt")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""

ROUTER_PASSWORD = _load_router_password()

# RouterOS SSH (Hotspot USER counters reset 용)
ROUTER_SSH_PORT = 22

DATA_DIR = os.path.expanduser("~/mcepi_data")
JSON_PATH = os.path.join(DATA_DIR, "usage_public.json")
DAILY_ARCHIVE_DIR = os.path.join(DATA_DIR, "daily_archives")
MANUAL_ARCHIVE_DIR = os.path.join(DATA_DIR, "manual_archives")
BILLING_SETTINGS_PATH = os.path.join(DATA_DIR, "billing_settings.json")
LOG_FILE = os.path.join(DATA_DIR, "collector.log")

# raw_now = U + A + I
TOL_SYNC_BYTES  = 100 * 1024 * 1024  # 100 MiB 허용오차(=104,857,600 bytes)

# 일별 집계 파일(일자별 delta 누적)
DAILY_JSON_PATH = os.path.join(DATA_DIR, "usage_daily.json")

# 과월 스냅샷 저장 디렉터리
MONTHLY_ARCHIVE_DIR = os.path.join(DATA_DIR, "monthly_archives")

# 기간 시작시간은 최초 생성 시 동적으로 계산.
# ~/mcepi_data/billing_start_day.txt 의 숫자(1~31)를 읽어
# "현재 진행 중인 기간의 시작일(이번 달 또는 전달 ?일)"을 반환한다.
# 파일이 없으면 이번 달 1일을 기본값으로 사용.
BILLING_START_DAY_FILE = os.path.join(DATA_DIR, "billing_start_day.txt")

def _load_billing_start_day() -> int:
    """billing_start_day.txt 에서 기준일(1~31)을 읽어 반환. 없으면 1."""
    try:
        if os.path.exists(BILLING_START_DAY_FILE):
            with open(BILLING_START_DAY_FILE, "r", encoding="utf-8") as _f:
                _d = int(_f.read().strip())
            if 1 <= _d <= 31:
                return _d
    except Exception:
        pass
    return 1

def _get_default_period_start() -> str:
    """
    billing_start_day.txt 의 기준일을 읽어
    현재 진행 중인 기간의 시작일 ISO 문자열을 반환한다.

    - 오늘 >= 이번 달 ?일  ->  이번 달 ?일 00:00:00Z
    - 오늘 <  이번 달 ?일  ->  전달 ?일 00:00:00Z
    - 파일 없으면 이번 달 1일 반환
    """
    import calendar as _cal
    start_day = _load_billing_start_day()
    _now = datetime.now(timezone.utc)

    # 이번 달 ?일 계산 (말일 clamp)
    _last_this = _cal.monthrange(_now.year, _now.month)[1]
    _day_this = min(start_day, _last_this)
    _this_start = datetime(_now.year, _now.month, _day_this, 0, 0, 0, tzinfo=timezone.utc)

    if _now >= _this_start:
        # 이번 달 ?일이 이미 지남 -> 이번 달 ?일이 기간 시작
        return _this_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        # 이번 달 ?일이 아직 안 지남 -> 전달 ?일이 기간 시작
        _prev_year = _now.year if _now.month > 1 else _now.year - 1
        _prev_month = _now.month - 1 if _now.month > 1 else 12
        _last_prev = _cal.monthrange(_prev_year, _prev_month)[1]
        _day_prev = min(start_day, _last_prev)
        _prev_start = datetime(_prev_year, _prev_month, _day_prev, 0, 0, 0, tzinfo=timezone.utc)
        return _prev_start.strftime("%Y-%m-%dT%H:%M:%SZ")



# =========================
# 로깅 설정
# =========================
# - print 대신 파일(collector.log) + 콘솔로 동시에 출력
# - RotatingFileHandler: 로그 파일이 너무 커지면 자동 롤링
import logging
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler()
    ]
)

# =========================
# 유틸 함수
# =========================
def now_utc_iso() -> str:
    # UTC 현재 시간을 JSON 표준 형식(Z)으로 반환
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



# -----------------------------
# 최소 스냅샷(users 리스트) 생성 헬퍼
# -----------------------------
def build_min_users_snapshot(user_config: dict, usage_public_out: dict) -> list:
    """user_config 정책 + usage_public_out 계측값을 병합해 users[] 최소 스냅샷 생성"""
    users_min = []
    cfg_users = (user_config or {}).get("users") or []
    by_name = (usage_public_out or {}).get("users_by_name") or {}
    if not isinstance(by_name, dict):
        by_name = {}

    for u in cfg_users:
        if not isinstance(u, dict):
            continue
        name = u.get("user")
        if not name:
            continue

        role = u.get("role", "crew")
        try:
            limit_gb = float(u.get("limit_gb", 100.0) or 0.0)
        except Exception:
            limit_gb = 100.0
        try:
            personal_prepaid = float(u.get("personal_prepaid", 0.0) or 0.0)
        except Exception:
            personal_prepaid = 0.0
        base_fee_override = u.get("base_fee_override", None)

        info = by_name.get(name) or {}
        try:
            total_bytes = int(info.get("total_bytes", 0) or 0)
        except Exception:
            total_bytes = 0
        try:
            offset_bytes = int(info.get("_offset_bytes", 0) or 0)
        except Exception:
            offset_bytes = 0

        users_min.append({
            "user": name,
            "role": role,
            "limit_gb": limit_gb,
            "personal_prepaid": personal_prepaid,
            "base_fee_override": base_fee_override,
            "total_bytes": total_bytes,
            "_offset_bytes": offset_bytes,
        })
    return users_min



def parse_iso(iso_str: str) -> datetime:
    """
    'YYYY-MM-DDTHH:MM:SSZ' 형식만 다룬다.
    (현재 시스템에서 우리가 저장하는 형식과 동일)
    """
    return datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def first_day_of_month(dt: datetime) -> datetime:
    """
    dt 와 같은 year/month 의 1일 00:00:00 (UTC) 를 리턴
    """
    return datetime(dt.year, dt.month, 1, 0, 0, 0, tzinfo=timezone.utc)


def ensure_data_dirs():
    # 데이터 저장 디렉토리들이 없으면 생성(Collector 첫 실행/복구 상황 대비)
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(MONTHLY_ARCHIVE_DIR):
        os.makedirs(MONTHLY_ARCHIVE_DIR, exist_ok=True)


# =========================================================
# [새로 추가] 원자적 쓰기 헬퍼 함수
# =========================================================
def atomic_write_json(filepath, data):
    """
    파일을 안전하게 저장합니다.
    1. .tmp 파일에 먼저 기록
    2. os.fsync로 디스크 기록 보장 (SD카드 보호)
    3. os.replace로 원본 파일과 원자적 교체
    """
    temp_path = f"{filepath}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()          # 버퍼 비우기
            os.fsync(f.fileno()) # 물리적 디스크(SD카드)에 기록 강제
        
        # 임시 파일을 원본 파일로 교체 (Atomic Operation)
        os.replace(temp_path, filepath)
        
    except Exception as e:
        logging.error(f"Atomic write failed for {filepath}: {e}")
        # 실패 시 임시 파일 정리
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass

# =========================
# usage_public.json 로드/저장
# =========================
def load_state() -> Dict[str, Any]:
    """
    기존 usage_public.json 상태를 읽어 내부 dict 구조로 변환.

    [외부 저장 포맷(파일)]
      - users는 리스트(list)로 저장:
        "users": [{"user": "...", "total_bytes": ..., "_last_raw_bytes": ..., "devices": {...}}, ...]

    [내부 운영 포맷(메모리)]
      - users는 dict로 운영(lookup/갱신이 쉬움):
        "users": { "MCE": {...}, "2EA": {...}, ... }

    devices 필드:
      - MAC 원장 구조를 위한 옵션 데이터
      - 기본 목표: MAC별 누적(total_bytes)을 증빙 자료로 남기기
    """
    ensure_data_dirs()

    def _fresh_state() -> Dict[str, Any]:
        return {
            "period_start_utc": _get_default_period_start(),
            "last_updated_utc": None,
            "users": {}
        }

    if not os.path.exists(JSON_PATH):
        # ✅ 파일이 없으면: 기본 포맷으로 즉시 파일 생성까지 해둔다
        state = _fresh_state()
        save_state(state)
        return state

    # ✅ 파일은 있는데 빈 파일/깨진 JSON이면: 크래시 방지 + 자동 복구
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logging.warning(f"[usage_public.json RECOVERY] invalid JSON -> reset. reason={e}")
        state = _fresh_state()
        save_state(state)
        return state

    users_dict: Dict[str, Dict[str, int]] = {}

    # 기존 리스트 구조를 dict 로 재구성
    for entry in raw.get("users", []):
        name = entry.get("user")
        if not name:
            continue

        # devices는 있을 수도/없을 수도 있음(구버전 호환)
        devices = entry.get("devices", {})
        if not isinstance(devices, dict):
            devices = {}

        users_dict[name] = {
            "total_bytes": int(entry.get("total_bytes", 0)),
            "_last_raw_bytes": int(entry.get("_last_raw_bytes", 0)),
            "_offset_bytes": int(entry.get("_offset_bytes", 0)),   # ✅ 추가
            "devices": devices,  # ✅ MAC 원장(선택)
        }

    period_start = raw.get("period_start_utc") or _get_default_period_start()

    return {
        "period_start_utc": period_start,
        "last_updated_utc": raw.get("last_updated_utc"),
        "users": users_dict,
    }

def save_state(state: Dict[str, Any]) -> None:
    """
    내부 dict 상태를 JSON 파일(usage_public.json)로 저장.
    """
    
    # =========================================================
    # 1. 데이터 가공 및 형식 잡기 (이 부분은 기존 코드 그대로 유지!)
    # =========================================================
    out_users = []

    for name in sorted(state["users"].keys()):
        info = state["users"][name]
        total_bytes = int(info.get("total_bytes", 0))
        last_raw = int(info.get("_last_raw_bytes", 0))
        offset = int(info.get("_offset_bytes", 0) or 0)

        # 여기서 형식을 잡아줍니다 (Row 생성)
        row = {
            "user": name,
            "total_bytes": total_bytes,
            "_last_raw_bytes": last_raw,
            "_offset_bytes": offset,   
        }

        out_users.append(row)

    # 최종 저장할 딕셔너리 구조 생성
    out = {
        "period_start_utc": state["period_start_utc"],
        "last_updated_utc": state["last_updated_utc"],
        "users": out_users,
    }

    # =========================================================
    # 2. 파일 저장 (이 부분만 atomic_write_json으로 교체!)
    # =========================================================
    # 기존의 with open(...) ~ os.replace(...) 코드를 아래 한 줄로 대체합니다.
    atomic_write_json(JSON_PATH, out)



# =========================
# 일별 집계용 상태(usage_daily.json)
# =========================
def load_daily_state() -> Dict[str, Any]:
    """
    usage_daily.json은 '날짜 롤오버 메타'만 유지한다.
    구조:
    {
      "current_date": "2026-01-21",
      "last_updated_utc": "2026-01-21T16:18:36Z"
    }
    """
    ensure_data_dirs()

    if not os.path.exists(DAILY_JSON_PATH):
        return {
            "current_date": None,
            "last_updated_utc": None,
        }

    try:
        with open(DAILY_JSON_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        # 혹시 과거 파일에 days가 남아있으면 읽는 즉시 제거(자동 정리)
        raw.pop("days", None)
        return {
            "current_date": raw.get("current_date"),
            "last_updated_utc": raw.get("last_updated_utc"),
        }
    except Exception:
        return {
            "current_date": None,
            "last_updated_utc": None,
        }


def save_daily_state(daily: Dict[str, Any]) -> None:
    # ---------------------------------------------------------
    # 1. 데이터 가공 (원래 님 코드에 있던 부분 - 살려야 함!)
    # ---------------------------------------------------------
    # usage_daily.json은 메타데이터만 저장한다고 하셨으므로 이 로직 필수
    out = {
        "current_date": daily.get("current_date"),
        "last_updated_utc": now_utc_iso(),
    }
    
    # ---------------------------------------------------------
    # 2. 저장 실행 (제가 추천한 원자적 쓰기 도구 사용)
    # ---------------------------------------------------------
    # out 변수에 담긴 깔끔한 데이터를 안전하게 저장합니다.
    atomic_write_json(DAILY_JSON_PATH, out)


    
# =========================
# Daily 스냅샷 저장(archive)
# =========================
# - UTC 날짜가 바뀌는 순간, 어제의 usage_public 상태를 “스냅샷”으로 daily_archives에 저장
# - snapshot 포맷은 manual 스냅샷과 동일한 형태(users=list + users_by_name=dict)로 정규화
def archive_daily_data(current_usage_state, target_date):
    try:
        ensure_data_dirs()
        os.makedirs(DAILY_ARCHIVE_DIR, exist_ok=True)

        # 스냅샷에 user_config도 함께 저장(나중에 정산/증빙 재현 가능)
        user_config = {}
        user_config_path = os.path.join(DATA_DIR, "user_config.json")
        if os.path.exists(user_config_path):
            with open(user_config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)

        # 스냅샷에 billing_settings도 함께 저장(정산 시점 재현 가능)
        billing_settings = {}
        if os.path.exists(BILLING_SETTINGS_PATH):
            with open(BILLING_SETTINGS_PATH, "r", encoding="utf-8") as f:
                billing_settings = json.load(f)

        # ============================================================
        # [PATCH] usage_public 포맷 정규화
        #  - manual snapshot과 동일하게:
        #    usage_public.users        = list
        #    usage_public.users_by_name = dict
        #  - record_date는 유지(요구사항)
        #
        #  - 추가 요구사항(이번 개선):
        #    devices(MAC 원장)는 “total_bytes만” 저장(증빙용)
        #    → 스냅샷에는 _last_raw_bytes 같은 운영용 내부값은 넣지 않음
        # ============================================================
        usage_public_in = current_usage_state or {}
        users_in = usage_public_in.get("users", {})

        users_list = []
        users_by_name = {}

        def _safe_int(x):
            try:
                return int(x or 0)
            except Exception:
                return 0

        # devices를 “total_bytes만 남겨” 스냅샷에 저장하기 위한 헬퍼
        def _devices_total_only(dev_in):
            """
            devices 딕셔너리에서 total_bytes만 남겨서 반환
            - {"MAC": {"total_bytes": X, "_last_raw_bytes": Y}} -> {"MAC": {"total_bytes": X}}
            """
            out = {}
            if not isinstance(dev_in, dict):
                return out
            for k, v in dev_in.items():
                if isinstance(v, dict):
                    out[k] = {"total_bytes": _safe_int(v.get("total_bytes"))}
                else:
                    # 혹시 v가 숫자/문자 등으로 들어오면 total_bytes로 감싸기
                    out[k] = {"total_bytes": _safe_int(v)}
            return out

        if isinstance(users_in, dict):
            # users가 dict(name -> info) 형태인 경우(현재 daily에서 흔함)
            # - 이 경우는 collector 내부 state(users)가 dict인 구조와 동일합니다.
            users_by_name = users_in

            # 표시/저장 순서:
            # 1) user_config에 정의된 순서 우선
            # 2) 나머지는 이름 정렬로 뒤에 추가
            ordered_names = []
            cfg_users = user_config.get("users")
            if isinstance(cfg_users, list):
                for u in cfg_users:
                    if isinstance(u, dict):
                        name = u.get("user")
                        if name and name in users_by_name:
                            ordered_names.append(name)

            for name in sorted(users_by_name.keys()):
                if name not in ordered_names:
                    ordered_names.append(name)

            # users(list) 생성: row에 devices(total-only) 포함
            for name in ordered_names:
                info = users_by_name.get(name) or {}
                if not isinstance(info, dict):
                    info = {}

                # devices 추가
                row = {
                    "user": name,
                    "total_bytes": _safe_int(info.get("total_bytes")),
                    "_last_raw_bytes": _safe_int(info.get("_last_raw_bytes")),
                    "_offset_bytes": _safe_int(info.get("_offset_bytes")),   # ✅ 추가
                }

                users_list.append(row)

            # users_by_name에도 증빙용 devices(total-only)를 포함(스냅샷 일관성)
            users_by_name = {}
            for name in (ordered_names if ordered_names else sorted(users_in.keys())):
                info = users_in.get(name) or {}
                if not isinstance(info, dict):
                    info = {}
                row = {
                    "total_bytes": _safe_int(info.get("total_bytes")),
                    "_last_raw_bytes": _safe_int(info.get("_last_raw_bytes")),
                    "_offset_bytes": _safe_int(info.get("_offset_bytes")),   # ✅ 추가
                }
                users_by_name[name] = row

        elif isinstance(users_in, list):
            # users가 list 형태인 경우(이미 manual 포맷과 동일)
            # - 구버전 스냅샷/수동저장 등에서 users=list로 들어오는 케이스 대비
            users_list = []
            users_by_name = {}
            for e in users_in:
                if not isinstance(e, dict):
                    continue
                name = e.get("user")
                if not name:
                    continue

                tb = _safe_int(e.get("total_bytes"))
                lr = _safe_int(e.get("_last_raw_bytes"))
                off = _safe_int(e.get("_offset_bytes"))   # ✅ 추가

                row = {"user": name, "total_bytes": tb, "_last_raw_bytes": lr, "_offset_bytes": off}
                users_list.append(row)

                row2 = {"total_bytes": tb, "_last_raw_bytes": lr, "_offset_bytes": off}
                users_by_name[name] = row2



        # dict/list 어떤 입력이든, 최종 스냅샷은 동일한 포맷으로 출력
        usage_public_out = {
            "period_start_utc": usage_public_in.get("period_start_utc"),
            "last_updated_utc": usage_public_in.get("last_updated_utc"),
            "users": users_list,
            "users_by_name": users_by_name,
        }

        # user_config 정책 + 계측값 병합
        # daily: 0 포함 전체 저장 (추이 추적 목적)
        full_users_snapshot = build_min_users_snapshot(user_config, usage_public_out)

        # ============================================================
        # daily snapshot 객체 구성
        # ============================================================
        snapshot = {
            "snapshot_type": "daily_auto",
            "record_date": target_date,
            "timestamp_utc": now_utc_iso(),
            "period_start_utc": usage_public_in.get("period_start_utc"),
            "last_updated_utc": usage_public_in.get("last_updated_utc"),
            "users": full_users_snapshot,  # 0 포함 전체 저장
        }

        save_path = os.path.join(DAILY_ARCHIVE_DIR, f"usage_daily_{target_date}.json")
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=4, ensure_ascii=False)

        logging.info(f"Daily archive saved: {target_date}")
        return True

    except Exception as e:
        logging.error(f"Archive Error: {str(e)}")
        return False


# =========================
# 월초 롤오버 & 과월 스냅샷
# =========================
def maybe_rollover_month(state: Dict[str, Any]) -> None:
    """
    월간 롤오버(정산/리셋) 기준은 billing_settings.json의
    billing_period_start_utc(우선) 또는 current_period_start_utc(대체) 값을 사용한다.

    - billing_settings.*_period_start_utc 는 "다음 롤오버 예정 시각(UTC)"으로 간주한다.
    - now >= target_dt 이면:
        1) monthly_archives에 직전 기간 스냅샷 저장
        2) state(usage_public.json)의 total_bytes를 0으로 리셋
        3) state.period_start_utc 를 target_dt 로 갱신

    - target_dt 를 지난 뒤 60초가 경과하면(= 00:01 수준),
      billing_settings.*_period_start_utc 를 "다음 달(또는 그 이상) 미래 시각"으로 자동 갱신한다.

    - 부팅 직후 시간 회귀 등으로 now_dt가 과거로 튀면, 롤오버/설정갱신을 모두 금지한다.
    """
    import calendar
    from datetime import timedelta
    from typing import Optional, List

    ensure_data_dirs()
    now_dt = datetime.now(timezone.utc)

    # ----------------------------
    # 0) state 내 시간 파싱 + 시간 회귀 방어
    # ----------------------------
    period_start_iso = state.get("period_start_utc") or _get_default_period_start()
    try:
        period_start_dt = parse_iso(period_start_iso)
    except Exception:
        period_start_iso = _get_default_period_start()
        period_start_dt = parse_iso(period_start_iso)
        state["period_start_utc"] = period_start_iso

    last_updated_iso = state.get("last_updated_utc")
    last_updated_dt = None
    if last_updated_iso:
        try:
            last_updated_dt = parse_iso(last_updated_iso)
        except Exception:
            last_updated_dt = None

    # now_dt가 period_start/last_updated보다 과거면 (예: 부팅 직후 시간 회귀),
    # 잘못된 롤오버/자동갱신이 일어나지 않도록 즉시 return
    if now_dt < period_start_dt:
        logging.warning(
            f"[Monthly Rollover Skipped] Clock rollback: now({now_dt.isoformat()}) < period_start({period_start_dt.isoformat()})"
        )
        return
    if last_updated_dt and now_dt < last_updated_dt:
        logging.warning(
            f"[Monthly Rollover Skipped] Clock rollback: now({now_dt.isoformat()}) < last_updated({last_updated_dt.isoformat()})"
        )
        return

    # user_config를 monthly 스냅샷에 포함(정산 재구성/증빙용)
    user_config_path = os.path.join(DATA_DIR, "user_config.json")
    user_config: Any = None
    try:
        with open(user_config_path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
    except FileNotFoundError:
        user_config = None
    except Exception as e:
        logging.warning(f"[User Config] Read failed: {e}")
        user_config = None

    # ----------------------------
    # 1) billing_settings.json 로드
    # ----------------------------
    billing: Dict[str, Any] = {}
    try:
        with open(BILLING_SETTINGS_PATH, "r", encoding="utf-8") as f:
            billing = json.load(f) or {}
    except FileNotFoundError:
        billing = {}
    except Exception as e:
        logging.warning(f"[Billing Settings] Read failed: {e}")
        billing = {}

    # auto_monthly_snapshot_enabled 가 명시적으로 false면, 아무것도 하지 않는다.
    if billing.get("auto_monthly_snapshot_enabled") is False:
        return

    # ----------------------------
    # 2) target_dt 결정 (다음 롤오버 예정 시각)
    # ----------------------------
    def _dt_to_iso_z(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    target_iso = billing.get("billing_period_start_utc") or billing.get("current_period_start_utc")
    target_dt: Optional[datetime] = None
    if target_iso:
        try:
            target_dt = parse_iso(str(target_iso))
        except Exception as e:
            logging.warning(f"[Billing Settings] Invalid target iso '{target_iso}': {e}")
            target_dt = None

    # billing_settings 가 없거나 target이 파싱 불가면, 기존(월 경계) 방식으로 폴백
    if target_dt is None:
        if (period_start_dt.year, period_start_dt.month) == (now_dt.year, now_dt.month):
            return
        target_dt = first_day_of_month(now_dt)
        target_iso = _dt_to_iso_z(target_dt)

    # ----------------------------
    # 3) 롤오버 수행 (target_dt를 처음 통과한 시점에 1회)
    # ----------------------------
    already_rolled = period_start_dt >= target_dt


    if (not already_rolled) and (now_dt >= target_dt):
            # (3-1) 스냅샷 생성: "직전 기간" = [state.period_start_utc, target_dt)
            # ============================================================
            # [PATCH] monthly도 daily/manual과 동일한 usage_public 포맷으로 정규화
            # ============================================================
            usage_public_in = state or {}
            users_in = usage_public_in.get("users", {})

            users_list = []
            users_by_name = {}

            def _safe_int(x):
                try:
                    return int(x or 0)
                except Exception:
                    return 0

            # users가 dict인 경우 (내부 state 구조)
            if isinstance(users_in, dict):
                ordered_names = []
                cfg_users = (user_config or {}).get("users") if isinstance(user_config, dict) else None
                if isinstance(cfg_users, list):
                    for u in cfg_users:
                        if isinstance(u, dict):
                            name = u.get("user")
                            if name and name in users_in:
                                ordered_names.append(name)
                
                for name in sorted(users_in.keys()):
                    if name not in ordered_names:
                        ordered_names.append(name)

                for name in ordered_names:
                    info = users_in.get(name) or {}
                    if not isinstance(info, dict):
                        info = {}
                    
                    tb = _safe_int(info.get("total_bytes"))
                    lr = _safe_int(info.get("_last_raw_bytes"))
                    off = _safe_int(info.get("_offset_bytes"))

                    row = {"user": name, "total_bytes": tb, "_last_raw_bytes": lr, "_offset_bytes": off}
                    users_list.append(row)
                    users_by_name[name] = row # 단순 참조용

            # users가 list인 경우 (혹시 모를 호환성)
            elif isinstance(users_in, list):
                for e in users_in:
                    if not isinstance(e, dict): continue
                    name = e.get("user")
                    if not name: continue
                    
                    tb = _safe_int(e.get("total_bytes"))
                    lr = _safe_int(e.get("_last_raw_bytes"))
                    off = _safe_int(e.get("_offset_bytes"))
                    row = {"user": name, "total_bytes": tb, "_last_raw_bytes": lr, "_offset_bytes": off}
                    users_list.append(row)
                    users_by_name[name] = row

            # 스냅샷 생성을 위한 임시 구조체
            usage_public_out = {
                "period_start_utc": usage_public_in.get("period_start_utc"),
                "last_updated_utc": usage_public_in.get("last_updated_utc"),
                "users": users_list,
                "users_by_name": users_by_name,
            }

            # ------------------------------------------------------------
            # [FIX] Monthly 스냅샷 저장 시 "사용량 0" 사용자 제외 로직
            # ------------------------------------------------------------
            # 1. 전체 유저 리스트 생성
            full_users_snapshot = build_min_users_snapshot(user_config, usage_public_out)
            
            # 2. 0 사용량 필터링 수행
            users_min_filtered = []
            for u in full_users_snapshot:
                try:
                    t_bytes = int(u.get("total_bytes", 0) or 0)
                    o_bytes = int(u.get("_offset_bytes", 0) or 0)
                    # 사용량이 조금이라도 있으면 포함
                    if (t_bytes + o_bytes) > 0:
                        users_min_filtered.append(u)
                except:
                    pass

            # 3. 아카이브 객체 생성 (필터링된 리스트 사용)
            archive_filename = f"usage_total_{now_dt.strftime('%Y%m%d_%H%M%S')}.json"
            archive_path = os.path.join(MONTHLY_ARCHIVE_DIR, archive_filename)

            archive_obj = {
                "snapshot_type": "monthly_auto",
                "timestamp_utc": _dt_to_iso_z(now_dt),
                "period_start_utc": usage_public_in.get("period_start_utc"),
                "last_updated_utc": usage_public_in.get("last_updated_utc"),
                "period_end_utc": _dt_to_iso_z(target_dt),
                "users": users_min_filtered,  # <--- [중요] 필터링된 결과 적용
            }

            try:
                with open(archive_path, "w", encoding="utf-8") as f:
                    json.dump(archive_obj, f, ensure_ascii=False, indent=2)
                logging.info(f"Monthly archive saved: {archive_filename}")
            except Exception as e:
                logging.error(f"[Monthly Rollover] Archive write failed: {e}")
                # 저장 실패 시에는 리셋하지 않고 리턴하여 데이터 보호
                return

            # (3-2) state 리셋 + period_start 갱신
            # - 여기서부터는 “새 기간”을 시작하므로 JSON 누적값을 0으로 초기화
            state["period_start_utc"] = _dt_to_iso_z(target_dt)

            # total_bytes=0 리셋 (offset은 0으로 리셋, last_raw는 유지하거나 0으로 - 여기선 0으로)
            for _, info in state.get("users", {}).items():
                info["total_bytes"] = 0
                info["_last_raw_bytes"] = 0
                info["_offset_bytes"] = 0

            logging.info("[Monthly Rollover] RouterOS counters reset is handled by RouterOS scheduler.")


    # ----------------------------
    # 4) target_dt 지난 뒤 일정 시간 경과 시점에, billing_settings를 다음 "미래" target으로 자동 갱신
    # ----------------------------
    if now_dt < (target_dt + timedelta(seconds=30)):
        return

    # state.period_start_utc 가 target_dt 이상이면 "이번 target은 이미 통과/롤오버됨"으로 간주
    try:
        new_period_start_dt = parse_iso(state.get("period_start_utc") or _get_default_period_start())
    except Exception:
        new_period_start_dt = period_start_dt

    if new_period_start_dt < target_dt:
        return

    # billing_settings가 아직도 "지난 target"을 가리키는 경우에만 갱신
    b1 = billing.get("billing_period_start_utc")
    b2 = billing.get("current_period_start_utc")
    if (b1 and str(b1) != str(target_iso)) and (b2 and str(b2) != str(target_iso)):
        return

    def _add_one_month_clamped(dt: datetime) -> datetime:
        # 월말(예: 31일) 처리: 다음달에 해당 일이 없으면 마지막 날로 clamp
        y = dt.year
        m = dt.month + 1
        if m == 13:
            y += 1
            m = 1
        last_day = calendar.monthrange(y, m)[1]
        d = min(dt.day, last_day)
        return datetime(y, m, d, dt.hour, dt.minute, dt.second, tzinfo=timezone.utc)

    # 다음달로 1회 이동한 뒤, now_dt보다 미래가 될 때까지 월을 추가(정전/장기정지 대비)
    next_dt = _add_one_month_clamped(target_dt)
    while next_dt <= now_dt:
        next_dt = _add_one_month_clamped(next_dt)

    next_iso = _dt_to_iso_z(next_dt)

    billing["billing_period_start_utc"] = next_iso
    billing["current_period_start_utc"] = next_iso

    # 사람이 보기 좋게 day/hour/minute도 동기화
    billing["billing_start_day"] = next_dt.day
    billing["billing_start_hour"] = next_dt.hour
    billing["billing_start_minute"] = next_dt.minute

    try:
        with open(BILLING_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(billing, f, ensure_ascii=False, indent=2)
        logging.info(f"Billing period advanced: {target_iso} -> {next_iso}")
    except Exception as e:
        logging.error(f"[Billing Settings] Write failed: {e}")
        return


# =========================
# MikroTik 데이터 수집 부분
# =========================
def get_hotspot_usage(api) -> Tuple[Dict[str, int], Set[str]]:
    """
    /ip hotspot active 의 bytes-in + bytes-out 을 user 기준으로 합산.
    + active에 잡힌 mac-address set 도 같이 반환(이중계수 방지용)

    리턴:
      ( {"MCE": 123, "AE": 456, ...},  {"AA:BB:..", "CC:DD:..", ...} )
    """
    usage: Dict[str, int] = {}
    active_macs: Set[str] = set()

    active = api.path("ip", "hotspot", "active")

    for sess in active:
        user = sess.get("user")
        if not user:
            continue

        mac = sess.get("mac-address")
        if mac:
            active_macs.add(str(mac).upper())

        b_in = int(sess.get("bytes-in", 0) or 0)
        b_out = int(sess.get("bytes-out", 0) or 0)
        total = b_in + b_out

        usage[user] = usage.get(user, 0) + total

    return usage, active_macs



def get_iot_usage_from_hotspot_host(api, active_macs: Optional[Set[str]] = None) -> Dict[str, int]:
    """
    IoT 사용량을 mangle이 아니라 Hotspot 테이블에서 집계한다.

    기준:
      - /ip hotspot ip-binding 중 comment에 "_IoT_"가 포함된 항목만 IoT로 간주
      - comment 형식: "<USER>_IoT_<DEVICE>"  → <USER>에게 귀속
      - 해당 ip-binding의 mac-address로 /ip hotspot host에서 bytes-in/out 합을 찾아 더함
      - 안전장치: 그 MAC이 /ip hotspot active에 잡혀 있으면(= 유저 로그인 세션으로 잡힌 경우)
        Active(A)에서 이미 계수되므로 IoT(I)에서는 제외하여 이중계수 방지

    반환:
      {"MCE": 123456, "3E": 7890, ...}  (user별 IoT bytes 합)
    """
    usage: Dict[str, int] = {}

    if active_macs is None:
        active_macs = set()
    else:
        # 혹시 소문자 들어올 수 있으니 통일
        active_macs = {str(m).upper() for m in active_macs}

    # 1) hotspot host를 먼저 맵으로 만든다: mac -> (bytes-in+bytes-out)
    host_bytes_by_mac: Dict[str, int] = {}
    hosts = api.path("ip", "hotspot", "host")
    for h in hosts:
        mac = h.get("mac-address")
        if not mac:
            continue
        mac_u = str(mac).upper()

        b_in = int(h.get("bytes-in", 0) or 0)
        b_out = int(h.get("bytes-out", 0) or 0)
        tot = b_in + b_out

        # 같은 MAC이 여러 번 있을 수도 있어 합산
        host_bytes_by_mac[mac_u] = host_bytes_by_mac.get(mac_u, 0) + tot

    # 2) ip-binding에서 IoT 항목만 찾아 user로 귀속
    bindings = api.path("ip", "hotspot", "ip-binding")
    for b in bindings:
        comment = (b.get("comment") or "")
        if "_IoT_" not in comment:
            continue

        # comment: "<USER>_IoT_<DEVICE>"
        user = comment.split("_IoT_", 1)[0].strip()
        if not user:
            continue

        mac = b.get("mac-address")
        if not mac:
            continue
        mac_u = str(mac).upper()

        # 이중계수 방지: active면 IoT에서 제외
        if mac_u in active_macs:
            continue

        tot = host_bytes_by_mac.get(mac_u, 0)
        if tot <= 0:
            continue

        usage[user] = usage.get(user, 0) + tot

    return usage



def get_hotspot_usernames(api) -> set:
    """
    /ip hotspot user 의 name 목록을 set 으로 반환.

    사용 목적:
      - Hotspot 에 정식 등록된 USER 이름만 집계 대상에 포함시키기 위한 필터용.
      - usage_public.json 에 이미 존재하더라도, 여기 없는 이름은 새 delta 집계에서 제외.
    """
    names = set()
    users = api.path("ip", "hotspot", "user")
    for u in users:
        name = u.get("name")
        if name:
            names.add(name)
    return names


def get_hotspot_user_counters(api) -> Dict[str, int]:
    """
    /ip hotspot user 의 bytes-in + bytes-out 을 name 기준으로 반환.
    리턴: {"user96": 12345, ...}  (U 값)
    """
    usage: Dict[str, int] = {}
    users = api.path("ip", "hotspot", "user")
    for u in users:
        name = u.get("name")
        if not name:
            continue
        b_in = int(u.get("bytes-in", 0) or 0)
        b_out = int(u.get("bytes-out", 0) or 0)
        usage[name] = b_in + b_out
    return usage






# =========================
# 메인 로직
# =========================
def main():
    # 1) 이전 상태 로드
    state = load_state()
    users_state = state["users"]

    # 3) 일별 집계 상태 로드
    daily_state = load_daily_state()
    now_dt = datetime.now(timezone.utc)
    today_str = now_dt.strftime("%Y-%m-%d")

    # --- Daily 날짜 변경 감지/아카이브 ---
    # - UTC 날짜 기준으로 “어제” 데이터 스냅샷을 daily_archives에 저장하고,
    #   오늘 날짜로 daily_state를 초기화합니다.
    old_date = daily_state.get("current_date")  # 저장되어 있던 날짜

    if old_date and old_date != today_str:
        # 시간 회귀(과거 날짜로 점프)면 아카이브/초기화 금지(데이터 꼬임 방지)
        if today_str < old_date:
            logging.warning(f"[Daily Rotation Skipped] Clock rollback: {old_date} -> {today_str}")
        else:
            logging.info(f"날짜 변경 감지: {old_date} -> {today_str}. 어제 데이터를 아카이브합니다.")
            archive_daily_data(state, old_date)
            daily_state["current_date"] = today_str
            save_daily_state(daily_state)

    elif not old_date:
        daily_state["current_date"] = today_str
        save_daily_state(daily_state)

    # 2) 월초 롤오버 체크 & 과월 스냅샷
    # (중요: daily 아카이브가 먼저 수행된 뒤에 월초 리셋이 되도록 여기서 호출)
    maybe_rollover_month(state)

    # 4) MikroTik API 연결
    api = connect(
        host=ROUTER_HOST,
        username=ROUTER_USER,
        password=ROUTER_PASSWORD,
        port=8728,
        timeout=10,
    )

    # 집계 대상 user 필터(라우터에 실제 존재하는 hotspot user만)
    router_usernames = get_hotspot_usernames(api)

    hotspot_active_by_user, active_macs = get_hotspot_usage(api)        # A + active mac set
    hotspot_user_counters = get_hotspot_user_counters(api)             # U
    iot_usage = get_iot_usage_from_hotspot_host(api, active_macs)      # IoT (host 기반)

  

    # 7) 증가분만 계산해서 누적 (✅ MAC 원장 기반)
    #
    # 핵심 원리:
    #   - RouterOS에서 가져오는 raw(bytes-in/out)는 “세션/카운터 누적값”
    #   - 우리는 마지막 raw(last)를 저장해두고, 이번 raw(now)와의 차이(delta)만 누적(total)에 더함
    #   - 문제 케이스(로그아웃/세션 감소/리셋 등)로 now < last가 될 수 있는데,
    #     이때 delta를 now로 더해버리면 “과대집계”가 발생할 수 있으므로 delta=0 처리(A단계 정책)
    
    

    for user in router_usernames:
        U = int(hotspot_user_counters.get(user, 0) or 0)
        A = int(hotspot_active_by_user.get(user, 0) or 0)
        I = int(iot_usage.get(user, 0) or 0)

        raw_now = U + A + I

        user_info = users_state.setdefault(user, {
            "total_bytes": 0,
            "_last_raw_bytes": 0,
            "_offset_bytes": 0,   # ✅ 추가
        })
        
        total = int(user_info.get("total_bytes", 0) or 0)
        raw_last = int(user_info.get("_last_raw_bytes", 0) or 0)

        # raw_now = U + A + I
        diff = total - raw_now  # (양수면 total이 더 큼)

        if raw_now + TOL_SYNC_BYTES  >= total:
            # (선택) 허용오차로 '감소 동기화'가 발생한 경우만 로그 남김
            if 0 < diff <= TOL_SYNC_BYTES:
                logging.warning(
                    f"[TOL_SYNC] user={user} total({total}) > raw_now({raw_now}) by {diff} bytes "
                    f"(U={U}, A={A}, I={I}) -> trust RouterOS, set total_new=raw_now"
                )
                
            # 정상 동기화(가장 정확한 라우터 값을 그대로 사용)
            total_new = raw_now
        else:
            # BO/누락(under-report) 구간: 증가분만 더함
            delta = raw_now - raw_last
            if delta < 0:
                delta = 0
            total_new = total + delta

        user_info["total_bytes"] = total_new
        user_info["_last_raw_bytes"] = raw_now

        # (선택) 기존 MAC 원장/legacy 데이터를 완전히 없애고 싶으면:
        if "devices" in user_info:
            user_info.pop("devices", None)



    # 저장(usage_public + usage_daily)
    state["last_updated_utc"] = now_utc_iso()
    save_state(state)
    save_daily_state(daily_state)

    # 콘솔 출력(운영 확인용)
    print("=== MCE Usage Collector ===")
    print(f"Period start : {state['period_start_utc']}")
    print(f"Last updated : {state['last_updated_utc']}")
    print()
    for name in sorted(users_state.keys()):
        total_b = users_state[name]["total_bytes"]
        total_gb = total_b / (1024 ** 3)
        print(f"{name:10s}  {total_b:12d} bytes  ({total_gb:6.3f} GiB)")


if __name__ == "__main__":
    main()
