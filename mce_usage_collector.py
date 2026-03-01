#!/usr/bin/env python3
"""
mce_usage_collector.py

구조 변경 사항 (2026-03):
  - billing_settings.json 완전 폐기 → billing_date.json 단독 사용
  - user_config.json 완전 폐기 → comment/limit_gb를 RouterOS에서 직접 읽어 usage_public.json에 저장
  - usage_public.json 포맷: {last_updated_utc, users: [{user, comment, limit_gb, total_bytes, _last_raw_bytes, _offset_bytes}]}
  - period_start_utc 필드 제거
  - maybe_rollover_month: billing_date.json의 last_snapshot_yyyymm / last_reset_yyyymm 기반 idempotent
  - 리셋: _offset_bytes = -current_total_bytes (RouterOS 카운터 유지)

billing_date.json 형식:
{
  "billing_start_day": 1,
  "pre_snapshot_seconds": 15,
  "last_snapshot_yyyymm": "202601",
  "last_reset_yyyymm": "202601"
}
"""

from librouteros import connect
from datetime import datetime, timezone, timedelta
import json
import os
import calendar
from typing import Dict, Any, Tuple, Set, Optional

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
            "timestamp_utc": now_utc_iso(),
            "last_updated_utc": current_usage_state.get("last_updated_utc"),
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

def _save_monthly_snapshot(state: Dict[str, Any], now_dt: datetime) -> None:
    try:
        os.makedirs(MONTHLY_ARCHIVE_DIR, exist_ok=True)
        users_list = build_min_users_snapshot(state.get("users", {}))
        archive_filename = f"usage_total_{now_dt.strftime('%Y%m%d_%H%M%S')}.json"
        archive_path = os.path.join(MONTHLY_ARCHIVE_DIR, archive_filename)
        archive_obj = {
            "snapshot_type": "monthly_auto",
            "timestamp_utc": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_updated_utc": state.get("last_updated_utc"),
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
    billing_date.json 기반 idempotent 롤오버.

    스냅샷 트리거:
      last_snapshot_yyyymm != snap_target_yyyymm
      AND target_snapshot_utc <= now < target_reset_utc

    리셋 트리거:
      last_reset_yyyymm != reset_target_yyyymm
      AND now >= target_reset_utc

    리셋: _offset_bytes =  0
        total_bytes = 0
        _last_raw_bytes = 0
    """
    ensure_data_dirs()
    now_dt = datetime.now(timezone.utc)

    try:
        bd = load_billing_date()
    except Exception as e:
        logging.error(f"[Rollover] billing_date.json load failed: {e}")
        return

    start_day = int(bd.get("billing_start_day", 1))
    pre_snapshot_seconds = int(bd.get("pre_snapshot_seconds", 15))
    last_snapshot_yyyymm = bd.get("last_snapshot_yyyymm", "000000")
    last_reset_yyyymm = bd.get("last_reset_yyyymm", "000000")

    # 이번 달의 billing_start_day 00:00:00Z
    last_day_this = calendar.monthrange(now_dt.year, now_dt.month)[1]
    clamped_day = min(start_day, last_day_this)
    target_reset_utc = datetime(now_dt.year, now_dt.month, clamped_day, 0, 0, 0, tzinfo=timezone.utc)

    # now >= target_reset_utc 이면 다음 달로 이동
    if now_dt >= target_reset_utc:
        next_year = now_dt.year if now_dt.month < 12 else now_dt.year + 1
        next_month = now_dt.month + 1 if now_dt.month < 12 else 1
        last_day_next = calendar.monthrange(next_year, next_month)[1]
        clamped_next = min(start_day, last_day_next)
        target_reset_utc = datetime(next_year, next_month, clamped_next, 0, 0, 0, tzinfo=timezone.utc)

    # snap_target_yyyymm = target_reset_utc 직전 달
    snap_target_yyyymm = (target_reset_utc - timedelta(seconds=1)).strftime("%Y%m")
    # reset_target_yyyymm = target_reset_utc 가 속한 달
    reset_target_yyyymm = target_reset_utc.strftime("%Y%m")
    # target_snapshot_utc
    target_snapshot_utc = target_reset_utc - timedelta(seconds=pre_snapshot_seconds)

    bd_changed = False

    # 스냅샷 트리거
    if last_snapshot_yyyymm != snap_target_yyyymm and target_snapshot_utc <= now_dt < target_reset_utc:
        _save_monthly_snapshot(state, now_dt)
        bd["last_snapshot_yyyymm"] = snap_target_yyyymm
        bd_changed = True
        logging.info(f"[Rollover] Monthly snapshot taken: {snap_target_yyyymm}")

    # 리셋 트리거 (옵션 B: 파일 수치 자체를 0으로 초기화)
    if last_reset_yyyymm != reset_target_yyyymm and now_dt >= target_reset_utc:
        for _, info in state.get("users", {}).items():
            if not isinstance(info, dict):
                continue
            info["total_bytes"] = 0
            info["_last_raw_bytes"] = 0
            info["_offset_bytes"] = 0

        bd["last_reset_yyyymm"] = reset_target_yyyymm
        bd_changed = True
        logging.info(f"[Rollover] Monthly reset done (HARD ZERO): {reset_target_yyyymm}")
        
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
