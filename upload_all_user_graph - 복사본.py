import sys
import paramiko
from pathlib import Path

REMOTE_HOST = "192.168.88.31"
REMOTE_PORT = 22
REMOTE_USER  = "YourPiName" # you must Change"
REMOTE_PASS  = "PassWordHear" # you must Change"
LOCAL_FILE  = "all_user_graph.html"
REMOTE_PATH = "/home/mce/Templates/all_user_graph.html"

def main():
    base_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    local_path = base_dir / LOCAL_FILE

    print(f"[업로드] {local_path} -> {REMOTE_USER}@{REMOTE_HOST}:{REMOTE_PATH}")

    transport = None
    sftp = None
    try:
        transport = paramiko.Transport((REMOTE_HOST, REMOTE_PORT))
        transport.connect(username=REMOTE_USER, password=REMOTE_PASS)
        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.put(str(local_path), REMOTE_PATH)
        print("[완료] 업로드 성공")
    except paramiko.AuthenticationException:
        print("[오류] 인증 실패")
    except FileNotFoundError as e:
        print(f"[오류] 로컬 파일 없음: {e}")
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
