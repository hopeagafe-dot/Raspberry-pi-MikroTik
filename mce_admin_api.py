#!/usr/bin/env python3
"""
mce_admin_api.py

구조 변경 사항 (2026-03):
  - billing_settings.json 완전 제거 (load/save/route 모두)
  - user_config.json 완전 제거 → usage_public.json에서 직접 읽음
  - create_user_config.py 호출 제거
  - /api/settings/billing_period 라우트 제거
  - /api/users/refresh 라우트 제거
  - usage_public.json 포맷: {last_updated_utc, users: [{user, comment, limit_gb, total_bytes, _last_raw_bytes, _offset_bytes}]}
  - period_start_utc 는 billing_date.json의 last_reset_yyyymmdd에서 계산
  - limit_gb 는 RouterOS에서 읽으므로 /api/users/save에서 변경 차단
  - _infer_role(name, comment) 헬퍼로 comment 기반 역할 추론
"""

import re
import string
import json
import os
import time
import paramiko

from pathlib import Path
from datetime import datetime, timezone, timedelta

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    redirect,
    session,
    make_response,
    send_from_directory,
)


# ---- CORS ----
ALLOWED_ORIGIN_HOTSPOT = "http://login.mce-ship.local"
ALLOWED_ORIGIN_IP      = "http://192.168.88.1"
ALLOWED_ORIGINS = [ALLOWED_ORIGIN_IP, ALLOWED_ORIGIN_HOTSPOT]

def _add_cors(resp):
    origin = request.headers.get("Origin")
    if origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
    else:
        resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN_IP
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# RouterOS SSH
ROUTER_HOST = "192.168.88.1"
ROUTER_USER = "admin"

def _load_router_password() -> str:
    try:
        p = os.path.expanduser("~/router_password.txt")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""

ROUTER_PASS = _load_router_password()
SSH_PORT = 22


def _run_ssh_command(cmd: str):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        ROUTER_HOST, port=SSH_PORT, username=ROUTER_USER, password=ROUTER_PASS,
        timeout=10, allow_agent=False, look_for_keys=False,
    )
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode("utf-8", errors="ignore").strip()
    err = stderr.read().decode("utf-8", errors="ignore").strip()
    ssh.close()
    return out, err


def _escape_password(pw: str):
    pw = pw.replace("\\", "\\\\")  # \ → \\
    pw = pw.replace("\"", "\\\"")  # " → \"
    pw = pw.replace("$", "\\$")    # $ → \$
    pw = pw.replace("`", "\\`")    # ` → \`
    pw = pw.replace(";", "\\;")    # ; → \;
    pw = pw.replace("{", "\\{")    # { → \{
    pw = pw.replace("}", "\\}")    # } → \}
    return pw


def _validate_new_password(pw: str):
    """
    비밀번호 유효성 검증
    - 길이: 4-64자
    - 허용: 영문, 숫자, !@#$%^&*()-_=+[],.?/
    - 차단: 위험한 특수문자
    """
    if not (4 <= len(pw) <= 64):
        return False, "Password must be 4-64 characters."
    
    # 허용된 문자만 사용했는지 검사 ($ 포함)
    if not re.match(r'^[a-zA-Z0-9!@#$%^&*()\-_=+\[\],.?/]+$', pw):
        return False, "Password can only contain: letters, numbers, and !@#$%^&*()-_=+[],.?/"
    
    # 위험한 문자들 명시적 차단 ($ 제외 - 이스케이프 처리로 안전하게 사용 가능)
    dangerous_chars = {
        '\\': 'backslash', '"': 'double quote',
        '`': 'backtick', ';': 'semicolon', '{': 'left brace',
        '}': 'right brace', '<': 'less than', '>': 'greater than',
        '|': 'pipe', "'": 'single quote', ' ': 'space',
    }
    
    found = [(ch, name) for ch, name in dangerous_chars.items() if ch in pw]
    if found:
        forbidden = ', '.join(f"'{ch}' ({name})" for ch, name in found)
        return False, f"Password cannot contain: {forbidden}"
    
    return True, ""


# ===== 기본 경로 설정 =====
BASE_DIR = Path.home()
DATA_DIR = BASE_DIR / "mcepi_data"
TEMPLATE_DIR = BASE_DIR / "Templates"

MANUAL_ARCHIVES_DIR  = DATA_DIR / "manual_archives"
DAILY_ARCHIVES_DIR   = DATA_DIR / "daily_archives"
MONTHLY_ARCHIVES_DIR = DATA_DIR / "monthly_archives"

USAGE_PUBLIC_PATH     = DATA_DIR / "usage_public.json"
USAGE_OVERRIDE_PATH   = DATA_DIR / "usage_override.json"
BILLING_RESULT_PATH   = DATA_DIR / "result_billing.json"
OVERWRITE_LOG_PATH    = DATA_DIR / "usage_overwrite.log"
BILLING_DATE_PATH     = DATA_DIR / "billing_date.json"
BILLING_ENGINE_PARAMS_PATH = DATA_DIR / "billing_engine_params.json"
ADMIN_PASSWORD_FILE   = BASE_DIR / "admin_password.txt"

SUPER_ADMIN_PASSWORD = "Star$625Link"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
app.secret_key = "MCE_Super_ADMIN"

# ★ 템플릿 파일 교체 후 재시작 없이도 즉시 반영
app.jinja_env.auto_reload = True
app.config["TEMPLATES_AUTO_RELOAD"] = True


