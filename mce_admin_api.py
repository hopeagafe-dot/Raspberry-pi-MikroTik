#!/usr/bin/env python3

import subprocess  # create_user_config.py 실행용
import re
import string
import json
import os
import time          # 🔹 비번 검증 재시도 딜레이용
import paramiko  # SSH로 RouterOS CLI 실행

from pathlib import Path
from datetime import datetime

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    redirect,
    session,
    make_response,   # 비번변경 OPTIONS용
)


# ---- CORS: RouterOS Hotspot status.html 용 ----
ALLOWED_ORIGIN_HOTSPOT = "http://login.mce-ship.local" # <--- 추가
ALLOWED_ORIGIN_IP      = "http://192.168.88.1"
ALLOWED_ORIGINS = [ALLOWED_ORIGIN_IP, ALLOWED_ORIGIN_HOTSPOT] # <--- 리스트로 관리


def _add_cors(resp):
    # 요청 헤더에서 Origin을 읽어와서 허용된 Origin 중 하나인지 확인
    origin = request.headers.get("Origin")
    if origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin # <--- 요청한 Origin을 그대로 반영
    else:
        # 허용 목록에 없는 경우, 기본값 또는 *을 설정 (보안상 *보다는 구체적인 리스트가 좋습니다)
        # 여기서는 ALLOWED_ORIGIN_IP를 기본으로 설정합니다.
        resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN_IP
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Credentials"] = "true"  # 테스트 환경 호환용
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# 🔹 테스트용 api_test_pw.py 와 똑같이, 모든 응답에 CORS를 붙이도록 (관리자/Hotspot 페이지에서 API를 호출하는 경우 대비)
#@app.after_request
#def after_request_cors(resp):
#    # 이미 Access-Control-Allow-Origin 헤더가 설정된 경우 (예: api_monthly_usage 의 '*') 는 건너뜀
#    if "Access-Control-Allow-Origin" in resp.headers:
#        return resp

#    # 그 외의 모든 응답에 ALLOWED_ORIGIN (RouterOS IP) 에 대한 CORS 헤더 적용
#    return _add_cors(resp)


# 🔹 RouterOS SSH 접속 정보 (비번 변경 전용)
ROUTER_HOST = "192.168.88.1"
ROUTER_USER = "admin"
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

ROUTER_PASS = _load_router_password()

SSH_PORT     = 22


def _run_ssh_command(cmd: str):
    """
    RouterOS 에 SSH 접속해서 CLI 명령을 실행하고,
    stdout / stderr 문자열을 리턴.
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        ROUTER_HOST,
        port=SSH_PORT,
        username=ROUTER_USER,
        password=ROUTER_PASS,
        timeout=10,
        allow_agent=False,
        look_for_keys=False,
    )
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode("utf-8", errors="ignore").strip()
    err = stderr.read().decode("utf-8", errors="ignore").strip()
    ssh.close()
    return out, err


def _escape_password(pw: str):
    """
    test_change_password.py / api_test_pw.py 에서 이미 검증한
    RouterOS 비밀번호 이스케이프 로직과 동일하게 적용.
    """
    pw = pw.replace("\\", "\\\\")
    pw = pw.replace("\"", "\\\"")
    pw = pw.replace("$", "\\$")
    return pw


# 비밀번호 정책 서버 검사 함수 (ASCII 가시문자만 허용, 길이 4~64)
_ALLOWED = set(string.printable) - set("\t\n\r\x0b\x0c")  # 공백(space) 포함 가시문자만

def _validate_new_password(pw: str):
    if not (4 <= len(pw) <= 64):
        return False, "Password must be 4–64 characters."
    if any(ch not in _ALLOWED for ch in pw):
        return False, "Only ASCII printable characters are allowed."
    return True, ""





# ===== 기본 경로 설정 =====
BASE_DIR = Path.home()
DATA_DIR = BASE_DIR / "mcepi_data"
TEMPLATE_DIR = BASE_DIR / "Templates"

# 추가된 아카이브 경로
MANUAL_ARCHIVES_DIR = DATA_DIR / "manual_archives"
DAILY_ARCHIVES_DIR  = DATA_DIR / "daily_archives"
MONTHLY_ARCHIVES_DIR = DATA_DIR / "monthly_archives"   # [ADD]


# 필요한 폴더가 없으면 자동 생성
#MANUAL_ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
#DAILY_ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
# =================================================

USAGE_PUBLIC_PATH = DATA_DIR / "usage_public.json"
USAGE_OVERRIDE_PATH = DATA_DIR / "usage_override.json"
BILLING_RESULT_PATH = DATA_DIR / "result_billing.json"
USER_CONFIG_PATH = DATA_DIR / "user_config.json"
OVERWRITE_LOG_PATH = DATA_DIR / "usage_overwrite.log"
BILLING_SETTINGS_PATH = DATA_DIR / "billing_settings.json"
BILLING_START_DAY_PATH = DATA_DIR / "billing_start_day.txt"  # 매월 기준일(day) 숫자만 저장
BILLING_ENGINE_PARAMS_PATH = DATA_DIR / "billing_engine_params.json"  # 요금 계산 엔진 상수 설정

ADMIN_PASSWORD_FILE = BASE_DIR / "admin_password.txt"

# ★ SUPER ADMIN 비밀번호 (수퍼관리자 전용)
SUPER_ADMIN_PASSWORD = "Star$625Link"

# ★ create_user_config.py 후보 위치 (둘 중 존재하는 쪽을 실행)
CREATE_USER_CONFIG_CANDIDATES = [
    DATA_DIR / "create_user_config.py",   # ~/mcepi_data/create_user_config.py
    BASE_DIR / "create_user_config.py",   # ~/create_user_config.py
]

# ===== Flask 앱 =====
app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
app.secret_key = "MCE_Super_ADMIN"


# ===== 유틸 함수: JSON 입출력 =====
def _safe_read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _safe_write_json(path: Path, data) -> None:
    """원자적(atomic) JSON 저장: 임시파일에 쓴 뒤 os.replace로 교체."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)



