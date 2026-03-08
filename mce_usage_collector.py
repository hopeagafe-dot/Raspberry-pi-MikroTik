#!/usr/bin/env python3
"""
mce_usage_collector.py

구조 변경 사항 (2026-03):
  - billing_settings.json 완전 폐기 → billing_date.json 단독 사용
  - user_config.json 완전 폐기 → comment/limit_gb를 RouterOS에서 직접 읽어 usage_public.json에 저장
  - usage_public.json 포맷: {last_updated_utc, users: [{user, comment, limit_gb, total_bytes, _last_raw_bytes, _offset_bytes}]}
  - period_start_utc 필드 제거
  - maybe_rollover_month: billing_date.json의 last_snapshot_yyyymmdd / last_reset_yyyymmdd 기반 idempotent
  - 리셋: _offset_bytes = 0 (RouterOS 카운터 0 리셋)

billing_date.json 형식:
{
  "billing_start_day": 1,
  "pre_snapshot_seconds": 15,
  "last_snapshot_yyyymmdd": "20260131",
  "last_reset_yyyymmdd": "20260201"
}
"""

from librouteros import connect
from datetime import datetime, timezone, timedelta
import json
import os
import calendar
from typing import Dict, Any, Tuple, Set, Optional
import paramiko

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

ROUTER_PASSWORD = _load_router_password()

DATA_DIR = os.path.expanduser("~/mcepi_data")
JSON_PATH = os.path.join(DATA_DIR, "usage_public.json")
DAILY_ARCHIVE_DIR = os.path.join(DATA_DIR, "daily_archives")
MONTHLY_ARCHIVE_DIR = os.path.join(DATA_DIR, "monthly_archives")
LOG_FILE = os.path.join(DATA_DIR, "collector.log")
BILLING_DATE_PATH = os.path.join(DATA_DIR, "billing_date.json")
DAILY_JSON_PATH = os.path.join(DATA_DIR, "usage_daily.json")

TOL_SYNC_BYTES = 100 * 1000 * 1000  # 100 MB

import logging
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1000*1000, backupCount=3),
        logging.StreamHandler()
    ]
)

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def ensure_data_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MONTHLY_ARCHIVE_DIR, exist_ok=True)



SSH_PORT = 22

def _run_ssh_command(cmd: str) -> Tuple[str, str]:
    """
    RouterOS SSH 접속 후 명령 실행.
    return: (stdout, stderr)
    """
    pw = _load_router_password() or ROUTER_PASSWORD
    if not pw:
        return "", "router_password.txt is empty"

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        ROUTER_HOST,
        port=SSH_PORT,
        username=ROUTER_USER,
        password=pw,
        timeout=10,
        allow_agent=False,
        look_for_keys=False,
    )
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode("utf-8", errors="ignore").strip()
    err = stderr.read().decode("utf-8", errors="ignore").strip()
    ssh.close()
    return out, err


def routeros_force_hotspot_reset() -> Tuple[bool, str]:
    """
    RouterOS에서:
      1) active 강제 로그아웃
      2) user reset-counters 전체
      3) IoT host reset( ip-binding comment에 _IoT_ 포함된 MAC의 host 제거 )
    """
    cmd = (
        ':local actList [/ip hotspot active find]; '
        ':if ([:len $actList] > 0) do={ /ip hotspot active remove $actList; }; '
        ':delay 1; '
        '/ip hotspot user reset-counters [find]; '
        ':foreach b in=[/ip hotspot ip-binding find where (comment~"_IoT_")] do={ '
        '  :local mac [/ip hotspot ip-binding get $b mac-address]; '
        '  :if ([:len $mac] > 0) do={ '
        '    :local h [/ip hotspot host find where mac-address=$mac]; '
        '    :if ([:len $h] > 0) do={ /ip hotspot host remove $h; }; '
        '  }; '
        '}; '
        ':put "RESET_DONE"'
    )

    out, err = _run_ssh_command(cmd)
    if err:
        return False, f"SSH err: {err}"
    if "RESET_DONE" not in out:
        # 일부 환경에서는 stdout이 비거나 put 출력이 안 잡히는 경우가 있어 “경고”만 남기고 성공 처리도 가능
        logging.warning(f"[RouterOS RESET] stdout did not contain RESET_DONE. stdout={out!r}")
    return True, out