# ===== 유틸 =====
def _safe_read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _safe_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def _ensure_json_file(path: Path, default: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if (not path.exists()) or path.stat().st_size == 0:
            _safe_write_json(path, default)
            return default
    except Exception:
        _safe_write_json(path, default)
        return default
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
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        if "T" in s:
            date_part, time_part = s.split("T", 1)
            return f"{date_part} {time_part[:5]}"
        return s
    except Exception:
        return s


# ===== billing_date.json =====
def _load_billing_date_raw() -> dict:
    with BILLING_DATE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("billing_date.json must be a JSON object")
    return data

def _get_billing_start_day() -> int:
    d = _load_billing_date_raw()
    day = d["billing_start_day"]
    if not isinstance(day, int):
        raise TypeError(f"billing_start_day must be int (got {type(day).__name__})")
    if not (1 <= day <= 31):
        raise ValueError(f"billing_start_day out of range 1~31 (got {day})")
    return day

def _calc_period_start_from_billing_date() -> str:
    """
    billing_date.json의 last_reset_yyyymmdd 에서 period_start_utc 계산.
    예: last_reset_yyyymmdd="20260301" → "2026-03-01T00:00:00Z"

    (레거시 호환)
    - last_reset_yyyymmdd 가 없으면 last_reset_yyyymm + billing_start_day 로 계산
    """
    try:
        import calendar as _cal
        bd = _load_billing_date_raw()

        last_reset_dd = (bd.get("last_reset_yyyymmdd") or "").strip()
        if len(last_reset_dd) == 8 and last_reset_dd.isdigit():
            y = int(last_reset_dd[:4])
            m = int(last_reset_dd[4:6])
            d = int(last_reset_dd[6:8])
            last_day = _cal.monthrange(y, m)[1]
            d = min(max(1, d), last_day)
            return f"{y:04d}-{m:02d}-{d:02d}T00:00:00Z"

        last_reset = (bd.get("last_reset_yyyymm") or "").strip()
        start_day = int(bd.get("billing_start_day", 1))
        if not last_reset or len(last_reset) != 6:
            return "-"
        y = int(last_reset[:4])
        m = int(last_reset[4:6])
        last_day = _cal.monthrange(y, m)[1]
        d = min(max(1, start_day), last_day)
        return f"{y:04d}-{m:02d}-{d:02d}T00:00:00Z"
    except Exception:
        return "-"


# ===== role 추론 헬퍼 (archive_billing.html 과 동일한 로직) =====
def _infer_role(name: str, comment: str) -> str:
    """comment/name 기반 역할 추론."""
    n = (name or "").lower()
    c = (comment or "").lower()
    if "default" in n:
        return "public"
    if "public" in n:
        return "public"
    if "manager" in n:
        return "manager"
    if "manager" in c:
        return "manager"
    if "apprentice" in c:
        return "apprentice"
    if "public" in c:
        return "public"
    return "crew"


# ===== usage_public.json =====
def load_usage_public():
    """
    usage_public.json을 읽어 comment/limit_gb 포함 반환.
    리턴 구조:
    {
      "last_updated_utc": str|None,
      "users": [...],
      "users_by_name": {name: {comment, limit_gb, total_bytes, _last_raw_bytes, _offset_bytes}}
    }
    """
    if (not USAGE_PUBLIC_PATH.exists()) or (USAGE_PUBLIC_PATH.stat().st_size == 0):
        _safe_write_json(USAGE_PUBLIC_PATH, {"last_updated_utc": None, "users": []})

    raw = _ensure_json_file(USAGE_PUBLIC_PATH, {"last_updated_utc": None, "users": []})

    by_name = {}
    for u in raw.get("users", []):
        name = u.get("user")
        if not name:
            continue
        by_name[name] = {
            "comment": u.get("comment", ""),
            "limit_gb": float(u.get("limit_gb", 100.0) or 100.0),
            "total_bytes": int(u.get("total_bytes", 0) or 0),
            "_last_raw_bytes": int(u.get("_last_raw_bytes", 0) or 0),
            "_offset_bytes": int(u.get("_offset_bytes", 0) or 0),
        }

    raw["users_by_name"] = by_name
    return raw


def _reset_usage_public_file_all_users() -> dict:
    """usage_public.json의 모든 사용자 total_bytes/_last_raw_bytes/_offset_bytes를 0으로 리셋."""
    raw = _ensure_json_file(USAGE_PUBLIC_PATH, {"last_updated_utc": None, "users": []})

    users = raw.get("users", [])
    if isinstance(users, list):
        for u in users:
            if not isinstance(u, dict):
                continue
            u["total_bytes"] = 0
            u["_last_raw_bytes"] = 0
            u["_offset_bytes"] = 0
            # comment, limit_gb 는 유지

    raw.pop("users_by_name", None)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw["last_updated_utc"] = now_iso
    raw["users"] = users

    _safe_write_json(USAGE_PUBLIC_PATH, raw)
    return {"last_updated_utc": now_iso}


def load_usage_override():
    raw = _safe_read_json(USAGE_OVERRIDE_PATH, {"last_updated_utc": None, "users": []})
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
            total_bytes = int(gb * (1000 ** 3))
        by_name[name] = {"total_bytes": total_bytes, "_last_raw_bytes": total_bytes}
    raw["users_by_name"] = by_name
    return raw


def build_usage_bytes_map(usage_public, usage_override):
    usage_bytes = {}
    for name, info in usage_public.get("users_by_name", {}).items():
        try:
            usage_bytes[name] = int(info.get("total_bytes", 0))
        except Exception:
            usage_bytes[name] = 0
    for name, info in usage_override.get("users_by_name", {}).items():
        try:
            usage_bytes[name] = int(info.get("total_bytes", 0))
        except Exception:
            usage_bytes[name] = 0
    return usage_bytes


def load_billing_result():
    return _safe_read_json(
        BILLING_RESULT_PATH,
        {"last_updated_utc": None, "params": {}, "summary": {}, "users": []},
    )

def load_admin_password():
    try:
        if ADMIN_PASSWORD_FILE.exists():
            text = ADMIN_PASSWORD_FILE.read_text(encoding="utf-8").strip()
            return text or None
        return None
    except Exception:
        return None


# ===== billing_engine_params.json =====
BILLING_ENGINE_PARAMS_DEFAULT = {
    "_comment": "요금 계산 엔진 상수 설정 파일.",
    "billing_inputs": {
        "base_amount_E1": 80.0,
        "target_total_F16": 1750.0,
        "common_prepaid_F21": 50.0
    },
    "manager": {"final_deduction": 30},
    "crew": {"over_limit_surcharge": 5, "lowest_usage_deduction": 5, "under_half_threshold": 0.5, "under_half_deduction": 10},
    "apprentice": {"rate": 0.5}
}

def load_billing_engine_params() -> dict:
    if (not BILLING_ENGINE_PARAMS_PATH.exists()) or BILLING_ENGINE_PARAMS_PATH.stat().st_size == 0:
        _safe_write_json(BILLING_ENGINE_PARAMS_PATH, BILLING_ENGINE_PARAMS_DEFAULT)
        return BILLING_ENGINE_PARAMS_DEFAULT.copy()
    data = _safe_read_json(BILLING_ENGINE_PARAMS_PATH, None)
    if not isinstance(data, dict):
        _safe_write_json(BILLING_ENGINE_PARAMS_PATH, BILLING_ENGINE_PARAMS_DEFAULT)
        return BILLING_ENGINE_PARAMS_DEFAULT.copy()
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


# ===== 로그인 =====
def require_login():
    return bool(session.get("logged_in"))

def current_role():
    return session.get("role", "admin")


# ===== 비밀번호 변경 API =====
@app.route("/api/hotspot/change_password", methods=["OPTIONS"])
def hotspot_pw_options():
    return _add_cors(make_response(("", 204)))

@app.route("/api/hotspot/change_password", methods=["POST"])
def hotspot_change_password():
    """사용자 비밀번호 변경 API"""
    # 1. 입력값 가져오기
    user = (request.form.get("user") or "").strip()
    cur  = (request.form.get("current_password") or "").strip()
    newp = (request.form.get("new_password") or "").strip()
    
    # 2. 필수 필드 검증
    if not user or not newp:
        return _add_cors(jsonify({"ok": False, "reason": "Missing required fields."})), 200
    
    # 3. Username 검증 (명령어 인젝션 방지)
    if not re.match(r'^[a-zA-Z0-9_\-]+$', user):
        app.logger.warning(f"Invalid username format attempted: {user}")
        return _add_cors(jsonify({"ok": False, "reason": "Invalid username format."})), 200
    
    # 4. 새 비밀번호 유효성 검증
    ok, reason = _validate_new_password(newp)
    if not ok:
        app.logger.info(f"Password validation failed for user '{user}': {reason}")
        return _add_cors(jsonify({"ok": False, "reason": reason})), 200
    
    # 5. RouterOS에서 현재 비밀번호 조회
    ver_cmd = f'/ip hotspot user print detail where name="{user}"'
    out_ver, err_ver = _run_ssh_command(ver_cmd)
    
    if err_ver:
        app.logger.error(f"RouterOS error while fetching user '{user}': {err_ver}")
        return _add_cors(jsonify({"ok": False, "reason": f"RouterOS error: {err_ver}"})), 200
    
    # 6. 비밀번호 필드 추출
    m = re.search(r'password="([^"]*)"', out_ver)
    if not m:
        app.logger.warning(f"User '{user}' not found or password field missing")
        return _add_cors(jsonify({"ok": False, "reason": "User not found."})), 200
    
    router_pw = m.group(1)
    
    # 7. 현재 비밀번호 검증 (원본 또는 이스케이프 버전 허용)
    esc_cur = _escape_password(cur)
    
    if (router_pw != cur) and (router_pw != esc_cur):
        app.logger.warning(f"Current password mismatch for user '{user}'")
        return _add_cors(jsonify({"ok": False, "reason": "Current password is incorrect."})), 200
    
    # 8. 새 비밀번호 설정
    esc_new = _escape_password(newp)
    set_cmd = f'/ip hotspot user set "{user}" password="{esc_new}"'
    
    out_set, err_set = _run_ssh_command(set_cmd)
    if err_set:
        app.logger.error(f"Failed to set password for user '{user}': {err_set}")
        return _add_cors(jsonify({"ok": False, "reason": f"Password change failed: {err_set}"})), 200
    
    # 9. 설정 확인 (RouterOS 반영 대기)
    time.sleep(1.0)  # RouterOS가 변경사항을 확실히 반영하도록 대기
    
    out_ver2, err_ver2 = _run_ssh_command(ver_cmd)
    if err_ver2:
        app.logger.error(f"Verification error for user '{user}': {err_ver2}")
        return _add_cors(jsonify({"ok": False, "reason": f"Verification failed: {err_ver2}"})), 200
    
    # 10. 새 비밀번호 확인
    m2 = re.search(r'password="([^"]*)"', out_ver2)
    if not m2:
        app.logger.error(f"Verification: password field not found for user '{user}'")
        return _add_cors(jsonify({"ok": False, "reason": "Verification failed."})), 200
    
    router_pw_new = m2.group(1)
    
    if (router_pw_new != newp) and (router_pw_new != esc_new):
        app.logger.error(f"Password verification failed for user '{user}'")
        return _add_cors(jsonify({"ok": False, "reason": "Password verification failed."})), 200
    
    # 11. 성공
    app.logger.info(f"✅ Password changed for user: {user}")
    return _add_cors(jsonify({"ok": True})), 200


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
        admin_id = request.form.get("admin_id", "").strip()
        pw = request.form.get("password", "").strip()
        
        # ID와 비밀번호 모두 확인
        admin_pw = load_admin_password()
        role = None
        
        if admin_id == "superadmin" and pw == SUPER_ADMIN_PASSWORD:
            role = "superadmin"
        elif admin_id == "admin" and admin_pw and pw == admin_pw:
            role = "admin"
        
        if role:
            session["logged_in"] = True
            session["role"] = role
            return redirect("/manager")
        else:
            if not admin_id:
                error_msg = "관리자 ID를 선택해주세요."
            elif not pw:
                error_msg = "비밀번호를 입력해주세요."
            else:
                error_msg = "ID 또는 비밀번호가 올바르지 않습니다."
    
    return render_template("login.html", error=error_msg)

@app.route("/manager/login", methods=["POST"])
def login_compat():
    return login()

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/archive_billing")
def archive_billing_page():
    if not require_login():
        return redirect("/login")
    return render_template("archive_billing.html", role=current_role())

@app.route("/all_user_graph")
def all_user_graph_page():
    if not require_login():
        return redirect("/login")
    return render_template("all_user_graph.html", role=current_role())

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

    # period_start_utc: billing_date.json의 last_reset_yyyymmdd에서 계산
    raw_period = _calc_period_start_from_billing_date()
    period_start = _iso_to_display_str(raw_period) if raw_period not in ("-", "") else "N/A"

    raw_last = usage_json.get("last_updated_utc")
    last_updated = _iso_to_display_str(raw_last) if raw_last else "N/A"

    # 총 사용자 수: public 제외
    users_by_name = usage_json.get("users_by_name", {})
    total_users = 0
    for name, info in users_by_name.items():
        comment = info.get("comment", "")
        role = _infer_role(name, comment)
        if role != "public":
            total_users += 1

    role = current_role()
    return render_template(
        "manager.html",
        total_users=total_users,
        last_updated_utc=last_updated,
        period_start_utc=period_start,
        role=role,
    )


# ===== /api/users =====
@app.route("/api/users")
def api_users():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    usage_public = load_usage_public()
    usage_override = {"users_by_name": {}}
    usage_bytes_map = build_usage_bytes_map(usage_public, usage_override)

    result_users = []
    users_by_name = usage_public.get("users_by_name", {})

    for name, info in sorted(users_by_name.items()):
        comment = info.get("comment", "")
        limit_gb = float(info.get("limit_gb", 100.0) or 100.0)
        role = _infer_role(name, comment)

        usage_bytes = int(usage_bytes_map.get(name, 0))
        usage_gb = usage_bytes / (1000 ** 3)
        offset_bytes = int(info.get("_offset_bytes", 0) or 0)

        result_users.append({
            "user": name,
            "comment": comment,
            "role": role,
            "limit_gb": limit_gb,
            "usage_bytes": usage_bytes,
            "usage_gb": round(usage_gb, 4),
            "_offset_bytes": offset_bytes,
        })

    # period_start_utc: billing_date.json 기반 계산
    period_start_utc = _calc_period_start_from_billing_date()

    return jsonify({
        "period_start_utc": period_start_utc,
        "last_updated_utc": usage_public.get("last_updated_utc"),
        "users": result_users,
    })


# ===== /api/monthly_usage =====
@app.route("/api/monthly_usage")
def api_monthly_usage():
    username = (request.args.get("user") or "").strip()
    if not username:
        resp = jsonify({"error": "missing user"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 400

    usage_public = load_usage_public()
    users_by_name = usage_public.get("users_by_name", {}) or {}
    info = users_by_name.get(username)

    used_bytes = 0
    offset_bytes = 0
    limit_gb = None
    comment = ""

    if isinstance(info, dict):
        try:
            used_bytes = int(info.get("total_bytes", 0))
        except Exception:
            used_bytes = 0
        try:
            offset_bytes = int(info.get("_offset_bytes", 0))
        except Exception:
            offset_bytes = 0
        try:
            limit_gb = float(info.get("limit_gb", 100.0))
        except Exception:
            limit_gb = None
        # comment 추가
        comment = info.get("comment", "")

    period_start_utc = _calc_period_start_from_billing_date()

    resp = jsonify({
        "user": username,
        "total_bytes": used_bytes,
        "_offset_bytes": offset_bytes,
        "limit_gb": limit_gb,
        "comment": comment,  # comment 필드 추가
        "period_start_utc": period_start_utc,
        "last_updated_utc": usage_public.get("last_updated_utc"),
    })
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ===== /api/usage_public =====
@app.route("/api/usage_public")
def api_usage_public_endpoint():
    """
    Return current usage_public.json for real-time usage monitoring
    Supports optional user parameter for filtering
    """
    try:
        usage_public = load_usage_public()
        
        # 특정 사용자만 요청하는 경우
        username = (request.args.get("user") or "").strip()
        if username:
            users_by_name = usage_public.get("users_by_name", {}) or {}
            user_info = users_by_name.get(username)
            
            if user_info:
                resp = jsonify({
                    "user": username,
                    "total_bytes": user_info.get("total_bytes", 0),
                    "_offset_bytes": user_info.get("_offset_bytes", 0),
                    "limit_gb": user_info.get("limit_gb", 0),
                    "last_updated_utc": usage_public.get("last_updated_utc")
                })
            else:
                resp = jsonify({"error": "User not found"})
                return _add_cors(resp), 404
        else:
            # 전체 데이터 반환
            resp = jsonify(usage_public)
        
        return _add_cors(resp)
    except Exception as e:
        app.logger.error(f"Error loading usage_public.json: {str(e)}")
        resp = jsonify({"error": str(e)})
        return _add_cors(resp), 500


# ===== /api/billing_date =====
@app.route("/api/billing_date")
def api_billing_date():
    """
    Return billing_date.json for client-side period calculation
    """
    try:
        billing_data = _load_billing_date_raw()
        resp = jsonify(billing_data)
        return _add_cors(resp)
    except FileNotFoundError:
        resp = jsonify({"error": "billing_date.json not found"})
        return _add_cors(resp), 404
    except Exception as e:
        app.logger.error(f"Error loading billing_date.json: {str(e)}")
        resp = jsonify({"error": str(e)})
        return _add_cors(resp), 500


# ===== /daily_archives/<filename> =====
@app.route("/daily_archives/<path:filename>")
def serve_daily_archives(filename):
    """
    Serve daily archive JSON files for direct client-side access
    Example: /daily_archives/usage_daily_2026-02-01.json
    """
    try:
        resp = send_from_directory(str(DAILY_ARCHIVES_DIR), filename)
        return _add_cors(resp)
    except FileNotFoundError:
        resp = jsonify({"error": "File not found"})
        return _add_cors(resp), 404
    except Exception as e:
        app.logger.error(f"Error serving daily archive {filename}: {str(e)}")
        resp = jsonify({"error": "Internal server error"})
        return _add_cors(resp), 500


# ===== /api/daily_usage =====
from datetime import timedelta

def _parse_ymd(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()

def _daterange(d0, d1):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)

def _read_daily_effective_bytes_for_user(path: Path, username: str):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    users = raw.get("users")
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
    username = (request.args.get("user") or "").strip()
    start_s  = (request.args.get("start") or "").strip()
    end_s    = (request.args.get("end") or "").strip()
    if not username or not start_s or not end_s:
        return _add_cors(jsonify({"ok": False, "reason": "missing user/start/end"})), 200
    try:
        d0 = _parse_ymd(start_s)
        d1 = _parse_ymd(end_s)
        if d1 < d0:
            d0, d1 = d1, d0
    except Exception:
        return _add_cors(jsonify({"ok": False, "reason": "invalid date format (YYYY-MM-DD)"})), 200
    if (d1 - d0).days > 370:
        return _add_cors(jsonify({"ok": False, "reason": "range too large (max 370 days)"})), 200

    series = []
    prev_eff = None
    cum = 0
    for d in _daterange(d0, d1):
        ymd = d.strftime("%Y-%m-%d")
        path = DAILY_ARCHIVES_DIR / f"usage_daily_{ymd}.json"
        eff = _read_daily_effective_bytes_for_user(path, username) if path.exists() else None
        if eff is None:
            series.append({"date": ymd, "effective_bytes": None, "daily_bytes": 0, "cumulative_bytes": cum, "missing": True})
            continue
        daily = 0 if prev_eff is None else max(0, eff - prev_eff)
        cum += daily
        prev_eff = eff
        series.append({"date": ymd, "effective_bytes": eff, "daily_bytes": daily, "cumulative_bytes": cum, "missing": False})

    return _add_cors(jsonify({"ok": True, "user": username, "start": start_s, "end": end_s, "series": series})), 200


# ===== /api/users/save =====
@app.route("/api/users/save", methods=["POST"])
def api_users_save():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    users_in = data.get("users", [])
    if not isinstance(users_in, list):
        return jsonify({"error": "invalid payload"}), 400

    # superadmin: _offset_bytes 갱신
    if current_role() == "superadmin":
        raw_usage = _safe_read_json(
            USAGE_PUBLIC_PATH,
            {"last_updated_utc": None, "users": []},
        )
        users_list = raw_usage.get("users", [])
        user_map = {e.get("user"): e for e in users_list if e.get("user")}

        for u in users_in:
            name = u.get("user")
            if not name:
                continue

            if "_offset_bytes" in u:
                try:
                    new_off = int(u.get("_offset_bytes") or 0)
                except Exception:
                    new_off = 0
                entry = user_map.get(name)
                if entry is None:
                    entry = {"user": name, "comment": "", "limit_gb": 100.0, "total_bytes": 0, "_last_raw_bytes": 0, "_offset_bytes": new_off}
                    users_list.append(entry)
                    user_map[name] = entry
                else:
                    entry["_offset_bytes"] = new_off
                continue

            if "usage_bytes" not in u:
                continue
            try:
                new_bytes = int(u.get("usage_bytes") or 0)
            except Exception:
                new_bytes = 0
            entry = user_map.get(name)
            if entry is None:
                entry = {"user": name, "comment": "", "limit_gb": 100.0, "total_bytes": new_bytes, "_last_raw_bytes": new_bytes, "_offset_bytes": 0}
                users_list.append(entry)
                user_map[name] = entry
            else:
                try:
                    raw_total = int(entry.get("total_bytes", 0) or 0)
                except Exception:
                    raw_total = 0
                entry["_offset_bytes"] = new_bytes - raw_total

        raw_usage["users"] = users_list
        raw_usage["last_updated_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _safe_write_json(USAGE_PUBLIC_PATH, raw_usage)

    return jsonify({"status": "ok"})


# ===== /api/billing =====
@app.route("/api/billing")
def api_billing():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(load_billing_result())


# ===== /api/settings/billing_start_day =====
@app.route("/api/settings/billing_start_day", methods=["GET", "POST"])
def api_billing_start_day():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    if request.method == "GET":
        try:
            day = _get_billing_start_day()
            return jsonify({"billing_start_day": day})
        except Exception as e:
            return jsonify({"error": f"billing_date.json read failed: {e}"}), 500

    data = request.get_json(silent=True) or {}
    day_val = data.get("billing_start_day")
    try:
        day_val = int(day_val)
    except (TypeError, ValueError):
        return jsonify({"error": "billing_start_day must be an integer 1~31"}), 400
    if not (1 <= day_val <= 31):
        return jsonify({"error": "billing_start_day out of range (1~31)"}), 400

    try:
        bd = _load_billing_date_raw()
        bd["billing_start_day"] = day_val

        # [PATCH] billing_start_day 변경 시 last_reset/last_snapshot도 새 기준으로 align
        import calendar as _cal
        now_dt = datetime.now(timezone.utc)

        def _clamp_day(y: int, m: int, day: int) -> int:
            last_day = _cal.monthrange(y, m)[1]
            return min(max(1, day), last_day)

        def _current_period_start(now_: datetime, start_day_: int) -> datetime:
            y = now_.year
            m = now_.month
            d_this = _clamp_day(y, m, start_day_)
            this_dt = datetime(y, m, d_this, 0, 0, 0, tzinfo=timezone.utc)
            if now_ >= this_dt:
                return this_dt
            if m == 1:
                py, pm = y - 1, 12
            else:
                py, pm = y, m - 1
            d_prev = _clamp_day(py, pm, start_day_)
            return datetime(py, pm, d_prev, 0, 0, 0, tzinfo=timezone.utc)

        cur_start_dt = _current_period_start(now_dt, day_val)
        bd["last_reset_yyyymmdd"] = cur_start_dt.strftime("%Y%m%d")
        bd["last_snapshot_yyyymmdd"] = (cur_start_dt - timedelta(seconds=1)).strftime("%Y%m%d")

        _safe_write_json(BILLING_DATE_PATH, bd)
        return jsonify({
            "status": "ok",
            "billing_start_day": day_val,
            "last_reset_yyyymmdd": bd["last_reset_yyyymmdd"],
            "last_snapshot_yyyymmdd": bd["last_snapshot_yyyymmdd"],
        })
    except Exception as e:
        return jsonify({"error": f"billing_date.json write failed: {e}"}), 500


# ===== /api/settings/billing_engine_params =====
@app.route("/api/settings/billing_engine_params", methods=["GET", "POST"])
def api_billing_engine_params():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    if request.method == "GET":
        return jsonify(load_billing_engine_params())

    data = request.get_json(silent=True) or {}

    # billing_inputs(E1/F16/F21)는 admin/superadmin 모두 저장 가능
    # manager/crew/apprentice 엔진 상수는 superadmin 전용
    has_engine_sections = any(k in data for k in ("manager", "crew", "apprentice"))
    if has_engine_sections and current_role() != "superadmin":
        return jsonify({"error": "엔진 파라미터(manager/crew/apprentice) 수정은 superadmin 전용입니다."}), 403

    def _check_positive(v, name):
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{name} must be a non-negative number (got {v!r})")
    def _check_rate(v, name):
        if not isinstance(v, (int, float)) or not (0 < v <= 1):
            raise ValueError(f"{name} must be between 0 (exclusive) and 1 (inclusive) (got {v!r})")

    try:
        bi = data.get("billing_inputs", {})
        if "base_amount_E1" in bi:
            v = bi["base_amount_E1"]
            if not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"billing_inputs.base_amount_E1 must be a non-negative number (got {v!r})")
        if "target_total_F16" in bi:
            v = bi["target_total_F16"]
            if not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"billing_inputs.target_total_F16 must be a non-negative number (got {v!r})")
        if "common_prepaid_F21" in bi:
            v = bi["common_prepaid_F21"]
            if not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"billing_inputs.common_prepaid_F21 must be a non-negative number (got {v!r})")
        mg = data.get("manager", {})
        if "final_deduction" in mg:
            _check_positive(mg["final_deduction"], "manager.final_deduction")
        cr = data.get("crew", {})
        if "over_limit_surcharge" in cr: _check_positive(cr["over_limit_surcharge"], "crew.over_limit_surcharge")
        if "lowest_usage_deduction" in cr: _check_positive(cr["lowest_usage_deduction"], "crew.lowest_usage_deduction")
        if "under_half_threshold" in cr:
            v = cr["under_half_threshold"]
            if not isinstance(v, (int, float)) or not (0 < v < 1):
                raise ValueError(f"crew.under_half_threshold must be between 0 and 1 (got {v!r})")
        if "under_half_deduction" in cr: _check_positive(cr["under_half_deduction"], "crew.under_half_deduction")
        ap = data.get("apprentice", {})
        if "rate" in ap: _check_rate(ap["rate"], "apprentice.rate")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    params = load_billing_engine_params()
    for section in ("billing_inputs", "manager", "crew", "apprentice"):
        if section in data and isinstance(data[section], dict):
            if section not in params or not isinstance(params[section], dict):
                params[section] = {}
            params[section].update(data[section])
    save_billing_engine_params(params)
    return jsonify({"status": "ok", "params": params})


# ===== 요금 정산 엔진 =====
def _run_billing_engine(usage_source, users_by_name, E1, F16, F21, billing_period_start_utc, engine_params=None):
    """
    usage_source: usage_public 전체 dict
    users_by_name: {name: {comment, limit_gb, ...}} (usage_public에서 직접)
    """
    usage_map = {}
    for u in usage_source.get("users", []):
        name = u.get("user")
        if not name:
            continue
        if "total_bytes" in u:
            try:
                b = int(u.get("total_bytes", 0) or 0)
                off = int(u.get("_offset_bytes", 0) or 0)
                b_eff = b + off
            except Exception:
                b_eff = 0
            gb = b_eff / (1000 ** 3)
        else:
            gb_val = u.get("total_gb", u.get("usage_gb", 0.0))
            try:
                gb = float(gb_val or 0.0)
            except Exception:
                gb = 0.0
        usage_map[name] = gb

    raw_users = []
    for name, info in sorted(users_by_name.items()):
        comment = info.get("comment", "")
        role = _infer_role(name, comment)
        if role == "public":
            continue
        limit_gb = float(info.get("limit_gb", 100.0) or 100.0)
        usage_gb = usage_map.get(name, 0.0)

        raw_users.append({
            "name": name,
            "display_name": comment if comment else name,
            "role": role,
            "limit_gb": limit_gb,
            "usage_gb": usage_gb,
            "personal_prepaid": 0.0,
            "base_fee_override": None,
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

    _ep = engine_params or {}
    _mg = _ep.get("manager", {})
    _cr = _ep.get("crew", {})
    _ap = _ep.get("apprentice", {})

    mg_under_ded  = float(_mg.get("under_limit_deduction", 60))
    mg_over_fixed = float(_mg.get("over_limit_fixed_fee", 110))
    cr_over_sur   = float(_cr.get("over_limit_surcharge", 5))
    cr_low_ded    = float(_cr.get("lowest_usage_deduction", 5))
    cr_half_thr   = float(_cr.get("under_half_threshold", 0.5))
    cr_half_ded   = float(_cr.get("under_half_deduction", 10))
    ap_rate       = float(_ap.get("rate", 0.5))

    for u in raw_users:
        D = u["D"]
        base = E1
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
    sum_personal = 0.0
    remaining_pool = max(0, F16 - E_total - F21 - sum_personal)

    for u in raw_users:
        var_fee = remaining_pool * (u["usage_gb"] / usage_sum) if usage_sum > 0 else 0.0
        u["variable_fee"] = float(round(var_fee))
        u["final_fee"] = float(round(u["base_fee"] + u["variable_fee"], 2))

    summary = {
        "E_total": float(round(E_total, 2)),
        "usage_sum_gb": float(round(usage_sum, 3)),
        "remaining_pool": float(round(remaining_pool, 2)),
        "sum_variable": sum(u["variable_fee"] for u in raw_users),
        "sum_final": sum(u["final_fee"] for u in raw_users),
        "sum_personal_prepaid": 0.0,
        "check_total_collected": float(round(sum(u["final_fee"] for u in raw_users) + F21, 2)),
        "target_total": float(round(F16, 2)),
    }

    return {
        "period_start_utc": billing_period_start_utc,
        "last_updated_utc": usage_source.get("last_updated_utc"),
        "params": {"base_amount_E1": E1, "target_total_F16": F16, "common_prepaid_F21": F21, "billing_period_start_utc": billing_period_start_utc},
        "summary": summary,
        "users": raw_users,
    }


@app.route("/api/billing/run", methods=["POST"])
def api_billing_run():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    engine_params = load_billing_engine_params()
    bi_defaults = engine_params.get("billing_inputs", {})
    try:
        E1  = float(data.get("base_amount_E1",   bi_defaults.get("base_amount_E1",  80.0)))
        F16 = float(data.get("target_total_F16", bi_defaults.get("target_total_F16", 1750.0)))
        F21 = float(data.get("common_prepaid_F21", bi_defaults.get("common_prepaid_F21", 50.0)))
    except (TypeError, ValueError):
        return jsonify({"error": "E1/F16/F21 invalid"}), 400

    usage_public = load_usage_public()
    users_by_name = usage_public.get("users_by_name", {})
    period_start_utc = _calc_period_start_from_billing_date()

    result = _run_billing_engine(usage_public, users_by_name, E1, F16, F21, period_start_utc, engine_params=engine_params)
    _safe_write_json(BILLING_RESULT_PATH, result)
    return jsonify(result)


# ===== /api/usage/reset_all =====
@app.route("/api/usage/reset_all", methods=["POST"])
def api_usage_reset_all():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    try:
        meta = _reset_usage_public_file_all_users()
        app.logger.info(f"Admin reset all usage at {meta['last_updated_utc']}")
        return jsonify({"message": "Pi usage_public.json reset OK", **meta}), 200
    except Exception as e:
        app.logger.error(f"Reset Usage Error: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ===== /api/usage/reset_pi_only =====
@app.route("/api/usage/reset_pi_only", methods=["POST"])
def api_usage_reset_pi_only():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    try:
        meta = _reset_usage_public_file_all_users()
        app.logger.info(f"Admin reset PI-only usage at {meta['last_updated_utc']}")
        return jsonify({"message": "RESET OK (Pi usage_public.json only)", **meta}), 200
    except Exception as e:
        app.logger.error(f"[reset_pi_only] exception: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ===== RouterOS reset_counters_all =====
@app.route("/api/routeros/reset_counters_all", methods=["POST"])
def api_routeros_reset_counters_all():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    try:
        cmd = ':local actList [/ip hotspot active find]; :if ([:len $actList] > 0) do={ /ip hotspot active remove $actList; }; :delay 1; /ip hotspot user reset-counters [find]; :foreach b in=[/ip hotspot ip-binding find where (comment~"_IoT_")] do={ :local mac [/ip hotspot ip-binding get $b mac-address]; :if ([:len $mac] > 0) do={ :local h [/ip hotspot host find where mac-address=$mac]; :if ([:len $h] > 0) do={ /ip hotspot host remove $h; }; }; }; :put "RESET_DONE"'
        out, err = _run_ssh_command(cmd)
        if err:
            app.logger.error(f"[RouterOS reset_counters_all] stderr: {err}")
            return jsonify({"error": err}), 500
        meta = _reset_usage_public_file_all_users()
        return jsonify({"message": "RESET OK (RouterOS + Pi usage_public.json)", "stdout": out, **meta}), 200
    except Exception as e:
        app.logger.error(f"[RouterOS reset_counters_all] exception: {str(e)}")
        return jsonify({"error": str(e)}), 500



# ===== RouterOS reset_counters_router_only =====
@app.route("/api/routeros/reset_counters_router_only", methods=["POST"])
def api_routeros_reset_counters_router_only():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    try:
        cmd = ':local actList [/ip hotspot active find]; :if ([:len $actList] > 0) do={ /ip hotspot active remove $actList; }; :delay 1; /ip hotspot user reset-counters [find]; :foreach b in=[/ip hotspot ip-binding find where (comment~"_IoT_")] do={ :local mac [/ip hotspot ip-binding get $b mac-address]; :if ([:len $mac] > 0) do={ :local h [/ip hotspot host find where mac-address=$mac]; :if ([:len $h] > 0) do={ /ip hotspot host remove $h; }; }; }; :put "RESET_DONE"'
        out, err = _run_ssh_command(cmd)
        if err:
            app.logger.error(f"[RouterOS reset_counters_router_only] stderr: {err}")
            return jsonify({"error": err}), 500
        return jsonify({"message": "RESET OK (RouterOS only)", "stdout": out}), 200
    except Exception as e:
        app.logger.error(f"[RouterOS reset_counters_router_only] exception: {str(e)}")
        return jsonify({"error": str(e)}), 500



# ===== Archives =====
_ALLOWED_ARCHIVE_KIND = {"manual", "daily", "monthly"}
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

def _archive_dir(kind: str) -> Path:
    if kind == "manual":  return MANUAL_ARCHIVES_DIR
    if kind == "daily":   return DAILY_ARCHIVES_DIR
    if kind == "monthly": return MONTHLY_ARCHIVES_DIR
    raise ValueError("invalid kind")

def _validate_kind_and_filename(kind: str, filename: str):
    if kind not in _ALLOWED_ARCHIVE_KIND:
        return False, "invalid kind"
    if not filename or not _SAFE_FILENAME_RE.match(filename):
        return False, "invalid filename"
    if ".." in filename or "/" in filename or "\\" in filename:
        return False, "invalid filename"
    return True, ""

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


# ===== 수동 스냅샷 저장 =====
@app.route("/api/snapshot/save", methods=["POST"])
def api_snapshot_save():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    try:
        usage_public = load_usage_public()
        users_by_name = usage_public.get("users_by_name", {}) or {}

        users_min = []
        for name, info in sorted(users_by_name.items()):
            try:
                total_bytes = int(info.get("total_bytes", 0) or 0)
                offset_bytes = int(info.get("_offset_bytes", 0) or 0)
            except Exception:
                total_bytes = 0
                offset_bytes = 0

            users_min.append({
                "user": name,
                "comment": info.get("comment", ""),
                "limit_gb": float(info.get("limit_gb", 100.0) or 100.0),
                "total_bytes": total_bytes,
                "_last_raw_bytes": int(info.get("_last_raw_bytes", 0) or 0),
                "_offset_bytes": offset_bytes,
            })

        now = datetime.now(timezone.utc)
        period_start_utc = _calc_period_start_from_billing_date()
        snapshot_data = {
            "snapshot_type": "manual",
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



# ===== /api/copy_offset (SUPERADMIN 전용) =====
@app.route("/api/copy_offset", methods=["POST"])
def api_copy_offset():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    if current_role() != "superadmin":
        return jsonify({"error": "superadmin only"}), 403

    data = request.get_json(silent=True) or {}
    kind     = (data.get("kind")     or "").strip()
    filename = (data.get("filename") or "").strip()

    ok, msg = _validate_kind_and_filename(kind, filename)
    if not ok:
        return jsonify({"error": msg}), 400

    # 선택된 아카이브 파일 읽기
    archive_path = _archive_dir(kind) / filename
    if not archive_path.exists():
        return jsonify({"error": "archive file not found"}), 404

    try:
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"error": f"archive read failed: {e}"}), 500

    # 아카이브의 user별 total_bytes 맵 생성
    archive_map = {}
    for u in archive.get("users", []):
        name = u.get("user")
        if name:
            archive_map[name] = int(u.get("total_bytes", 0) or 0)

    # usage_public.json 읽기
    if not USAGE_PUBLIC_PATH.exists():
        return jsonify({"error": "usage_public.json not found"}), 404

    try:
        usage = json.loads(USAGE_PUBLIC_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"error": f"usage_public read failed: {e}"}), 500

    # 아카이브의 total_bytes → usage_public.json 의 _offset_bytes 에 직접 적용
    applied = 0
    skipped = 0
    for u in usage.get("users", []):
        name = u.get("user")
        if not name:
            continue
        if name in archive_map:
            u["_offset_bytes"] = archive_map[name]
            applied += 1
        else:
            skipped += 1

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    usage["last_updated_utc"] = now_iso

    try:
        _safe_write_json(USAGE_PUBLIC_PATH, usage)
    except Exception as e:
        return jsonify({"error": f"usage_public.json 저장 실패: {e}"}), 500

    app.logger.info(
        f"[copy_offset] archive={filename}, applied={applied}, skipped={skipped}, by=superadmin"
    )
    return jsonify({
        "message": "usage_public.json _offset_bytes 적용 완료",
        "archive": filename,
        "applied_count": applied,
        "skipped_count": skipped,
        "last_updated_utc": now_iso,
    }), 200



ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico"}

@app.route("/assets/<path:filename>")
def serve_assets(filename):
    # 보안상 이미지 확장자만 허용
    try:
        ext = Path(filename).suffix.lower()
    except Exception:
        return "Bad request", 400

    if ext not in ASSET_EXTS:
        return "Forbidden", 403

    try:
        return send_from_directory(str(TEMPLATE_DIR), filename)
    except FileNotFoundError:
        return "Not found", 404

if __name__ == "__main__":
    # ★ threaded=True: SSH 등 느린 요청이 다른 API 호출을 막지 않도록 멀티스레드 활성화
    app.run(host="0.0.0.0", port=5000, threaded=True)