def _ensure_json_file(path: Path, default: dict) -> dict:
    """
    파일이 없거나(혹은 0바이트/깨진 JSON)인 경우:
      - 기본 포맷(default)으로 즉시 생성(복구) 후 반환
    정상 JSON이면 그대로 반환.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # 파일이 없거나 0바이트면 default로 생성
    try:
        if (not path.exists()) or path.stat().st_size == 0:
            _safe_write_json(path, default)
            return default
    except Exception:
        # stat 실패 등 예외도 default로 복구
        _safe_write_json(path, default)
        return default

    # 파일은 있으나 JSON이 깨졌으면 default로 복구
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    _safe_write_json(path, default)
    return default

def _iso_to_display_str(s: str) -> str:
    """ISO 형태(또는 비슷한 문자열)를 'YYYY-MM-DD HH:MM' 형태로 바꿔서 화면에 보여주기."""
    try:
        if len(s) >= 16 and s[4] == "-" and s[7] == "-" and s[10] == " ":
            return s[:16]

        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")

        if "T" in s:
            date_part, time_part = s.split("T", 1)
            hhmm = time_part[:5]
            return f"{date_part} {hhmm}"

        return s
    except Exception:
        return s



def _reset_usage_public_file_all_users() -> dict:
    """
    usage_public.json의 모든 사용자 total_bytes/_last_raw_bytes를 0으로 리셋하고,
    period_start_utc/last_updated_utc를 현재 UTC로 갱신한 뒤 저장한다.
    """
    raw = _ensure_json_file(
        USAGE_PUBLIC_PATH,
        {"period_start_utc": None, "last_updated_utc": None, "users": []},
    )

    users = raw.get("users", [])
    if isinstance(users, list):
        for u in users:
            if not isinstance(u, dict):
                continue
            u["total_bytes"] = 0
            u["_last_raw_bytes"] = 0
            u["_offset_bytes"] = 0   # ✅ 추가

    # load_usage_public()이 만들어 붙인 users_by_name 같은 보조키가 있으면 제거(파일 깔끔 유지)
    raw.pop("users_by_name", None)

    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    raw["period_start_utc"] = now_iso
    raw["last_updated_utc"] = now_iso
    raw["users"] = users

    _safe_write_json(USAGE_PUBLIC_PATH, raw)
    return {"period_start_utc": now_iso, "last_updated_utc": now_iso}



# ===== 데이터 로더 =====
def load_usage_public():
    """
    usage_public.json 을 읽어서 bytes 기반으로 정리.

    리턴:
      {
        "period_start_utc": str | None,
        "last_updated_utc": str | None,
        "users": [...],
        "users_by_name": { user: {"total_bytes": int, "_last_raw_bytes": int|None} }
      }
    """

    # ✅ 파일이 없거나(삭제) / 빈 파일(0 byte)이면 기본 포맷으로 생성
    if (not USAGE_PUBLIC_PATH.exists()) or (USAGE_PUBLIC_PATH.stat().st_size == 0):
        _safe_write_json(
            USAGE_PUBLIC_PATH,
            {"period_start_utc": None, "last_updated_utc": None, "users": []},
        )

    raw = _ensure_json_file(
        USAGE_PUBLIC_PATH,
        {"period_start_utc": None, "last_updated_utc": None, "users": []},
    )

    by_name = {}
    for u in raw.get("users", []):
        name = u.get("user")
        if not name:
            continue
        try:
            total_bytes = int(u.get("total_bytes", 0))
        except Exception:
            total_bytes = 0
        try:
            last_raw = int(u.get("_last_raw_bytes", 0))
        except Exception:
            last_raw = 0
        try:
            offset = int(u.get("_offset_bytes", 0))
        except Exception:
            offset = 0

        by_name[name] = {
            "total_bytes": total_bytes,
            "_last_raw_bytes": last_raw,
            "_offset_bytes": offset,   # ✅ 추가
        }

    raw["users_by_name"] = by_name
    return raw


def load_usage_override():
    """
    usage_override.json (있을 경우) 를 읽어서 bytes 기반으로 정리.

    ※ 현재는 override 기반 동작을 사용하지 않지만,
       과거 데이터 호환을 위해 함수는 남겨 둠.
    """
    raw = _safe_read_json(
        USAGE_OVERRIDE_PATH,
        {"period_start_utc": None, "last_updated_utc": None, "users": []},
    )

    by_name = {}
    for u in raw.get("users", []):
        name = u.get("user")
        if not name:
            continue

        if "total_bytes" in u:
            try:
                total_bytes = int(u.get("total_bytes", 0))
            except Exception:
                total_bytes = 0
        else:
            gb = u.get("total_gb", 0.0)
            try:
                gb = float(gb)
            except Exception:
                gb = 0.0
            total_bytes = int(gb * (1024 ** 3))

        by_name[name] = {
            "total_bytes": total_bytes,
            "_last_raw_bytes": total_bytes,
        }

    raw["users_by_name"] = by_name
    return raw


def build_usage_bytes_map(usage_public, usage_override, user_cfg):
    """
    최종 usage_bytes 맵을 생성.

    우선순위:
      1) usage_override.json
      2) usage_public.json
    """
    usage_bytes = {}

    for name, info in usage_public.get("users_by_name", {}).items():
        try:
            b = int(info.get("total_bytes", 0))
        except Exception:
            b = 0
        usage_bytes[name] = b

    for name, info in usage_override.get("users_by_name", {}).items():
        try:
            b = int(info.get("total_bytes", 0))
        except Exception:
            b = 0
        usage_bytes[name] = b

    return usage_bytes


def load_user_config():
    # ✅ 파일이 없거나 빈 파일이면: RouterOS 기준으로 user_config 자동 생성 시도
    if (not USER_CONFIG_PATH.exists()) or (USER_CONFIG_PATH.stat().st_size == 0):
        run_create_user_config()

    data = _safe_read_json(USER_CONFIG_PATH, {"users": []})

    # ✅ 생성 스크립트가 실패했거나 users가 비어있으면 한 번 더 시도
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, list) or len(users) == 0:
        run_create_user_config()
        data = _safe_read_json(USER_CONFIG_PATH, {"users": []})

    # ✅ 그래도 실패하면: 최소 템플릿을 실제 파일로 생성해서 API 안정성 확보
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, list):
        data = {"users": []}
        _safe_write_json(USER_CONFIG_PATH, data)

    return data


def load_billing_result():
    return _safe_read_json(
        BILLING_RESULT_PATH,
        {
            "period_start_utc": None,
            "last_updated_utc": None,
            "params": {},
            "summary": {},
            "users": [],
        },
    )

def load_admin_password():
    try:
        if ADMIN_PASSWORD_FILE.exists():
            text = ADMIN_PASSWORD_FILE.read_text(encoding="utf-8").strip()
            return text or None
        return None
    except Exception:
        return None

def load_billing_settings():
    # ✅ 파일이 없거나 빈 파일이면 기본 포맷 생성
    if (not BILLING_SETTINGS_PATH.exists()) or (BILLING_SETTINGS_PATH.stat().st_size == 0):
        _safe_write_json(BILLING_SETTINGS_PATH, {"billing_period_start_utc": None})

    return _safe_read_json(
        BILLING_SETTINGS_PATH,
        {"billing_period_start_utc": None},
    )

def save_billing_settings(settings: dict):
    _safe_write_json(BILLING_SETTINGS_PATH, settings)

# ===== billing_engine_params.json: 요금 계산 엔진 상수 =====

BILLING_ENGINE_PARAMS_DEFAULT = {
    "_comment": "요금 계산 엔진 상수 설정 파일. 이 값을 수정하면 정산 결과에 즉시 반영됩니다.",
    "manager": {
        "under_limit_deduction": 60,
        "over_limit_fixed_fee": 110
    },
    "crew": {
        "over_limit_surcharge": 5,
        "lowest_usage_deduction": 5,
        "under_half_threshold": 0.5,
        "under_half_deduction": 10
    },
    "apprentice": {
        "rate": 0.5
    }
}

def load_billing_engine_params() -> dict:
    """billing_engine_params.json 을 읽어 반환. 없으면 기본값 생성."""
    if (not BILLING_ENGINE_PARAMS_PATH.exists()) or BILLING_ENGINE_PARAMS_PATH.stat().st_size == 0:
        _safe_write_json(BILLING_ENGINE_PARAMS_PATH, BILLING_ENGINE_PARAMS_DEFAULT)
        return BILLING_ENGINE_PARAMS_DEFAULT.copy()
    data = _safe_read_json(BILLING_ENGINE_PARAMS_PATH, None)
    if not isinstance(data, dict):
        _safe_write_json(BILLING_ENGINE_PARAMS_PATH, BILLING_ENGINE_PARAMS_DEFAULT)
        return BILLING_ENGINE_PARAMS_DEFAULT.copy()
    # 누락 키 보정 (기본값으로 채움)
    def _merge(base, override):
        result = {}
        for k, v in base.items():
            if k == "_comment":
                result[k] = v
                continue
            if isinstance(v, dict):
                result[k] = _merge(v, override.get(k, {}))
            else:
                result[k] = override.get(k, v)
        return result
    return _merge(BILLING_ENGINE_PARAMS_DEFAULT, data)

def save_billing_engine_params(params: dict) -> None:
    _safe_write_json(BILLING_ENGINE_PARAMS_PATH, params)


# ===== 로그인 / 권한 유틸 =====
def require_login():
    return bool(session.get("logged_in"))


def current_role():
    return session.get("role", "admin")


# ===== create_user_config.py 실행기 =====
def run_create_user_config():
    """
    RouterOS Hotspot 사용자 목록 + usage_public.json 을 기반으로
    user_config.json 을 자동 동기화.
    """
    for script_path in CREATE_USER_CONFIG_CANDIDATES:
        if script_path.exists():
            try:
                result = subprocess.run(
                    ["python3", str(script_path)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                print(f"[INFO] create_user_config.py executed: {script_path}")
                if result.stdout:
                    print("[create_user_config stdout]")
                    print(result.stdout)
                if result.stderr:
                    print("[create_user_config stderr]")
                    print(result.stderr)
                return
            except subprocess.CalledProcessError as e:
                print(f"[ERROR] create_user_config failed ({script_path}): {e}")
                print("stdout:", e.stdout)
                print("stderr:", e.stderr)
            except Exception as e:
                print(f"[ERROR] run_create_user_config exception ({script_path}): {e}")

    print("[WARN] create_user_config.py not found in any candidate path")



# ⬇⬇⬇ 비밀번호 변경 API (RouterOS SSH 연동) ⬇⬇⬇
@app.route("/api/hotspot/change_password", methods=["OPTIONS"])
def hotspot_pw_options():
    # CORS 프리플라이트 응답
    return _add_cors(make_response(("", 204)))

"""
    반환 규칙:
      - 항상 HTTP 200
     - body: { "ok": true }  또는  { "ok": false, "reason": "..." }
    → 프론트에서 body.ok 기준으로만 성공/실패 판단.