def atomic_write_json(filepath, data):
    temp_path = f"{filepath}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
    except Exception as e:
        logging.error(f"Atomic write failed for {filepath}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

def load_billing_date() -> dict:
    with open(BILLING_DATE_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("billing_date.json must be a JSON object")
    return d

def load_state() -> Dict[str, Any]:
    ensure_data_dirs()

    def _fresh() -> Dict[str, Any]:
        return {"last_updated_utc": None, "users": {}}

    if not os.path.exists(JSON_PATH):
        state = _fresh()
        save_state(state)
        return state

    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logging.warning(f"[usage_public.json RECOVERY] {e}")
        state = _fresh()
        save_state(state)
        return state

    users_dict: Dict[str, Dict] = {}
    for entry in raw.get("users", []):
        name = entry.get("user")
        if not name:
            continue
        users_dict[name] = {
            "comment": entry.get("comment", ""),
            "limit_gb": float(entry.get("limit_gb", 100.0) or 100.0),
            "total_bytes": int(entry.get("total_bytes", 0) or 0),
            "_last_raw_bytes": int(entry.get("_last_raw_bytes", 0) or 0),
            "_offset_bytes": int(entry.get("_offset_bytes", 0) or 0),
        }

    return {
        "last_updated_utc": raw.get("last_updated_utc"),
        "users": users_dict,
    }

def save_state(state: Dict[str, Any]) -> None:
    out_users = []
    for name in sorted(state["users"].keys()):
        info = state["users"][name]
        out_users.append({
            "user": name,
            "comment": info.get("comment", ""),
            "limit_gb": float(info.get("limit_gb", 100.0) or 100.0),
            "total_bytes": int(info.get("total_bytes", 0) or 0),
            "_last_raw_bytes": int(info.get("_last_raw_bytes", 0) or 0),
            "_offset_bytes": int(info.get("_offset_bytes", 0) or 0),
        })
    out = {
        "last_updated_utc": state.get("last_updated_utc"),
        "users": out_users,
    }
    atomic_write_json(JSON_PATH, out)

def load_daily_state() -> Dict[str, Any]:
    ensure_data_dirs()
    if not os.path.exists(DAILY_JSON_PATH):
        return {"current_date": None, "last_updated_utc": None}
    try:
        with open(DAILY_JSON_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        raw.pop("days", None)
        return {"current_date": raw.get("current_date"), "last_updated_utc": raw.get("last_updated_utc")}
    except Exception:
        return {"current_date": None, "last_updated_utc": None}

def save_daily_state(daily: Dict[str, Any]) -> None:
    atomic_write_json(DAILY_JSON_PATH, {
        "current_date": daily.get("current_date"),
        "last_updated_utc": now_utc_iso(),
    })

def build_min_users_snapshot(users_state: dict) -> list:
    """포맷: {user, comment, limit_gb, total_bytes, _last_raw_bytes, _offset_bytes}. 0 사용자 제외."""
    result = []
    for name in sorted(users_state.keys()):
        info = users_state.get(name, {})
        try:
            total_bytes = int(info.get("total_bytes", 0) or 0)
            offset_bytes = int(info.get("_offset_bytes", 0) or 0)
        except Exception:
            total_bytes = 0
            offset_bytes = 0
        if (total_bytes + offset_bytes) == 0:
            continue
        result.append({
            "user": name,
            "comment": info.get("comment", ""),
            "limit_gb": float(info.get("limit_gb", 100.0) or 100.0),
            "total_bytes": total_bytes,
            "_last_raw_bytes": int(info.get("_last_raw_bytes", 0) or 0),
            "_offset_bytes": offset_bytes,
        })
    return result

def archive_daily_data(current_usage_state, target_date):
    try:
        ensure_data_dirs()
        os.makedirs(DAILY_ARCHIVE_DIR, exist_ok=True)

        users_in = current_usage_state.get("users", {})
        users_list = []
        if isinstance(users_in, dict):
            for name in sorted(users_in.keys()):
                info = users_in.get(name, {})
                users_list.append({
                    "user": name,
                    "comment": info.get("comment", ""),
                    "limit_gb": float(info.get("limit_gb", 100.0) or 100.0),
                    "total_bytes": int(info.get("total_bytes", 0) or 0),
                    "_last_raw_bytes": int(info.get("_last_raw_bytes", 0) or 0),
                    "_offset_bytes": int(info.get("_offset_bytes", 0) or 0),
                })

        snapshot = {
            "snapshot_type": "daily_auto",
            "record_date": target_date,
            "users": users_list,
        }
        save_path = os.path.join(DAILY_ARCHIVE_DIR, f"usage_daily_{target_date}.json")
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=4, ensure_ascii=False)
        logging.info(f"Daily archive saved: {target_date}")
        return True
    except Exception as e:
        logging.error(f"Archive Error: {str(e)}")
        return False

# ── 보관 기간 설정 (일) ──
RETENTION_DAILY_DAYS   = 100
RETENTION_MANUAL_DAYS  = 180
RETENTION_MONTHLY_DAYS = 180

MANUAL_ARCHIVE_DIR = os.path.join(DATA_DIR, "manual_archives")

def purge_old_archives(now_dt: datetime) -> None:
    """
    날짜가 바뀔 때 호출.
    보관 기간을 초과한 archive JSON 파일을 삭제한다.
    - daily_archives  : 100일 초과
    - manual_archives : 180일 초과
    - monthly_archives: 180일 초과
    파일명 파싱 실패 시 mtime 기준으로 fallback.
    """
    targets = [
        (DAILY_ARCHIVE_DIR,   RETENTION_DAILY_DAYS,   "daily"),
        (MANUAL_ARCHIVE_DIR,  RETENTION_MANUAL_DAYS,  "manual"),
        (MONTHLY_ARCHIVE_DIR, RETENTION_MONTHLY_DAYS, "monthly"),
    ]

    for dir_path, keep_days, label in targets:
        if not os.path.isdir(dir_path):
            continue

        cutoff = now_dt - timedelta(days=keep_days)
        deleted = 0
        errors  = 0

        for fname in os.listdir(dir_path):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(dir_path, fname)

            # ── 파일명에서 날짜 파싱 시도 ──
            file_dt = None
            # 패턴 1: usage_daily_YYYY-MM-DD.json
            import re as _re
            m = _re.search(r'(\d{4})-(\d{2})-(\d{2})', fname)
            if m:
                try:
                    file_dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                       tzinfo=timezone.utc)
                except ValueError:
                    pass
            # 패턴 2: usage_total_YYYYMMDD_HHMMSS.json
            if file_dt is None:
                m2 = _re.search(r'(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', fname)
                if m2:
                    try:
                        file_dt = datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)),
                                           int(m2.group(4)), int(m2.group(5)), int(m2.group(6)),
                                           tzinfo=timezone.utc)
                    except ValueError:
                        pass
            # fallback: mtime
            if file_dt is None:
                try:
                    mtime = os.path.getmtime(fpath)
                    file_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                except Exception:
                    continue

            if file_dt < cutoff:
                try:
                    os.remove(fpath)
                    deleted += 1
                    logging.info(f"[Purge/{label}] 삭제: {fname} (기준: {keep_days}일)")
                except Exception as e:
                    errors += 1
                    logging.warning(f"[Purge/{label}] 삭제 실패: {fname} - {e}")

        if deleted or errors:
            logging.info(f"[Purge/{label}] 완료 — 삭제:{deleted}개, 오류:{errors}개")


