# -*- coding: utf-8 -*-
import os
import sys
import time
import shutil
import subprocess

PORT = 5005

# 1. Xác định chuẩn xác thư mục gốc của ứng dụng (dù chạy file .py hay file .exe PyInstaller)
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

os.chdir(APP_DIR)


def kiem_tra_python(cmd: str) -> bool:
    if not cmd or not os.path.exists(cmd):
        return False
    try:
        res = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
        out = (res.stdout or "") + (res.stderr or "")
        return res.returncode == 0 and "Python" in out
    except Exception:
        return False


def tim_python_runtime() -> str:
    # 1. Ưu tiên số 1: python_runtime nhúng kèm trong thư mục tool
    local_py = os.path.join(APP_DIR, "python_runtime", "python.exe")
    if os.path.exists(local_py):
        return local_py

    # 2. Thử thư mục cha (nếu có)
    parent_py = os.path.join(APP_DIR, "..", "python_runtime", "python.exe")
    if os.path.exists(parent_py):
        return os.path.abspath(parent_py)

    # 3. Nếu đang chạy bằng python script thuần
    if not getattr(sys, 'frozen', False):
        return sys.executable

    # 4. Tìm trong PATH của máy khách
    for c in ["python", "python3", "py"]:
        found = shutil.which(c)
        if found and kiem_tra_python(found):
            return found

    # 5. Các đường dẫn mặc định của Windows
    local_app = os.environ.get("LOCALAPPDATA", "")
    for p in [
        os.path.join(local_app, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app, "Programs", "Python", "Python311", "python.exe"),
        r"C:\Program Files\Python312\python.exe",
        r"C:\Python312\python.exe",
    ]:
        if os.path.exists(p) and kiem_tra_python(p):
            return p

    return ""


def giai_phong_cong(port: int):
    try:
        cmd = f'netstat -aon | findstr ":{port}" | findstr "LISTENING"'
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if res.stdout:
            pids = set()
            for line in res.stdout.strip().splitlines():
                parts = line.strip().split()
                if len(parts) >= 5 and parts[-1].isdigit():
                    pid = parts[-1]
                    if pid != str(os.getpid()):
                        pids.add(pid)
            for p in pids:
                print(f"[*] Giai phong tien trinh cu chiem cong {port} (PID: {p})...")
                subprocess.run(f"taskkill /F /PID {p}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
    except Exception:
        pass


def main():
    print("=" * 70)
    print("  🚀 GIAM SAT BAI NO 24/24 - BAN DOC LAP (100% OFFLINE)")
    print("  🛡️ Quet Views Reels Facebook, Tu Bao Telegram & Canh Bao 0 Cmt")
    print("=" * 70)
    print()

    # Cấu hình Chromium Offline
    local_pw = os.path.join(APP_DIR, "ms-playwright")
    if os.path.isdir(local_pw):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = local_pw
        print("[+] Chromium Offline:", local_pw)

    # Tìm Python
    py_cmd = tim_python_runtime()
    if not py_cmd:
        print("❌ LOI: Khong tim thay Python runtime de khoi chay ung dung!")
        print("   Vui long kiem tra thu muc 'python_runtime' da duoc giai nen day du.")
        print("=" * 70)
        input("Nhan Enter de thoat...")
        return

    # TUYỆT ĐỐI CHẶN VÒNG LẶP: Nếu py_cmd trùng với chính file exe này -> Dừng ngay lập tức!
    if getattr(sys, 'frozen', False):
        if os.path.abspath(py_cmd).lower() == os.path.abspath(sys.executable).lower():
            print("❌ LOI NGHIM TRONG: Khong tim thay python_runtime/python.exe!")
            input("Nhan Enter de thoat...")
            return

    print("[+] Trinh thong dich Python:", py_cmd)

    # Giải phóng cổng 5005
    giai_phong_cong(PORT)

    url = f"http://127.0.0.1:{PORT}"
    print(f"\n[*] May chu dang khoi dong tai: {url}")
    print("[*] Vui long mo trinh duyet va truy cap dia chi tren de su dung.\n")

    webui_script = os.path.join(APP_DIR, "webui_giam_sat.py")
    if not os.path.exists(webui_script):
        print(f"❌ Khong tim thay file ung dung: {webui_script}")
        input("Nhan Enter de thoat...")
        return

    try:
        subprocess.run([py_cmd, webui_script], cwd=APP_DIR)
    except KeyboardInterrupt:
        print("\n[!] Da dung may chu.")
    except Exception as e:
        print(f"\n❌ Loi khoi chay: {e}")

    print("\n" + "=" * 70)
    input("Nhan Enter de thoat...")


if __name__ == "__main__":
    main()
