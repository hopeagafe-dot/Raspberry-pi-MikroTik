#!/usr/bin/env python3
"""
create_user_config.py

- RouterOS Hotspot 사용자 목록 + usage_public.json 을 기반으로
  ~/mcepi_data/user_config.json 을 생성/갱신한다.

운영 철학:

1) RouterOS 에 존재하는 USER 가 기준(정답)이다.
2) 기존 user_config.json 에 이미 저장된 값이 있다면:
   - limit_gb          : 가능한 한 그대로 유지 (중요 설정)
   - personal_prepaid  : 그대로 유지 (돈 관련, 절대 날리면 안 됨)
   - is_manager        : 기존 True 이면 유지 (또는 이름이 *_Manager 이면 True)
   - role / active     : "유지하지 않는다" → RouterOS 기준으로 기본값 재설정
3) RouterOS 에는 더 이상 없지만, user_config.json 에만 있던 USER:
   - 남겨둘 필요 없다고 요청 → user_config 에서도 완전히 제거
"""

from pathlib import Path
import os
import json
from librouteros import connect

# ===== 기본 경로 =====
BASE_DIR = Path.home()
DATA_DIR = BASE_DIR / "mcepi_data"
#USAGE_PUBLIC_PATH = DATA_DIR / "usage_public.json"
USER_CONFIG_PATH = DATA_DIR / "user_config.json"


# 🔹 ===== RouterOS 접속 정보 =====
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
ROUTER_PORT = 8728


def safe_read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


    # 2) 기존 user_config.json (있을 경우)
    existing_cfg = safe_read_json(USER_CONFIG_PATH, {"users": []})
    old_users_map = {
        u.get("user"): u
        for u in existing_cfg.get("users", [])
        if u.get("user")
    }

    # 3) RouterOS 연결
    api = connect(
        username=ROUTER_USER,
        password=ROUTER_PASS,
        host=ROUTER_HOST,
        port=ROUTER_PORT,
    )

    # 4) Hotspot user 목록 가져오기 (RouterOS 기준 정답 리스트)
    hotspot_users = list(api("/ip/hotspot/user/print"))

    cfg_users = []
    processed_names = set()

    # --- 4-1) RouterOS 에 실제 존재하는 USER 들 처리 ---
    for u in hotspot_users:
        name = u.get("name")
        if not name:
            continue

        processed_names.add(name)

        comment = (u.get("comment") or "").strip()
        display_name = comment if comment else name

        old = old_users_map.get(name, {})

        # limit_gb : 유지
        try:
            limit_gb = float(old.get("limit_gb", 100.0))
        except Exception:
            limit_gb = 100.0

        # personal_prepaid : 유지
        try:
            personal_prepaid = float(old.get("personal_prepaid", 0.0))
        except Exception:
            personal_prepaid = 0.0

        # role / active 는 RouterOS 기준으로 재설정
        role = "crew"
        active = True
        
        # ✅ username에 default 포함 시 public으로 강제
        if isinstance(name, str) and ("default" in name.lower()):
            role = "public"

        # is_manager :
        #  - 이름이 *_Manager 면 True
        #  - 기존 설정에서 True 였으면 그대로 True 유지
        is_mgr_from_name = name.endswith("_Manager")
        is_mgr_old = bool(old.get("is_manager", False))
        is_mgr = is_mgr_from_name or is_mgr_old

        cfg_users.append(
            {
                "user": name,
                "display_name": display_name,
                "limit_gb": limit_gb,
                "is_manager": is_mgr,
                "personal_prepaid": personal_prepaid,
                "active": active,
                "role": role,
                "comment": comment,
            }
        )

    # --- 4-2) RouterOS 에 없는 user 는 이제 완전히 제거 ---
    # (요청사항에 따라 아카이브하지 않음)

    # 5) 이름 기준 정렬
    cfg_users.sort(key=lambda x: x["user"])

    # 6) 저장
    out = {"users": cfg_users}
    with USER_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[OK] user_config.json 생성/갱신 완료: {USER_CONFIG_PATH}")


if __name__ == "__main__":
    main()