"""
@app.route("/api/hotspot/change_password", methods=["POST"])
def hotspot_change_password():

    # 🔹 1. 입력 파라미터
    user = (request.form.get("user") or "").strip()
    cur  = (request.form.get("current_password") or "").strip()
    newp = (request.form.get("new_password") or "").strip()   # HTML 에서 입력한 원본 비번 그대로 보관
    # not user or not cur or
    if not user or not newp:
        resp = jsonify({"ok": False, "reason": "Missing fields."})
        return _add_cors(resp), 200


    # 🔹 2. 정책 검사 (길이/문자)
    ok, reason = _validate_new_password(newp)
    if not ok:
        resp = jsonify({"ok": False, "reason": reason })
        return _add_cors(resp), 200

    # 🔹 3. 현재 비번 확인 및 추출/저장
    ver_cmd = f'/ip hotspot user print detail where name="{user}"'
    out_ver, err_ver = _run_ssh_command(ver_cmd)
    # 확인한 비번을 router_pw 로 저장
    m = re.search(r'password="([^"]+)"', out_ver)
    router_pw = m.group(1) if m else None
    # 사용자가 입력한 현재비번을 escape한 비번값으로 따로 저장
    esc_cur = _escape_password(cur)
    if err_ver:
        resp = jsonify({"ok": False, "reason": f"RouterOS error: {err_ver}"})
        return _add_cors(resp), 200

    # router_pw가 비어있을 수 도 있으므로 주석처리함.
    #if not router_pw:
    #    resp = jsonify({
    #        "ok": False,
    #        "reason": "User not found or password not readable.",
    #    })
    #    return _add_cors(resp), 200

    # 🔹 4. Current password 를 *치환/비치환 모두* 비교
    if (router_pw != cur) and (router_pw != esc_cur):
        resp = jsonify({
            "ok": False,
            "reason": "Current password does not match.",
            # 디버그용: RouterOS에서 읽은 실제 현재 비번 + 사용자가 입력한 값
            "router_current_password": router_pw,
            "provided_current_password": cur,
        })
        return _add_cors(resp), 200


    # 🔹 5. 변경 수행
    esc_new = _escape_password(newp)   # RouterOS에 들어가는 값
    set_cmd = f'/ip hotspot user set {user} password="{esc_new}"'
    out_set, err_set = _run_ssh_command(set_cmd)
    if err_set:
        resp = jsonify({"ok": False, "reason": f"SET error: {err_set}"})
        return _add_cors(resp), 200

    # 마지막 검증단계 전에는 잠시 대기 (RouterOS 반영 지연 대비)
    time.sleep(0.5)

    # 🔹 6. VERIFY 바꾸었다면 검증필요
        # Router 로부터 현재 비번 호출/저장
    ver_cmd = f'/ip hotspot user print detail where name="{user}"'
    out_ver, err_ver = _run_ssh_command(ver_cmd)
    
    if err_ver: # 오류 발생 시 resp에 저장만 하고 통과하는 것이 아니라,
        resp = jsonify({"ok": False, "reason": f"RouterOS verify error: {err_ver}"})
        # API를 종료하여 프론트엔드에 오류를 명확히 전달해야 함.
        return _add_cors(resp), 200

        # 오류없이 여기까지 통과했다면, 호출항 정보중 password만 추출 & 저장
    m = re.search(r'password="([^"]+)"', out_ver)
    router_pw = m.group(1) if m else "(not found)"
    

    # 🔹 7. New password 를 *(치환/비치환 모두)* Router값과 비교
    if (router_pw != newp) and (router_pw != esc_new):
        resp = jsonify({
            "ok": False,
            "reason": "New password does not match as Router.",
            # 디버그용: RouterOS에서 읽은 실제 현재 비번 + 사용자가 입력한 값
            "router_current_password": router_pw,
            "provided_current_password": cur,
        })
        return _add_cors(resp), 200


    # ✅ 여기까지 왔으면 RouterOS 비번이 실제로 newp (또는 esc_new) 로 반영된 상태
    resp = jsonify({"ok": True})
    return _add_cors(resp), 200
    # ⬆⬆⬆ 비밀번호 변경 API 끝 ⬆⬆⬆


# ===== 라우팅 =====
@app.route("/")
def index():
    if not require_login():
        return redirect("/login")
    return redirect("/manager")


@app.route("/login", methods=["GET", "POST"])
def login():
    error_msg = None

    if request.method == "POST":
        pw = request.form.get("password", "")
        admin_pw = load_admin_password()
        role = None

        if pw == SUPER_ADMIN_PASSWORD:
            role = "superadmin"
        elif admin_pw and pw == admin_pw:
            role = "admin"

        if role:
            session["logged_in"] = True
            session["role"] = role
            return redirect("/manager")
        else:
            error_msg = "비밀번호가 올바르지 않습니다."

    return render_template("login.html", error=error_msg)


@app.route("/manager/login", methods=["POST"])
def login_compat():
    return login()


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


#
@app.route("/archive_billing")
def archive_billing_page():
    if not require_login():
        return redirect("/login")
    return render_template("archive_billing.html", role=current_role())


# ===== /api/whoami: 현재 로그인 역할 반환 =====
@app.route("/api/whoami", methods=["GET"])
def api_whoami():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"role": current_role()})


@app.route("/manager")
def manager():
    if not require_login():
        return redirect("/login")

    usage_json = load_usage_public()
    user_cfg = load_user_config()
    settings = load_billing_settings()

    users_cfg = user_cfg.get("users", [])

    for u in users_cfg:
        display_name = u.get("display_name", "")
        role = u.get("role", "crew")
        if isinstance(display_name, str):
            if "Public" in display_name and role != "public":
                u["role"] = "public"
            elif "Apprentice_" in display_name and role != "apprentice":
                u["role"] = "apprentice"

        name = u.get("user", "")
        if isinstance(name, str) and ("default" in name.lower()):
            u["role"] = "public"

    total_users = len(users_cfg)

    raw_period = settings.get("billing_period_start_utc") or usage_json.get("period_start_utc")
    period_start = _iso_to_display_str(raw_period) if raw_period else "N/A"

    raw_last = usage_json.get("last_updated_utc")
    last_updated = _iso_to_display_str(raw_last) if raw_last else "N/A"

    role = current_role()

    return render_template(
        "manager.html",
        total_users=total_users,
        last_updated_utc=last_updated,
        period_start_utc=period_start,
        role=role,
    )


# ===== /api/users: 관리자 화면에서 보는 사용자 리스트 =====
@app.route("/api/users")
def api_users():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    user_cfg = load_user_config()
    usage_public = load_usage_public()
    usage_override = {"users_by_name": {}}

    usage_bytes_map = build_usage_bytes_map(usage_public, usage_override, user_cfg)

    result_users = []

    for u in user_cfg.get("users", []):
        name = u.get("user")
        if not name:
            continue

        display_name = u.get("display_name", name)
        role = u.get("role", "crew")
        limit_gb = float(u.get("limit_gb", 100.0))
        personal_prepaid = float(u.get("personal_prepaid", 0.0))
        base_fee_override = u.get("base_fee_override")

        # display_name 문자열 규칙으로 role 보정(기존 동작 유지)
        if isinstance(display_name, str):
            if "Public" in display_name:
                role = "public"
            elif "Apprentice_" in display_name:
                role = "apprentice"
                
        # ✅ 추가: username에 default 포함 시 public
        if isinstance(name, str) and ("default" in name.lower()):
            role = "public"

        # usage bytes
        usage_bytes = int(usage_bytes_map.get(name, 0)) if name in usage_bytes_map else 0
        usage_gb = usage_bytes / (1024 ** 3)

        # offset bytes (usage_public.users_by_name에서)
        offset_bytes = 0
        info2 = usage_public.get("users_by_name", {}).get(name)
        if isinstance(info2, dict):
            try:
                offset_bytes = int(info2.get("_offset_bytes", 0))
            except Exception:
                offset_bytes = 0

        result_users.append({
            "user": name,
            "display_name": display_name,
            "role": role,
            "limit_gb": limit_gb,
            "personal_prepaid": personal_prepaid,
            "base_fee_override": base_fee_override,
            "usage_bytes": usage_bytes,
            "usage_gb": round(usage_gb, 4),
            "_offset_bytes": offset_bytes,
        })

    return jsonify({
        "period_start_utc": usage_public.get("period_start_utc"),
        "last_updated_utc": usage_public.get("last_updated_utc"),
        "users": result_users,
    })


# ===== /api/users/refresh: RouterOS 기준 user_config 재생성 =====
@app.route("/api/users/refresh", methods=["POST"])
def api_users_refresh():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    # ★ 수정: superadmin 제한 제거 → 로그인된 admin / superadmin 모두 실행 가능
    run_create_user_config()
    return jsonify({"status": "ok"})


# ===== /api/monthly_usage: Hotspot status.html 에서 개별 사용자 월간 사용량 조회 =====
@app.route("/api/monthly_usage")
def api_monthly_usage():
    username = (request.args.get("user") or "").strip()
    if not username:
        resp = jsonify({"error": "missing user"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 400

    usage_public = load_usage_public()
    user_cfg = load_user_config()

    used_bytes = 0
    users_by_name = usage_public.get("users_by_name", {}) or {}
    info = users_by_name.get(username)
    if isinstance(info, dict):
        try:
            used_bytes = int(info.get("total_bytes", 0))
        except Exception:
            used_bytes = 0

    limit_gb = None
    for u in user_cfg.get("users", []):
        if u.get("user") == username:
            try:
                limit_gb = float(u.get("limit_gb"))
            except (TypeError, ValueError):
                limit_gb = None
            break

    offset_bytes = 0
    if isinstance(info, dict):
        try:
            offset_bytes = int(info.get("_offset_bytes", 0))
        except Exception:
            offset_bytes = 0


    resp = jsonify(
        {
            "user": username,
            "total_bytes": used_bytes,
            "_offset_bytes": offset_bytes,   # ✅ 추가
            "limit_gb": limit_gb,
            "period_start_utc": usage_public.get("period_start_utc"),
            "last_updated_utc": usage_public.get("last_updated_utc"),
        }
    )
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


##일별 그래프용 /api/daily_usage 2026-02-15수정첨부함.
from datetime import timedelta  # (상단 import에 없으면 추가되어도 OK)

def _parse_ymd(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()

def _daterange(d0, d1):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)

def _read_daily_effective_bytes_for_user(path: Path, username: str):
    """
    daily archive 파일 1개에서 해당 user의 effective bytes를 뽑아 반환.
    effective_bytes = total_bytes + _offset_bytes
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    # 케이스 A: {"users":[...]}
    users = raw.get("users")

    # 케이스 B: {"data":{"users":[...]}} (혹시 감싸진 형태 대비)
    if not isinstance(users, list):
        data = raw.get("data")
        if isinstance(data, dict):
            users = data.get("users")

    if not isinstance(users, list):
        return None

    for u in users:
        if not isinstance(u, dict):
            continue

        name = (u.get("user") or u.get("name") or "").strip()
        if name != username:
            continue

        try:
            total = int(u.get("total_bytes", 0) or 0)
        except Exception:
            total = 0

        try:
            off = int(u.get("_offset_bytes", 0) or 0)
        except Exception:
            off = 0

        return total + off

    return None


