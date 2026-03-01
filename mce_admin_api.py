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
  - period_start_utc 는 billing_date.json의 last_reset_yyyymm에서 계산
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
from datetime import datetime

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    redirect,
    session,
    make_response,
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
    pw = pw.replace("\\", "\\\\")
    pw = pw.replace("\"", "\\\"")
    pw = pw.replace("$", "\\$")
    return pw


_ALLOWED = set(string.printable) - set("\t\n\r\x0b\x0c")

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
    billing_date.json의 last_reset_yyyymm + billing_start_day 에서 period_start_utc 계산.
    예: last_reset_yyyymm="202603", billing_start_day=1 → "2026-03-01T00:00:00Z"
    """
    try:
        import calendar as _cal
        bd = _load_billing_date_raw()
        last_reset = bd.get("last_reset_yyyymm", "")
        start_day = int(bd.get("billing_start_day", 1))
        if not last_reset or len(last_reset) != 6:
            return "-"
        year = int(last_reset[:4])
        month = int(last_reset[4:6])
        last_day = _cal.monthrange(year, month)[1]
        clamped = min(start_day, last_day)
        return f"{year:04d}-{month:02d}-{clamped:02d}T00:00:00Z"
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
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
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
    "manager": {"under_limit_deduction": 60, "over_limit_fixed_fee": 110},
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
    user = (request.form.get("user") or "").strip()
    cur  = (request.form.get("current_password") or "").strip()
    newp = (request.form.get("new_password") or "").strip()
    if not user or not newp:
        return _add_cors(jsonify({"ok": False, "reason": "Missing fields."})), 200

    ok, reason = _validate_new_password(newp)
    if not ok:
        return _add_cors(jsonify({"ok": False, "reason": reason})), 200

    ver_cmd = f'/ip hotspot user print detail where name="{user}"'
    out_ver, err_ver = _run_ssh_command(ver_cmd)
    m = re.search(r'password="([^"]+)"', out_ver)
    router_pw = m.group(1) if m else None
    esc_cur = _escape_password(cur)
    if err_ver:
        return _add_cors(jsonify({"ok": False, "reason": f"RouterOS error: {err_ver}"})), 200

    if (router_pw != cur) and (router_pw != esc_cur):
        return _add_cors(jsonify({
            "ok": False,
            "reason": "Current password does not match.",
            "router_current_password": router_pw,
            "provided_current_password": cur,
        })), 200

    esc_new = _escape_password(newp)
    set_cmd = f'/ip hotspot user set {user} password="{esc_new}"'
    out_set, err_set = _run_ssh_command(set_cmd)
    if err_set:
        return _add_cors(jsonify({"ok": False, "reason": f"SET error: {err_set}"})), 200

    time.sleep(0.5)
    out_ver, err_ver = _run_ssh_command(ver_cmd)
    if err_ver:
        return _add_cors(jsonify({"ok": False, "reason": f"RouterOS verify error: {err_ver}"})), 200

    m = re.search(r'password="([^"]+)"', out_ver)
    router_pw = m.group(1) if m else "(not found)"
    if (router_pw != newp) and (router_pw != esc_new):
        return _add_cors(jsonify({
            "ok": False,
            "reason": "New password does not match as Router.",
            "router_current_password": router_pw,
            "provided_current_password": cur,
        })), 200

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

@app.route("/archive_billing")
def archive_billing_page():
    if not require_login():
        return redirect("/login")
    return render_template("archive_billing.html", role=current_role())

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

    # period_start_utc: billing_date.json의 last_reset_yyyymm에서 계산
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

    period_start_utc = _calc_period_start_from_billing_date()

    resp = jsonify({
        "user": username,
        "total_bytes": used_bytes,
        "_offset_bytes": offset_bytes,
        "limit_gb": limit_gb,
        "period_start_utc": period_start_utc,
        "last_updated_utc": usage_public.get("last_updated_utc"),
    })
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


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
        raw_usage["last_updated_utc"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
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
        _safe_write_json(BILLING_DATE_PATH, bd)
        return jsonify({"status": "ok", "billing_start_day": day_val})
    except Exception as e:
        return jsonify({"error": f"billing_date.json write failed: {e}"}), 500


# ===== /api/settings/billing_engine_params =====
@app.route("/api/settings/billing_engine_params", methods=["GET", "POST"])
def api_billing_engine_params():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    if request.method == "GET":
        return jsonify(load_billing_engine_params())
    if current_role() != "superadmin":
        return jsonify({"error": "superadmin only"}), 403

    data = request.get_json(silent=True) or {}

    def _check_positive(v, name):
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{name} must be a non-negative number (got {v!r})")
    def _check_rate(v, name):
        if not isinstance(v, (int, float)) or not (0 < v <= 1):
            raise ValueError(f"{name} must be between 0 (exclusive) and 1 (inclusive) (got {v!r})")

    try:
        mg = data.get("manager", {})
        if "under_limit_deduction" in mg: _check_positive(mg["under_limit_deduction"], "manager.under_limit_deduction")
        if "over_limit_fixed_fee" in mg: _check_positive(mg["over_limit_fixed_fee"], "manager.over_limit_fixed_fee")
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
    for section in ("manager", "crew", "apprentice"):
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
    try:
        E1 = float(data.get("base_amount_E1"))
        F16 = float(data.get("target_total_F16"))
        F21 = float(data.get("common_prepaid_F21", 0.0))
    except (TypeError, ValueError):
        return jsonify({"error": "E1/F16/F21 invalid"}), 400

    usage_public = load_usage_public()
    users_by_name = usage_public.get("users_by_name", {})
    engine_params = load_billing_engine_params()
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

        now = datetime.utcnow()
        period_start_utc = _calc_period_start_from_billing_date()
        snapshot_data = {
            "snapshot_type": "manual",
            "timestamp_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period_start_utc": period_start_utc,
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