def _save_monthly_snapshot(state: Dict[str, Any], now_dt: datetime) -> None:
    try:
        os.makedirs(MONTHLY_ARCHIVE_DIR, exist_ok=True)
        users_list = build_min_users_snapshot(state.get("users", {}))
        archive_filename = f"usage_total_{now_dt.strftime('%Y%m%d_%H%M%S')}.json"
        archive_path = os.path.join(MONTHLY_ARCHIVE_DIR, archive_filename)
        archive_obj = {
            "snapshot_type": "monthly_auto",
            "users": users_list,
        }
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(archive_obj, f, ensure_ascii=False, indent=2)
        logging.info(f"Monthly snapshot saved: {archive_filename}")
    except Exception as e:
        logging.error(f"[Rollover] Snapshot save failed: {e}")
        raise

def maybe_rollover_month(state: Dict[str, Any]) -> None:
    """
    billing_date.json 기반 idempotent 롤오버 (yyyymmdd).

    컨셉:
      - target_reset_utc는 "last_reset_yyyymmdd의 YYYYMM + billing_start_day"를 기준으로 +1개월(말일 clamp)
      - 스냅샷은 target_reset_utc - pre_snapshot_seconds ~ target_reset_utc 직전(window)에서 1회
      - window를 놓쳤더라도 reset 직전에 스냅샷 1회 보강
      - 리셋(HARD ZERO): total_bytes/_last_raw_bytes/_offset_bytes = 0
      - 마지막에 billing_date.json 갱신(멱등성)
    """
    ensure_data_dirs()
    now_dt = datetime.now(timezone.utc)

    try:
        bd = load_billing_date()
    except Exception as e:
        logging.error(f"[Rollover] billing_date.json load failed: {e}")
        return

    # ---- helpers ----
    def _parse_yyyymmdd(s: str) -> Optional[datetime]:
        s = (s or "").strip()
        if len(s) != 8 or not s.isdigit():
            return None
        try:
            y = int(s[:4]); m = int(s[4:6]); d = int(s[6:8])
            return datetime(y, m, d, 0, 0, 0, tzinfo=timezone.utc)
        except Exception:
            return None

    def _month_last_day(y: int, m: int) -> int:
        return calendar.monthrange(y, m)[1]

    def _clamp_day(y: int, m: int, day: int) -> int:
        return min(max(1, day), _month_last_day(y, m))

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

    # ---- read settings ----
    try:
        start_day = int(bd.get("billing_start_day", 1))
    except Exception:
        start_day = 1
    if start_day < 1: start_day = 1
    if start_day > 31: start_day = 31

    try:
        pre_snapshot_seconds = int(bd.get("pre_snapshot_seconds", 15))
    except Exception:
        pre_snapshot_seconds = 15
    if pre_snapshot_seconds < 1:
        pre_snapshot_seconds = 15

    last_snapshot = (bd.get("last_snapshot_yyyymmdd") or "").strip()
    last_reset = (bd.get("last_reset_yyyymmdd") or "").strip()

    # ---- migrate from legacy yyyymm if needed ----
    last_reset_dt = _parse_yyyymmdd(last_reset)
    if last_reset_dt is None:
        legacy = (bd.get("last_reset_yyyymm") or "").strip()
        if len(legacy) == 6 and legacy.isdigit():
            y = int(legacy[:4]); m = int(legacy[4:6])
            d = _clamp_day(y, m, start_day)
            last_reset_dt = datetime(y, m, d, 0, 0, 0, tzinfo=timezone.utc)
            last_reset = f"{y:04d}{m:02d}{d:02d}"
            bd["last_reset_yyyymmdd"] = last_reset
        else:
            last_reset_dt = _current_period_start(now_dt, start_day)
            last_reset = last_reset_dt.strftime("%Y%m%d")
            bd["last_reset_yyyymmdd"] = last_reset

    if _parse_yyyymmdd(last_snapshot) is None:
        snap_dt = last_reset_dt - timedelta(seconds=1)
        last_snapshot = snap_dt.strftime("%Y%m%d")
        bd["last_snapshot_yyyymmdd"] = last_snapshot

    # [PATCH] 미래 last_reset 방어: last_reset_yyyymmdd가 now보다 미래면 "현재 period start"로 교정
    # (last_reset은 "마지막 리셋"이므로 미래일 수 없음)
    if last_reset_dt > (now_dt + timedelta(seconds=60)):  # 60초는 미세 오차/동시성 완충
        cur_start_dt = _current_period_start(now_dt, start_day)
        cur_start = cur_start_dt.strftime("%Y%m%d")

        bd["last_reset_yyyymmdd"] = cur_start
        last_reset = cur_start
        last_reset_dt = cur_start_dt

        # snapshot은 period start 직전 날짜로 맞춤(멱등성 유지)
        bd["last_snapshot_yyyymmdd"] = (cur_start_dt - timedelta(seconds=1)).strftime("%Y%m%d")
        last_snapshot = bd["last_snapshot_yyyymmdd"]

        atomic_write_json(BILLING_DATE_PATH, bd)
        logging.warning(
            f"[Rollover][GUARD] last_reset_yyyymmdd was in the FUTURE -> aligned to current period start: {cur_start}"
        )

    # ---- compute next target_reset based on (last_reset month + start_day) + 1 month ----
    base_y = last_reset_dt.year
    base_m = last_reset_dt.month
    base_d = _clamp_day(base_y, base_m, start_day)
    base_dt = datetime(base_y, base_m, base_d, 0, 0, 0, tzinfo=timezone.utc)

    if base_m == 12:
        ny, nm = base_y + 1, 1
    else:
        ny, nm = base_y, base_m + 1
    next_d = _clamp_day(ny, nm, start_day)
    target_reset_utc = datetime(ny, nm, next_d, 0, 0, 0, tzinfo=timezone.utc)

    snap_target = (target_reset_utc - timedelta(seconds=1)).strftime("%Y%m%d")
    reset_target = target_reset_utc.strftime("%Y%m%d")
    target_snapshot_utc = target_reset_utc - timedelta(seconds=pre_snapshot_seconds)

    # [PATCH] late-reset cutoff (하드코딩 1시간)
    late_deadline_utc = target_reset_utc + timedelta(hours=1)

    bd_changed = False

    # ---- (A) pre-snapshot window ----
    # target_snapshot_utc <= now_dt < target_reset_utc 구간에서 스냅샷 1회 보장
    if (last_snapshot != snap_target) and (target_snapshot_utc <= now_dt < target_reset_utc):
        _save_monthly_snapshot(state, now_dt)
        bd["last_snapshot_yyyymmdd"] = snap_target
        last_snapshot = snap_target
        bd_changed = True
        logging.info(f"[Rollover] Monthly snapshot (window) taken: {snap_target}")

    # ---- reset trigger ----
    if last_reset != reset_target and (now_dt >= target_reset_utc):

        # [PATCH] 너무 늦으면(H+1 초과) HARD ZERO 금지 -> align-only로 폭주/사고 방지
        # (cutoff 초과 상태에서는 월 스냅샷도 찍지 않도록 "보강 스냅샷"보다 위에서 return)
        if now_dt > late_deadline_utc:
            cur_start_dt = _current_period_start(now_dt, start_day)
            cur_start = cur_start_dt.strftime("%Y%m%d")

            bd["last_reset_yyyymmdd"] = cur_start
            bd["last_snapshot_yyyymmdd"] = (cur_start_dt - timedelta(seconds=1)).strftime("%Y%m%d")

            atomic_write_json(BILLING_DATE_PATH, bd)
            logging.warning(
                f"[Rollover][SKIP HARD ZERO] reset missed too long ago. "
                f"target_reset={reset_target}, now={now_dt.strftime('%Y%m%d_%H%M%S')}Z, "
                f"cutoff=+1h -> aligned only (no reset)"
            )
            return

        # 보강: snapshot window를 놓쳤으면 reset 직전에 1회 저장 (cutoff 이내에서만)
        if last_snapshot != snap_target:
            _save_monthly_snapshot(state, now_dt)
            bd["last_snapshot_yyyymmdd"] = snap_target
            last_snapshot = snap_target
            bd_changed = True
            logging.info(f"[Rollover] Monthly snapshot (late) taken before reset: {snap_target}")

        # [PATCH] Pi HARD ZERO 직전 RouterOS 강제 리셋 (active logout + reset-counters + IoT reset)
        ok, msg = routeros_force_hotspot_reset()
        if not ok:
            logging.error(f"[Rollover] RouterOS reset FAILED -> abort HARD ZERO to avoid mismatch. ({msg})")

            # snapshot을 이미 찍어 bd_changed가 True일 수 있으므로, 여기서 billing_date.json은 반영해둠(중복 스냅샷 방지)
            if bd_changed:
                atomic_write_json(BILLING_DATE_PATH, bd)
            return
        logging.info(f"[Rollover] RouterOS reset OK: {msg}")


        for _, info in state.get("users", {}).items():
            if not isinstance(info, dict):
                continue
            info["total_bytes"] = 0
            info["_last_raw_bytes"] = 0
            info["_offset_bytes"] = 0

        # 반복 리셋 폭주 방지: reset 후 last_reset을 "현재 기간 시작일"로 점프
        cur_start = _current_period_start(now_dt, start_day).strftime("%Y%m%d")
        bd["last_reset_yyyymmdd"] = cur_start
        last_reset = cur_start
        bd_changed = True
        logging.info(f"[Rollover] Monthly reset done (HARD ZERO): {reset_target} -> last_reset_yyyymmdd={cur_start}")

    if bd_changed:
        atomic_write_json(BILLING_DATE_PATH, bd)