@app.route("/api/daily_usage")
def api_daily_usage():
    """
    GET /api/daily_usage?user=user96&start=2026-02-01&end=2026-02-13
    return: { ok:true, series:[{date,daily_bytes,cumulative_bytes,missing},...] }
    """
    username = (request.args.get("user") or "").strip()
    start_s  = (request.args.get("start") or "").strip()
    end_s    = (request.args.get("end") or "").strip()

    if not username or not start_s or not end_s:
        resp = jsonify({"ok": False, "reason": "missing user/start/end"})
        return _add_cors(resp), 200

    try:
        d0 = _parse_ymd(start_s)
        d1 = _parse_ymd(end_s)
        if d1 < d0:
            d0, d1 = d1, d0
    except Exception:
        resp = jsonify({"ok": False, "reason": "invalid date format (YYYY-MM-DD)"})
        return _add_cors(resp), 200

    # 너무 큰 범위 제한 (안전장치)
    if (d1 - d0).days > 370:
        resp = jsonify({"ok": False, "reason": "range too large (max 370 days)"})
        return _add_cors(resp), 200

    series = []
    prev_eff = None
    cum = 0

    for d in _daterange(d0, d1):
        ymd = d.strftime("%Y-%m-%d")
        fn = f"usage_daily_{ymd}.json"
        path = DAILY_ARCHIVES_DIR / fn

        eff = _read_daily_effective_bytes_for_user(path, username) if path.exists() else None

        if eff is None:
            series.append({
                "date": ymd,
                "effective_bytes": None,
                "daily_bytes": 0,
                "cumulative_bytes": cum,
                "missing": True,
            })
            continue

        if prev_eff is None:
            daily = 0
        else:
            daily = eff - prev_eff
            if daily < 0:
                daily = 0

        cum += daily
        prev_eff = eff

        series.append({
            "date": ymd,
            "effective_bytes": eff,
            "daily_bytes": daily,
            "cumulative_bytes": cum,
            "missing": False,
        })

    resp = jsonify({
        "ok": True,
        "user": username,
        "start": start_s,
        "end": end_s,
        "series": series
    })
    return _add_cors(resp), 200


