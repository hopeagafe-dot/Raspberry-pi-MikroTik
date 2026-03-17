import sys
import paramiko
from pathlib import Path

REMOTE_HOST  = "192.168.88.31"
REMOTE_PORT  = 22
REMOTE_USER  = "YourPiName" # you must Change"
REMOTE_PASS  = "PassWordHear" # you must Change"
REMOTE_DIR   = "/home/mce/mcepi_data/monthly_archives/"
LOCAL_FOLDER = "monthly_archives"

def main():
    base_dir   = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    local_path = base_dir / LOCAL_FOLDER
    local_path.mkdir(parents=True, exist_ok=True)

    print(f"[다운로드] {REMOTE_USER}@{REMOTE_HOST}:{REMOTE_DIR} -> {local_path}")

    transport = None
    sftp      = None
    try:
        transport = paramiko.Transport((REMOTE_HOST, REMOTE_PORT))
        transport.connect(username=REMOTE_USER, password=REMOTE_PASS)
        sftp = paramiko.SFTPClient.from_transport(transport)

        entries    = sftp.listdir_attr(REMOTE_DIR)
        files_only = [e for e in entries if not (e.st_mode & 0o40000)]

        if not files_only:
            print("[안내] 다운로드할 파일이 없습니다.")
        else:
            print(f"[안내] 파일 {len(files_only)}개 다운로드 시작...")
            for entry in files_only:
                remote_file = REMOTE_DIR.rstrip("/") + "/" + entry.filename
                local_file  = local_path / entry.filename
                print(f"  ← {entry.filename}")
                sftp.get(remote_file, str(local_file))
            print(f"[완료] {len(files_only)}개 파일 다운로드 성공 → {local_path}")

    except paramiko.AuthenticationException:
        print("[오류] 인증 실패")
    except FileNotFoundError as e:
        print(f"[오류] 원격 경로를 찾을 수 없음: {e}")
    except Exception as e:
        print(f"[오류] {type(e).__name__}: {e}")
    finally:
        if sftp:
            sftp.close()
        if transport:
            transport.close()

    input("\n엔터를 눌러 종료...")

if __name__ == "__main__":
    main()