def get_hotspot_users_full(api) -> Dict[str, dict]:
    """RouterOS에서 name/comment/limit-bytes-total 읽기."""
    result: Dict[str, dict] = {}
    users = api.path("ip", "hotspot", "user")
    for u in users:
        name = u.get("name")
        if not name:
            continue
        comment = (u.get("comment") or "").strip()
        try:
            limit_bytes = int(u.get("limit-bytes-total", 0) or 0)
            limit_gb = limit_bytes / 1_000_000_000 if limit_bytes > 0 else 100.0
        except Exception:
            limit_gb = 100.0
        result[name] = {"comment": comment, "limit_gb": limit_gb}
    return result

def get_hotspot_usage(api) -> Tuple[Dict[str, int], Set[str]]:
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
        usage[user] = usage.get(user, 0) + b_in + b_out
    return usage, active_macs

def get_iot_usage_from_hotspot_host(api, active_macs: Optional[Set[str]] = None) -> Dict[str, int]:
    usage: Dict[str, int] = {}
    if active_macs is None:
        active_macs = set()
    else:
        active_macs = {str(m).upper() for m in active_macs}

    host_bytes_by_mac: Dict[str, int] = {}
    hosts = api.path("ip", "hotspot", "host")
    for h in hosts:
        mac = h.get("mac-address")
        if not mac:
            continue
        mac_u = str(mac).upper()
        host_bytes_by_mac[mac_u] = host_bytes_by_mac.get(mac_u, 0) + int(h.get("bytes-in", 0) or 0) + int(h.get("bytes-out", 0) or 0)

    bindings = api.path("ip", "hotspot", "ip-binding")
    for b in bindings:
        comment = (b.get("comment") or "")
        if "_IoT_" not in comment:
            continue
        user = comment.split("_IoT_", 1)[0].strip()
        if not user:
            continue
        mac = b.get("mac-address")
        if not mac:
            continue
        mac_u = str(mac).upper()
        if mac_u in active_macs:
            continue
        tot = host_bytes_by_mac.get(mac_u, 0)
        if tot <= 0:
            continue
        usage[user] = usage.get(user, 0) + tot
    return usage