# ===== /api/users/save: 관리자 화면에서 수정한 내용 저장 =====
@app.route("/api/users/save", methods=["POST"])
def api_users_save():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    users_in = data.get("users", [])

    if not isinstance(users_in, list):
        return jsonify({"error": "invalid payload"}), 400

    existing_cfg = load_user_config()
    old_cfg_map = {u.get("user"): u for u in existing_cfg.get("users", [])}

    new_cfg_users = []

    for u in users_in:
        name = u.get("user")
        if not name:
            continue

        old = old_cfg_map.get(name, {})

        new_entry = {
            "user": name,
            "display_name": u.get("display_name", old.get("display_name", name)),
            "limit_gb": float(u.get("limit_gb", old.get("limit_gb", 100.0))),
            "personal_prepaid": float(u.get("personal_prepaid", old.get("personal_prepaid", 0.0))),
            "role": u.get("role", old.get("role", "crew")),
            "base_fee_override": u.get("base_fee_override", old.get("base_fee_override", None)),
            "comment": u.get("comment", old.get("comment", "")),
        }
        new_cfg_users.append(new_entry)

    _safe_write_json(USER_CONFIG_PATH, {"users": new_cfg_users})

    if current_role() == "superadmin":
        raw_usage = _safe_read_json(
            USAGE_PUBLIC_PATH,
            {"period_start_utc": None, "last_updated_utc": None, "users": []},
        )
        users_list = raw_usage.get("users", [])
        user_map = {e.get("user"): e for e in users_list if e.get("user")}

        for u in users_in:
            name = u.get("user")
            if not name:
                continue

            # ✅ 1) UI에서 계산한 _offset_bytes가 오면 그대로 저장 (우선권)
            if "_offset_bytes" in u:
                try:
                    new_off = int(u.get("_offset_bytes") or 0)
                except Exception:
                    new_off = 0

                entry = user_map.get(name)
                if entry is None:
                    entry = {
                        "user": name,
                        "total_bytes": 0,
                        "_last_raw_bytes": 0,
                        "_offset_bytes": new_off,
                    }
                    users_list.append(entry)
                    user_map[name] = entry
                else:
                    entry["_offset_bytes"] = new_off
                continue

            # ✅ 2) (호환) usage_bytes가 오면 raw(total_bytes) 대비 offset을 계산해서 저장
            if "usage_bytes" not in u:
                continue

            try:
                new_bytes = int(u.get("usage_bytes") or 0)
            except Exception:
                new_bytes = 0

            entry = user_map.get(name)
            if entry is None:
                entry = {
                    "user": name,
                    "total_bytes": new_bytes,
                    "_last_raw_bytes": new_bytes,
                    "_offset_bytes": 0,   # ✅ 추가
                }
                users_list.append(entry)
                user_map[name] = entry

            else:
                # ✅ raw total은 건드리지 않고, offset만 계산해서 저장
                try:
                    raw_total = int(entry.get("total_bytes", 0) or 0)
                except Exception:
                    raw_total = 0

                entry["_offset_bytes"] = int(new_bytes - raw_total)
                # entry["total_bytes"] / entry["_last_raw_bytes"]는 변경하지 않음
        raw_usage["users"] = users_list
        raw_usage["last_updated_utc"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        _safe_write_json(USAGE_PUBLIC_PATH, raw_usage)

    return jsonify({"status": "ok"})


# ===== /api/billing: 최근 요금 정산 결과 조회 =====
@app.route("/api/billing")
def api_billing():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    billing = load_billing_result()
    return jsonify(billing)


# ===== /api/settings/billing_period: 집계 기준 시작 시간 =====
@app.route("/api/settings/billing_period", methods=["GET", "POST"])
def api_billing_period():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    if request.method == "GET":
        settings = load_billing_settings()
        return jsonify(settings)

    data = request.get_json(silent=True) or {}
    new_val = data.get("billing_period_start_utc")

    if not isinstance(new_val, str) or "T" not in new_val or not new_val.endswith("Z"):
        return jsonify({"error": "invalid format"}), 400

    settings = load_billing_settings()
    settings["billing_period_start_utc"] = new_val
    save_billing_settings(settings)

    return jsonify({"status": "ok"})


# ===== /api/settings/billing_start_day: 기준일(day) 숫자 읽기/쓰기 =====
@app.route("/api/settings/billing_start_day", methods=["GET", "POST"])
def api_billing_start_day():
    """
    billing_start_day.txt 에 저장된 '매월 기준일(1~31)' 숫자를 읽거나 갱신한다.
    GET  → {"billing_start_day": <int>}
    POST → {"billing_start_day": <int>}  (1~31 범위 검증)

    collector가 billing_settings.json 안에 billing_start_day 필드도 갱신하므로,
    GET 시 txt 파일이 없으면 billing_settings.json 의 해당 필드를 fallback으로 사용한다.
    """
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    if request.method == "GET":
        # 1순위: billing_start_day.txt
        if BILLING_START_DAY_PATH.exists():
            try:
                day = int(BILLING_START_DAY_PATH.read_text(encoding="utf-8").strip())
                if 1 <= day <= 31:
                    return jsonify({"billing_start_day": day})
            except Exception:
                pass
        # 2순위: billing_settings.json 내 billing_start_day 필드
        settings = load_billing_settings()
        day_fallback = settings.get("billing_start_day")
        if day_fallback and isinstance(day_fallback, int) and 1 <= day_fallback <= 31:
            return jsonify({"billing_start_day": day_fallback})
        return jsonify({"billing_start_day": None})

    # POST
    data = request.get_json(silent=True) or {}
    day_val = data.get("billing_start_day")
    try:
        day_val = int(day_val)
    except (TypeError, ValueError):
        return jsonify({"error": "billing_start_day must be an integer 1~31"}), 400

    if not (1 <= day_val <= 31):
        return jsonify({"error": "billing_start_day out of range (1~31)"}), 400

    # billing_start_day.txt 저장
    try:
        BILLING_START_DAY_PATH.parent.mkdir(parents=True, exist_ok=True)
        BILLING_START_DAY_PATH.write_text(str(day_val), encoding="utf-8")
    except Exception as e:
        return jsonify({"error": f"write failed: {e}"}), 500

    # billing_settings.json 에도 동기화 (collector가 참조)
    try:
        settings = load_billing_settings()
        settings["billing_start_day"] = day_val
        save_billing_settings(settings)
    except Exception as e:
        app.logger.warning(f"billing_settings.json sync failed: {e}")

    return jsonify({"status": "ok", "billing_start_day": day_val})


# ===== /api/settings/billing_engine_params: 요금 계산 엔진 상수 =====
@app.route("/api/settings/billing_engine_params", methods=["GET", "POST"])
def api_billing_engine_params():
    """
    GET  → billing_engine_params.json 전체 반환
    POST → 상수 값 일부/전체 갱신 (superadmin 전용)

    요청 예시:
    {
      "manager": {"under_limit_deduction": 60, "over_limit_fixed_fee": 110},
      "crew":    {"over_limit_surcharge": 5, "lowest_usage_deduction": 5,
                  "under_half_threshold": 0.5, "under_half_deduction": 10},
      "apprentice": {"rate": 0.5}
    }
    """
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    if request.method == "GET":
        return jsonify(load_billing_engine_params())

    # POST: superadmin 전용
    if current_role() != "superadmin":
        return jsonify({"error": "superadmin only"}), 403

    data = request.get_json(silent=True) or {}

    # 숫자 범위 검증
    def _check_positive(v, name):
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{name} must be a non-negative number (got {v!r})")

    def _check_rate(v, name):
        if not isinstance(v, (int, float)) or not (0 < v <= 1):
            raise ValueError(f"{name} must be between 0 (exclusive) and 1 (inclusive) (got {v!r})")

    try:
        mg = data.get("manager", {})
        if "under_limit_deduction" in mg:
            _check_positive(mg["under_limit_deduction"], "manager.under_limit_deduction")
        if "over_limit_fixed_fee" in mg:
            _check_positive(mg["over_limit_fixed_fee"], "manager.over_limit_fixed_fee")

        cr = data.get("crew", {})
        if "over_limit_surcharge" in cr:
            _check_positive(cr["over_limit_surcharge"], "crew.over_limit_surcharge")
        if "lowest_usage_deduction" in cr:
            _check_positive(cr["lowest_usage_deduction"], "crew.lowest_usage_deduction")
        if "under_half_threshold" in cr:
            v = cr["under_half_threshold"]
            if not isinstance(v, (int, float)) or not (0 < v < 1):
                raise ValueError(f"crew.under_half_threshold must be between 0 and 1 (got {v!r})")
        if "under_half_deduction" in cr:
            _check_positive(cr["under_half_deduction"], "crew.under_half_deduction")

        ap = data.get("apprentice", {})
        if "rate" in ap:
            _check_rate(ap["rate"], "apprentice.rate")

    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # 기존 값에 deep-merge
    params = load_billing_engine_params()
    for section in ("manager", "crew", "apprentice"):
        if section in data and isinstance(data[section], dict):
            if section not in params or not isinstance(params[section], dict):
                params[section] = {}
            params[section].update(data[section])

    save_billing_engine_params(params)
    return jsonify({"status": "ok", "params": params})


# ===== 요금 정산 엔진 =====
def _run_billing_engine(usage_source, user_cfg, E1, F16, F21, billing_period_start_utc, engine_params=None):
    usage_map = {}
    for u in usage_source.get("users", []):
        name = u.get("user")
        if not name:
            continue

        if "total_bytes" in u:
            # ✅ raw(total_bytes) + offset(_offset_bytes) 를 정산 기준으로 사용
            try:
                b = int(u.get("total_bytes", 0) or 0)
            except Exception:
                b = 0

            try:
                off = int(u.get("_offset_bytes", 0) or 0)
            except Exception:
                off = 0

            b_eff = b + off

            # (선택) 정말 최소 안전장치가 필요하면 아래 2줄만 켜세요.
            # if b_eff < 0:
            #     b_eff = 0

            gb = b_eff / (1024 ** 3)

        else:
            gb_val = u.get("total_gb", u.get("usage_gb", 0.0))
            try:
                gb = float(gb_val or 0.0)
            except Exception:
                gb = 0.0

        usage_map[name] = gb

    raw_users = []
    for u in user_cfg.get("users", []):
        name = u.get("user")
        if not name:
            continue

        role = u.get("role", "crew")
        if role == "public":
            continue

        display_name = u.get("display_name", name)
        limit_gb = float(u.get("limit_gb", 100.0))
        personal_prepaid = float(u.get("personal_prepaid", 0.0))
        base_fee_override = u.get("base_fee_override", None)

        usage_gb = usage_map.get(name, 0.0)

        raw_users.append({
            "name": name,
            "display_name": display_name,
            "role": role,
            "limit_gb": limit_gb,
            "usage_gb": usage_gb,
            "personal_prepaid": personal_prepaid,
            "base_fee_override": base_fee_override,
        })

    if not raw_users:
        return {
            "summary": {"target_total": F16},
            "users": [],
            "params": {"base_amount_E1": E1, "target_total_F16": F16},
        }

    for u in raw_users:
        u["D"] = (u["usage_gb"] / u["limit_gb"]) if u["limit_gb"] > 0 else 0.0

    min_D = min(u["D"] for u in raw_users)

    for u in raw_users:
        D = u["D"]
        base = E1

        # billing_engine_params.json 에서 상수 로드 (없으면 기본값)
        _ep = engine_params or {}
        _mg = _ep.get("manager", {})
        _cr = _ep.get("crew", {})
        _ap = _ep.get("apprentice", {})

        mg_under_ded   = float(_mg.get("under_limit_deduction", 60))
        mg_over_fixed  = float(_mg.get("over_limit_fixed_fee",   110))
        cr_over_sur    = float(_cr.get("over_limit_surcharge",   5))
        cr_low_ded     = float(_cr.get("lowest_usage_deduction", 5))
        cr_half_thr    = float(_cr.get("under_half_threshold",   0.5))
        cr_half_ded    = float(_cr.get("under_half_deduction",   10))
        ap_rate        = float(_ap.get("rate",                   0.5))

        if u["role"] == "manager":
            base = (E1 - mg_under_ded) if D < 1 else mg_over_fixed
        else:
            if D >= 1:
                base = E1 + cr_over_sur
            elif D == min_D:
                base = E1 - cr_low_ded
            elif D < cr_half_thr:
                base = E1 - cr_half_ded

        if u["role"] == "apprentice":
            base *= ap_rate

        if u["base_fee_override"] is not None:
            try:
                base = float(u["base_fee_override"])
            except Exception:
                pass

        u["base_fee"] = float(round(base, 2))

    E_total = sum(u["base_fee"] for u in raw_users)
    usage_sum = sum(u["usage_gb"] for u in raw_users)
    sum_personal = sum(u["personal_prepaid"] for u in raw_users)

    remaining_pool = max(0, F16 - E_total - F21 - sum_personal)

    for u in raw_users:
        if usage_sum > 0:
            var_fee = remaining_pool * (u["usage_gb"] / usage_sum)
            u["variable_fee"] = float(round(var_fee))
        else:
            u["variable_fee"] = 0.0

        final = u["base_fee"] + u["variable_fee"] - u["personal_prepaid"]
        u["final_fee"] = float(round(final, 2))

    summary = {
        "E_total": float(round(E_total, 2)),
        "usage_sum_gb": float(round(usage_sum, 3)),
        "remaining_pool": float(round(remaining_pool, 2)),
        "sum_variable": sum(u["variable_fee"] for u in raw_users),
        "sum_final": sum(u["final_fee"] for u in raw_users),
        "sum_personal_prepaid": float(round(sum_personal, 2)),
        "check_total_collected": float(
            round(sum(u["final_fee"] for u in raw_users) + F21 + sum_personal, 2)
        ),
        "target_total": float(round(F16, 2)),
    }

    return {
        "period_start_utc": usage_source.get("period_start_utc"),
        "last_updated_utc": usage_source.get("last_updated_utc"),
        "params": {
            "base_amount_E1": E1,
            "target_total_F16": F16,
            "common_prepaid_F21": F21,
            "billing_period_start_utc": billing_period_start_utc,
        },
        "summary": summary,
        "users": raw_users,
    }


# ===== /api/billing/run: 요금 정산 실행 =====
@app.route("/api/billing/run", methods=["POST"])
def api_billing_run():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        E1 = float(data.get("base_amount_E1"))
        F16 = float(data.get("target_total_F16"))
        F21 = float(data.get("common_prepaid_F21", 0.0))
    except (TypeError, ValueError):
        return jsonify({"error": "E1/F16/F21 invalid"}), 400

    usage_public = load_usage_public()
    user_cfg = load_user_config()
    settings = load_billing_settings()

    source = usage_public

    engine_params = load_billing_engine_params()

    result = _run_billing_engine(
        source,
        user_cfg,
        E1,
        F16,
        F21,
        settings.get("billing_period_start_utc"),
        engine_params=engine_params,
    )

    _safe_write_json(BILLING_RESULT_PATH, result)
    return jsonify(result)


# ===== [추가] 모든 사용자 사용량 수동 초기화 API =====
@app.route("/api/usage/reset_all", methods=["POST"])
def api_usage_reset_all():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    try:
        meta = _reset_usage_public_file_all_users()
        app.logger.info(f"Admin reset all usage (Pi) at {meta['period_start_utc']}")
        return jsonify({"message": "Pi usage_public.json reset OK", **meta}), 200

    except Exception as e:
        app.logger.error(f"Reset Usage Error: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ---- archive_billing.html 용 ----
## 과거 data 로딩해서 작업하기 위한 로직들
_ALLOWED_ARCHIVE_KIND = {"manual", "daily", "monthly"}
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

def _archive_dir(kind: str) -> Path:
    if kind == "manual":
        return MANUAL_ARCHIVES_DIR
    if kind == "daily":
        return DAILY_ARCHIVES_DIR
    if kind == "monthly":
        return MONTHLY_ARCHIVES_DIR
    raise ValueError("invalid kind")

def _validate_kind_and_filename(kind: str, filename: str) -> tuple[bool, str]:
    if kind not in _ALLOWED_ARCHIVE_KIND:
        return False, "invalid kind"
    if not filename or not _SAFE_FILENAME_RE.match(filename):
        return False, "invalid filename"
    if ".." in filename or "/" in filename or "\\" in filename:
        return False, "invalid filename"
    return True, ""




# ===== [TEST] RouterOS Hotspot user counters 전체 리셋 API =====
@app.route("/api/routeros/reset_counters_all", methods=["POST"])
def api_routeros_reset_counters_all():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    try:
        # RouterOS에서 모조리 0으로 리셋
        cmd = ':local actList [/ip hotspot active find]; :if ([:len $actList] > 0) do={ /ip hotspot active remove $actList; }; :delay 1; /ip hotspot user reset-counters [find]; :foreach b in=[/ip hotspot ip-binding find where (comment~"_IoT_")] do={ :local mac [/ip hotspot ip-binding get $b mac-address]; :if ([:len $mac] > 0) do={ :local h [/ip hotspot host find where mac-address=$mac]; :if ([:len $h] > 0) do={ /ip hotspot host remove $h; }; }; }; :put "RESET_DONE"'

        out, err = _run_ssh_command(cmd)

        if err:
            app.logger.error(f"[RouterOS reset_counters_all] stderr: {err}")
            return jsonify({"error": err}), 500
        
        # ✅ RouterOS 리셋 성공 → Pi 누적값(total/raw_last)도 같이 리셋
        meta = _reset_usage_public_file_all_users()

        return jsonify({
            "message": "RESET OK (RouterOS + Pi usage_public.json)",
            "stdout": out,
            **meta,
        }), 200

    except Exception as e:
        app.logger.error(f"[RouterOS reset_counters_all] exception: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/archives/list", methods=["GET"])
def api_archives_list():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    kind = (request.args.get("kind") or "").strip()
    if kind not in _ALLOWED_ARCHIVE_KIND:
        return jsonify({"error": "invalid kind"}), 400

    d = _archive_dir(kind)
    d.mkdir(parents=True, exist_ok=True)

    files = sorted([p.name for p in d.glob("*.json")], reverse=True)
    return jsonify({"kind": kind, "files": files})

@app.route("/api/archives/load", methods=["GET"])
def api_archives_load():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    kind = (request.args.get("kind") or "").strip()
    filename = (request.args.get("filename") or "").strip()

    ok, msg = _validate_kind_and_filename(kind, filename)
    if not ok:
        return jsonify({"error": msg}), 400

    path = _archive_dir(kind) / filename
    if not path.exists():
        return jsonify({"error": "file not found"}), 404

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return jsonify({"kind": kind, "filename": filename, "data": data})
    except Exception as e:
        return jsonify({"error": f"read failed: {e}"}), 500



# ===== [최종 수정] 수동 스냅샷 저장 API =====
@app.route("/api/snapshot/save", methods=["POST"])
def api_snapshot_save():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    try:
        user_cfg = load_user_config()
        usage_public = load_usage_public()

        users_min = []
        usage_by_name = usage_public.get("users_by_name", {}) or {}

        for u in user_cfg.get("users", []):
            name = u.get("user")
            if not name:
                continue

            role = u.get("role", "crew")
            limit_gb = float(u.get("limit_gb", 100.0))
            personal_prepaid = float(u.get("personal_prepaid", 0.0))
            base_fee_override = u.get("base_fee_override", None)

            info = usage_by_name.get(name) if isinstance(usage_by_name, dict) else None
            total_bytes = 0
            offset_bytes = 0
            if isinstance(info, dict):
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

        now = datetime.utcnow()
        snapshot_data = {
            "snapshot_type": "manual",
            "timestamp_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period_start_utc": usage_public.get("period_start_utc"),
            "last_updated_utc": usage_public.get("last_updated_utc"),
            "users": users_min,
        }

        MANUAL_ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_snapshot.json"
        save_path = MANUAL_ARCHIVES_DIR / filename

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(snapshot_data, f, indent=2, ensure_ascii=False)

        return jsonify({"message": "success", "filename": filename}), 200

    except Exception as e:
        app.logger.error(f"Snapshot Save Error: {str(e)}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)