def get_hotspot_usernames(api) -> set:
    names = set()
    for u in api.path("ip", "hotspot", "user"):
        name = u.get("name")
        if name:
            names.add(name)
    return names

def get_hotspot_user_counters(api) -> Dict[str, int]:
    usage: Dict[str, int] = {}
    for u in api.path("ip", "hotspot", "user"):
        name = u.get("name")
        if not name:
            continue
        usage[name] = int(u.get("bytes-in", 0) or 0) + int(u.get("bytes-out", 0) or 0)
    return usage


def main():
    state = load_state()
    users_state = state["users"]

    daily_state = load_daily_state()
    now_dt = datetime.now(timezone.utc)
    today_str = now_dt.strftime("%Y-%m-%d")

    old_date = daily_state.get("current_date")
    if old_date and old_date != today_str:
        if today_str < old_date:
            logging.warning(f"[Daily Rotation Skipped] Clock rollback: {old_date} -> {today_str}")
        else:
            logging.info(f"날짜 변경 감지: {old_date} -> {today_str}. 어제 데이터를 아카이브합니다.")
            archive_daily_data(state, old_date)
            purge_old_archives(now_dt)          # 보관 기간 초과 파일 정리
            daily_state["current_date"] = today_str
            save_daily_state(daily_state)
    elif not old_date:
        daily_state["current_date"] = today_str
        save_daily_state(daily_state)

    maybe_rollover_month(state)

    api = connect(
        host=ROUTER_HOST,
        username=ROUTER_USER,
        password=ROUTER_PASSWORD,
        port=8728,
        timeout=10,
    )

    # RouterOS에서 comment/limit_gb 읽어 state 갱신
    hotspot_user_info = get_hotspot_users_full(api)
    for name, info in hotspot_user_info.items():
        entry = users_state.setdefault(name, {
            "comment": "",
            "limit_gb": 100.0,
            "total_bytes": 0,
            "_last_raw_bytes": 0,
            "_offset_bytes": 0,
        })
        entry["comment"] = info["comment"]
        entry["limit_gb"] = info["limit_gb"]

    router_usernames = get_hotspot_usernames(api)
    hotspot_active_by_user, active_macs = get_hotspot_usage(api)
    hotspot_user_counters = get_hotspot_user_counters(api)
    iot_usage = get_iot_usage_from_hotspot_host(api, active_macs)


    # [PATCH] RouterOS에 없는 사용자 제거 (RouterOS에서 user 삭제 시 usage_public에서도 삭제)
    # 안전장치: router_usernames가 비어있으면(통신/권한 문제 등) 대량 삭제를 막기 위해 purge 스킵
    if router_usernames:
        stale_users = set(users_state.keys()) - set(router_usernames)
        if stale_users:
            for u in stale_users:
                users_state.pop(u, None)
            logging.info(f"[SYNC] Purged {len(stale_users)} users not present in RouterOS: {sorted(stale_users)}")
    else:
        logging.warning("[SYNC] router_usernames is empty -> purge skipped (avoid mass deletion)")


    for user in router_usernames:
        U = int(hotspot_user_counters.get(user, 0) or 0)
        A = int(hotspot_active_by_user.get(user, 0) or 0)
        I = int(iot_usage.get(user, 0) or 0)
        raw_now = U + A + I

        user_info = users_state.setdefault(user, {
            "comment": hotspot_user_info.get(user, {}).get("comment", ""),
            "limit_gb": hotspot_user_info.get(user, {}).get("limit_gb", 100.0),
            "total_bytes": 0,
            "_last_raw_bytes": 0,
            "_offset_bytes": 0,
        })

        total = int(user_info.get("total_bytes", 0) or 0)
        raw_last = int(user_info.get("_last_raw_bytes", 0) or 0)
        diff = total - raw_now

        if raw_now + TOL_SYNC_BYTES >= total:
            if 0 < diff <= TOL_SYNC_BYTES:
                logging.warning(
                    f"[TOL_SYNC] user={user} total({total}) > raw_now({raw_now}) by {diff} bytes "
                    f"(U={U}, A={A}, I={I}) -> trust RouterOS"
                )
            total_new = raw_now
        else:
            delta = max(0, raw_now - raw_last)
            total_new = total + delta

        user_info["total_bytes"] = total_new
        user_info["_last_raw_bytes"] = raw_now
        user_info.pop("devices", None)

    state["last_updated_utc"] = now_utc_iso()
    save_state(state)
    save_daily_state(daily_state)

    print("=== MCE Usage Collector ===")
    print(f"Last updated : {state['last_updated_utc']}")
    print()
    for name in sorted(users_state.keys()):
        info = users_state[name]
        eff_b = info["total_bytes"] + info.get("_offset_bytes", 0)
        eff_gb = eff_b / 1_000_000_000
        limit_gb = info.get("limit_gb", 100.0)
        comment = info.get("comment", "")
        print(f"{name:10s}  {eff_gb:7.3f} / {limit_gb:.0f} GB  [{comment}]")


if __name__ == "__main__":
    main()
