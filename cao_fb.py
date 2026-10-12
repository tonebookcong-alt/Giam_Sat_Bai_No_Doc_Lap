#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cào Facebook Page bằng TRÌNH DUYỆT THẬT (Edge/Chrome có sẵn trên máy) qua Playwright.

Vì sao: Facebook 2026 chặn mọi truy cập "ẩn danh" bằng requests — trang chỉ render
trong trình duyệt có đăng nhập. Cách này mở đúng trang profile, cuộn để tải thêm
bài, rồi đọc dữ liệu ngay từ DOM (text, cảm xúc, bình luận, chia sẻ, ảnh).

Đầu ra: danh sách post dạng dict GIỐNG hệt định dạng cũ của scraper.py:
    post_id, time, text, images, post_url, likes, comments, shares
"""

import hashlib
import json
import os
import re
import time
import gc as _gc
from datetime import datetime, timedelta

# Tự động nhận diện thư mục ms-playwright nhúng sẵn trong tool (100% Offline)
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    _thu_muc_hien_tai = os.path.dirname(os.path.abspath(__file__))
    for _thu_muc_ung_vien in [
        os.path.join(_thu_muc_hien_tai, "ms-playwright"),
        os.path.join(os.path.dirname(_thu_muc_hien_tai), "ms-playwright"),
        os.path.join(_thu_muc_hien_tai, "core", "ms-playwright")
    ]:
        if os.path.isdir(_thu_muc_ung_vien):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _thu_muc_ung_vien
            break

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Khoảng cách (px) tối đa giữa text bài và thanh tương tác (toolbar) của CÙNG bài.
# Nếu chênh lệch lớn hơn -> chúng thuộc 2 bài khác nhau.
NGUONG_GHEP = 2500


def _cung_mot_bai(a: str, b: str) -> bool:
    """Kiểm tra 2 nội dung có phải CÙNG MỘT bài (sau khi đã bỏ header/emoji).

    Chỉ coi là trùng khi gần như GIỐNG HỆT — KHÔNG dùng tiền tố 120 ký tự
    (vì 2 bài khác nhau có chung mở đầu/template sẽ bị gộp nhầm thành 1,
    khiến đặt 10 bài chỉ cào được 3-4 bài).
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    # Bỏ khoảng trắng để so chính xác hơn
    ga = re.sub(r"\s+", "", a)
    gb = re.sub(r"\s+", "", b)
    if not ga or not gb:
        return False
    # Giống hệt toàn bộ -> chắc chắn trùng
    if ga == gb:
        return True
    # Một bản là TIỀN TỐ đầy đủ của bản kia (bản ngắn hơn nằm trọn ở đầu bản dài)
    # -> cùng 1 bài được render 2 lần (bản ngắn trước khi bấm "Xem thêm").
    # Yêu cầu bản ngắn phải đủ dài (>= 60 ký tự) để tránh gộp nhầm 2 bài
    # khác nhau chỉ vì mở đầu trùng vài từ.
    if len(ga) >= 60 and len(gb) >= 60:
        ngan, dai = (ga, gb) if len(ga) <= len(gb) else (gb, ga)
        if dai.startswith(ngan):
            return True
    return False


def _map_same_site(val):
    """Chuyển sameSite từ định dạng trình duyệt sang Playwright.
    Trình duyệt export: 'no_restriction'/'lax'/'strict'/null
    Playwright cần: 'None'/'Lax'/'Strict'
    """
    if not val or val == "null":
        return "Lax"  # mặc định an toàn
    v = str(val).lower().replace("_", "")
    if v in ("norestriction", "none"):
        return "None"
    if v == "strict":
        return "Strict"
    return "Lax"


def doc_cookies(path: str):
    """Đọc cookies.txt -> list cookies cho Playwright.
    Truyền đủ httpOnly, secure, sameSite để Facebook nhận phiên đăng nhập.

    utf-8-sig: Notepad hay lưu file kèm ký tự ẩn BOM ở đầu file. Nếu không gỡ
    ra thì file JSON (bắt đầu bằng '[') đọc thành "[BOM[" và hỏng bước parse.
    """
    if not path:
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        noi_dung = f.read().strip().lstrip("\ufeff\u200b").strip()
    if not noi_dung:
        return []
    if noi_dung.startswith("["):
        try:
            ds = json.loads(noi_dung)
            if isinstance(ds, list):
                ket_qua = []
                for c in ds:
                    if not isinstance(c, dict) or not c.get("name"):
                        continue
                    cookie = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".facebook.com"),
                        "path": c.get("path", "/"),
                        "httpOnly": bool(c.get("httpOnly", False)),
                        "secure": bool(c.get("secure", True)),
                        "sameSite": _map_same_site(c.get("sameSite")),
                    }
                    ket_qua.append(cookie)
                return ket_qua
        except json.JSONDecodeError:
            pass
    cookies = {}
    for line in noi_dung.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
        elif "=" in line:
            for cap in line.split(";"):
                cap = cap.strip()
                if "=" in cap:
                    k, v = cap.split("=", 1)
                    cookies[k.strip()] = v.strip()
    return [{"name": k, "value": v, "domain": ".facebook.com", "path": "/",
             "httpOnly": False, "secure": True, "sameSite": "None"}
            for k, v in cookies.items()]


# ===================================================================
# PROXY (mỗi tài khoản một IP dân cư VN — sticky 1:1)
# ===================================================================
def _la_nhom_hex(t: str) -> bool:
    return bool(t) and all(ch in "0123456789abcdefABCDEF" for ch in t)


def _chuan_hoa_ipv6(s: str) -> str:
    """Chuẩn hoá 1 dòng proxy IPv6 -> URL 'http://[host]:port' hoặc
    'http://user:pass@[host]:port'.

    Host IPv6 PHẢI nằm trong ngoặc vuông khi đưa vào URL (Chromium, requests,
    urllib đều yêu cầu dạng '[2001:db8::1]:30017'). Hàm tự bọc lại nếu người
    dùng ghi trần không ngoặc.

    Quy tắc giải nhập nhằng (không ngoặc, không credential): đuôi ':digits'
    chỉ là PORT khi bỏ đi rồi phần host VẪN còn >= 3 nhóm hex (vd
    '2001:db8::1:8080' -> host '2001:db8::1' + port 8080; còn '2001:db8::1'
    là địa chỉ thuần, không có port). Cách an toàn nhất vẫn là ghi ngoặc.
    """
    s = (s or "").strip()
    if s.startswith("["):
        # [ipv6] | [ipv6]:port | [ipv6]:port:user:pass | [ipv6]:user:pass
        m = re.match(
            r"^(\[[0-9a-fA-F:.]+\])(?::(\d+))?(?::([^:@\[\]]+):([^@\[\]]*))?$", s)
        if m:
            host, port, user, pw = m.groups()
            if user is not None:
                return f"http://{user}:{pw}@{host}:{port or 80}"
            return f"http://{host}:{port}" if port else f"http://{host}"
        return s
    parts = s.split(":")
    # 'host...:port:user:pass' -> port là nhóm NGAY trước 2 nhóm credential.
    # Chỉ nhận khi phần host là IPv6 hợp lệ: toàn nhóm hex, VÀ có '::' hoặt
    # đúng 8 nhóm (nếu không, có thể đó là địa chỉ đầy đủ + port ở cuối).
    if len(parts) >= 5 and parts[-3].isdigit():
        host_groups = [g for g in parts[:-3] if g]
        if all(_la_nhom_hex(g) for g in host_groups) and (
                "::" in s or len(host_groups) == 8):
            port, user, pw = parts[-3], parts[-2], parts[-1]
            return f"http://{user}:{pw}@[{':'.join(parts[:-3])}]:{port}"
    # 'host...[:port]' (không credential): đuôi ':digits' là PORT khi bỏ nó ra
    # rồi phần host VẪN là địa chỉ hợp lệ — dạng nén '::' (đã hoàn chỉnh) hoặc
    # đầy đủ đúng 8 nhóm hex. VD '::1:8080' -> host '::1' + port 8080;
    # '2001:db8:0:0:0:0:0:1:8080' -> host 8 nhóm + port 8080.
    if parts[-1].isdigit():
        host = ":".join(parts[:-1])
        nhom = [g for g in host.split(":") if g]
        if "::" in host or (len(nhom) == 8 and all(_la_nhom_hex(g) for g in nhom)):
            return f"http://[{host}]:{parts[-1]}"
    return f"http://[{s}]"                # IPv6 thuần, không có port


def chuan_hoa_proxy(s: str) -> str:
    """Chuẩn 1 dòng proxy về dạng URL mà Playwright + requests đều hiểu.

    Chấp nhận: 'http://user:pass@host:port', 'host:port', 'host:port:user:pass',
    'socks5://host:port', có/không có scheme.

    IPv6 (từ 2026-09-16): host có dấu '::' hoặc từ 2 dấu ':' trở lên. Nên ghi
    dạng '[2001:db8::1]:30017' (chuẩn URL); ghi trần '2001:db8::1:30017' cũng
    tự nhận. Dạng IPv4 'host:port' (1 dấu ':') và 'host:port:user:pass' (3 dấu
    ':') giữ nguyên như cũ.
    """
    s = (s or "").strip()
    if not s or s.startswith("#"):
        return ""
    if "://" in s:
        return s
    n = s.count(":")
    # IPv6: có ngoặc, có '::', hoặc số dấu ':' không khớp cấu trúc IPv4 (1 hoặc 3)
    if s.startswith("[") or "::" in s or (n >= 2 and n != 3):
        return _chuan_hoa_ipv6(s)
    parts = s.split(":")
    if len(parts) == 2:            # host:port (IPv4 / tên miền)
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:            # host:port:user:pass (IPv4)
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return s


# Từ khóa nói "dòng này KHÔNG dùng proxy = IP mặc định của máy"
_TU_KHOA_LOCAL = {"-", "local", "direct", "none", "no", "khong", "không",
                  "ip-may", "ipmay", "ip_may", "may", "máy", "trống"}


def phai_dong_local(s: str) -> bool:
    """Dòng proxies.txt có mang nghĩa 'IP máy' không (không tính dòng chú thích #)."""
    t = (s or "").strip().lower()
    return bool(t) and not t.startswith("#") and t in _TU_KHOA_LOCAL


def doc_proxies(path: str = "proxies.txt") -> list:
    """Đọc proxies.txt -> danh sách proxy đã chuẩn hóa, THEO ĐÚNG THỨ TỰ DÒNG.

    Số phần tử = số dòng có nội dung (bỏ dòng trống và dòng bắt đầu bằng '#').
    Dòng '-', 'local', 'direct', 'ip-may', 'khong' -> None, nghĩa là phiên
    tương ứng chạy bằng IP mặc định của máy (vẫn giữ vị trí 1:1 với cookie).

    Đọc utf-8-sig: Notepad/PowerShell hay lưu file kèm BOM, nếu không xử lý thì
    ký tự \ufeff bám vào dòng ĐẦU làm dòng đầu tiên không nhận ra.
    """
    from config import DUONG_DAN
    if not path:
        return []
    if not os.path.exists(path):
        duong_dan_goc = os.path.join(DUONG_DAN, os.path.basename(path))
        if os.path.exists(duong_dan_goc):
            path = duong_dan_goc
        else:
            return []
    with open(path, "r", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    ds = []
    for dong in lines:
        dong = dong.replace("\ufeff", "").replace("\u200b", "")
        if not dong.strip() or dong.strip().startswith("#"):
            continue
        if phai_dong_local(dong):
            ds.append(None)
            continue
        p = chuan_hoa_proxy(dong)
        if p:
            ds.append(p)
    return ds


def _ru_mat_khau(px: str) -> str:
    """'http://user:pass@host:port' -> 'http://user:***@host:port' (để log an toàn)."""
    return re.sub(r"(//[^/@:]+:)[^@]+@", r"\1***@", px or "")


def proxy_dict(server: str):
    """Chuyển dòng proxy -> dict cho Playwright (TÁCH username/password riêng).

    Playwright/Chromium KHÔNG chấp nhận credential nhúng trong trường 'server'
    ('http://user:pass@host:port') -> mọi page.goto fail ngay từ đầu với
    'net::ERR_INVALID_AUTH_CREDENTIALS' và phiên cào trả về 0 bài.
    (requests thì vẫn chấp nhận, nên nút 'Test proxy' trên UI báo OK trong khi
    trình duyệt không đi qua proxy được -> phải tách 3 trường mới đúng.)
    """
    p = chuan_hoa_proxy(server)
    if not p:
        return None
    # Regex IPv4: 'http://user:pass@host:port' (user/pass không chứa ':' '@')
    # Regex IPv6: host nằm trong [..] -> '(//[^/]+)' phải nới thành '(//[^/]+)'
    m = re.match(r"(https?://)([^/@:\[\]]+):([^@]*)@(\[[^\]]+\]):(\d+)$", p) \
        or re.match(r"(https?://)([^/@:\[\]]+):([^@]*)@([^:]+):(\d+)$", p) \
        or re.match(r"(https?://)(\[[^\]]+\]):(\d+)$", p) \
        or re.match(r"(https?://)([^:]+):(\d+)$", p)
    if m and len(m.groups()) == 5:
        scheme, user, pw, host, port = m.groups()
        from urllib.parse import unquote
        return {"server": f"{scheme}{host}:{port}",
                "username": unquote(user), "password": unquote(pw)}
    if m and len(m.groups()) == 3:             # host:port, không credential
        scheme, host, port = m.groups()
        return {"server": f"{scheme}{host}:{port}"}
    return {"server": p}


def kiem_tra_proxy(server: str, thoi_gian: int = 15) -> dict:
    """Test 1 proxy: ra được IP nào, ASN/org nào, có phải VN không.

    Dùng requests qua proxy — cùng đường với lúc tải ảnh, nên kết quả phản
    ánh đúng những gì Facebook nhìn thấy.
    """
    ket_qua = {"ok": False, "proxy": server, "ip": "", "org": "", "quoc_gia": "",
               "loi": "", "la_may": False}
    try:
        import requests
        px = chuan_hoa_proxy(server)
        if not px:                       # không proxy -> test chính IP máy
            ket_qua["la_may"] = True
            proxies = None
        else:
            proxies = {"http": px, "https": px}
        r = requests.get("https://api.ipify.org?format=json", proxies=proxies,
                         timeout=thoi_gian, headers={"User-Agent": DEFAULT_UA})
        r.raise_for_status()
        ket_qua["ip"] = (r.json() or {}).get("ip") or ""
        if ket_qua["ip"]:
            try:
                r2 = requests.get(f"http://ip-api.com/json/{ket_qua['ip']}"
                                  "?fields=status,countryCode,isp,org,as,proxy,hosting",
                                  timeout=thoi_gian)
                j = r2.json() or {}
                ket_qua["quoc_gia"] = j.get("countryCode") or ""
                ket_qua["org"] = " · ".join(x for x in
                                            (j.get("as") or j.get("isp") or "",
                                             j.get("org") or "") if x)
                ket_qua["la_datacenter"] = bool(j.get("hosting"))
                ket_qua["la_proxy_sanh"] = bool(j.get("proxy"))
            except Exception:
                pass
        ket_qua["ok"] = bool(ket_qua["ip"])
    except Exception as e:
        ket_qua["loi"] = str(e)[:200]
    return ket_qua


# ==============================================================================
# TỐI ƯU RAM — tránh mở quá nhiều trình duyệt khiến máy hết RAM / tự reset
# ==============================================================================
RAM_MOI_TRINH_DUYET_MB = 450   # 1 trình duyệt headless tối ưu ~ 350-450MB
RAM_DU_PHONG_HE_THONG_MB = 1536  # chừa lại cho Windows + webui + các tiến trình khác
TRINH_DUYET_TOI_DA_CUNG = 4    # trần an toàn tuyệt đối tránh nghẽn RAM / GPU


def ram_trong_mb() -> int:
    """RAM vật lý còn trống (MB) — đọc trực tiếp từ Windows API, không cần psutil."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return int(st.ullAvailPhys // (1024 * 1024))
    except Exception:
        return 4096  # không đọc được -> giả định an toàn


def gioi_han_trinh_duyet_theo_ram(tran_cung: int = TRINH_DUYET_TOI_DA_CUNG) -> int:
    """Số trình duyệt TỐI ĐA được mở đồng thời theo RAM còn trống hiện tại."""
    trong = ram_trong_mb()
    kha_dung = max(0, trong - RAM_DU_PHONG_HE_THONG_MB)
    return max(1, min(tran_cung, kha_dung // RAM_MOI_TRINH_DUYET_MB))


def chan_tai_nguyen_nang(ctx, chan_anh: bool = False):
    """Chặn tải video/font (và cả ảnh nếu chan_anh=True) trong context.

    DOM vẫn chứa đầy đủ thẻ <img src=...> nên việc trích xuất URL ảnh không bị
    ảnh hưởng, nhưng trình duyệt không tải/giải mã dữ liệu nặng -> tiết kiệm
    vài trăm MB RAM mỗi phiên và giảm băng thông đáng kể.
    """
    loai_chan = {"media", "font"} | ({"image"} if chan_anh else set())

    def _route(route):
        try:
            if route.request.resource_type in loai_chan:
                return route.abort()
        except Exception:
            pass
        try:
            return route.continue_()
        except Exception:
            pass

    try:
        ctx.route("**/*", _route)
    except Exception:
        pass


def don_dep_trang(page=None, ctx=None):
    """Điều hướng về about:blank và đóng sạch page / context để giải phóng DOM/V8 heap."""
    if page:
        try:
            page.goto("about:blank", timeout=2000)
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass
    if ctx:
        try:
            ctx.close()
        except Exception:
            pass
    try:
        import gc
        gc.collect()
    except Exception:
        pass


def mo_trinh_duyet(playwright, cookies: list = None, proxy: str = None):
    """Mở Chromium Headless của Playwright với bộ cờ tối ưu RAM triệt để,
    dập tắt hoàn toàn GPU/DirectX để chống sập nguồn kernel (BSOD 0x10E);
    lỗi thì fallback sang Chrome hoặc Edge."""
    args_tiet_kiem = [
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu",
        "--disable-gpu-compositing",
        "--disable-gpu-rasterization",
        "--disable-software-rasterizer",
        "--disable-accelerated-2d-canvas",
        "--disable-features=AcceleratedVideoDecode,AcceleratedVideoEncode",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-component-update",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--mute-audio",
        "--no-first-run",
        "--js-flags=--max-old-space-size=256",
    ]
    px = proxy_dict(proxy) if proxy else None

    # 1. Ưu tiên số 1: Chromium chuẩn của Playwright (độc lập, sạch rác, không kích hoạt DirectX/GPU nền)
    try:
        return playwright.chromium.launch(
            headless=True, proxy=px, args=args_tiet_kiem
        )
    except Exception:
        pass

    # 2. Fallback sang trình duyệt có sẵn trên máy nếu môi trường chưa có Chromium
    for kenh in ["chrome", "msedge"]:
        try:
            return playwright.chromium.launch(
                channel=kenh, headless=True, proxy=px, args=args_tiet_kiem
            )
        except Exception:
            continue

    # 3. Tự động phục hồi: Tự tải và cài đặt Playwright Chromium nếu máy khách chưa có
    print("\n  ⚠️ [TỰ ĐỘNG CÀI ĐẶT] Máy chưa có trình duyệt Playwright Chromium.")
    print("     Đang tự động tải về Chromium (khoảng 30-60s, chỉ diễn ra 1 lần duy nhất)...")
    try:
        import subprocess, sys
        py_exe = sys.executable or "python"
        res_inst = subprocess.run([py_exe, "-m", "playwright", "install", "chromium"], timeout=180)
        if res_inst.returncode == 0:
            print("  ✅ Đã tải và cài đặt Playwright Chromium thành công! Đang mở trình duyệt...")
            return playwright.chromium.launch(
                headless=True, proxy=px, args=args_tiet_kiem
            )
    except Exception as e_inst:
        print(f"  ❌ Không thể tự động cài chromium: {e_inst}")

    raise RuntimeError(
        "Không mở được trình duyệt. Hãy cài Chromium cho Playwright:\n"
        "    python -m playwright install chromium"
    )


def _parse_so(so_text: str) -> int:
    """Hỗ trợ chuẩn hoá cả số US (1.2K, 1.8M, 2,100) lẫn VN/Bắc Âu (1,2K, 1,5 Tr, 2.100)."""
    if not so_text:
        return 0
    raw = str(so_text).strip()
    if not raw:
        return 0

    # CHẶN BẢO VỆ: Thời gian Facebook (19m, 14m, 2h, 3d, 5s, 1w, 21 phút) TUYỆT ĐỐI không phải số view
    # Trên Facebook, '19m' = 19 phút trước. Chữ 'm' thường đứng ngay sau số mà không có 'view'/'play' = PHÚT!
    if re.match(r"^\d+\s*[smhdw]$", raw) or any(t in raw.lower() for t in ["phút", "giờ", "ngày", "tuần", "tháng", "năm", "min", "sec", "hour", "day"]):
        return 0

    s = raw.lower().replace(" ", "")
    he_so = 1
    if s.endswith("k"):
        he_so = 1000
        s = s[:-1]
    elif raw.endswith("M") or s.endswith("tr") or s.endswith("triệu") or "million" in s:
        # Chỉ nhận Million khi là chữ 'M' viết hoa hoặc có 'tr'/'triệu'/'million' rõ ràng
        he_so = 1000000
        for suf in ["triệu", "million", "tr", "m"]:
            if s.endswith(suf):
                s = s[:-len(suf)]
                break
    elif raw.endswith("B") or s.endswith("tỷ") or "billion" in s:
        he_so = 1000000000
        for suf in ["billion", "tỷ", "b"]:
            if s.endswith(suf):
                s = s[:-len(suf)]
                break

    if "." in s and "," in s:
        if s.find(",") < s.find("."):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        if he_so != 1:
            s = s.replace(",", ".")
        else:
            parts = s.split(",")
            if len(parts) == 2 and len(parts[1]) == 3:
                s = s.replace(",", "")
            else:
                s = s.replace(",", ".")
    elif "." in s:
        if he_so == 1:
            parts = s.split(".")
            if len(parts) == 2 and len(parts[1]) == 3:
                s = s.replace(".", "")

    try:
        return int(float(s) * he_so)
    except ValueError:
        return 0


def _parse_thoi_gian(label: str):
    """'Thứ bảy, 22 Tháng 8, 2026 lúc 20:02' -> datetime.
    'Hôm nay lúc 20:02' / '7 giờ' / '3 ngày' -> datetime gần đúng.
    Không hiểu được -> trả về None."""
    if not label:
        return None
    label = label.strip()
    m = re.search(r"(\d{1,2})\s+Tháng\s+(\d{1,2}),?\s+(\d{4})\s*lúc\s*(\d{1,2}):(\d{2})", label)
    if m:
        try:
            ngay, thang, nam, gio, phut = map(int, m.groups())
            return datetime(nam, thang, ngay, gio, phut)
        except ValueError:
            return None
    gio_ht = datetime.now()
    if "Hôm nay" in label:
        m = re.search(r"lúc\s*(\d{1,2}):(\d{2})", label)
        if m:
            return gio_ht.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                  second=0, microsecond=0)
    if "Hôm qua" in label:
        m = re.search(r"lúc\s*(\d{1,2}):(\d{2})", label)
        if m:
            return (gio_ht - timedelta(days=1)).replace(
                hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    m = re.search(r"(\d+)\s*giờ", label)
    if m:
        return gio_ht - timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s*ngày", label)
    if m:
        return gio_ht - timedelta(days=int(m.group(1)))
    return None


def _trich_so_lieu(toolbar_text: str):
    """'1,5K 64 4' -> (1500, 64, 4, views). Các số = cảm xúc, bình luận, chia sẻ, lượt xem.
    Hỗ trợ cả chế độ khách (Guest mode không cookies): 'All reactions: 48 6 3 Like Comment'.
    Tự động nhận diện và bóc tách riêng LƯỢT XEM / VIEWS / PLAYS trên Video & Reel."""
    txt = toolbar_text or ""

    # 1. Phát hiện và loại bỏ LƯỢT XEM / LƯỢT PHÁT / VIEWS / PLAYS (tiếng Anh, Việt, Bắc Âu/Tây Âu)
    tu_khoa_views = r"(?:views?|plays?|lượt\s*xem|lượt\s*phát|visningar|weergaven|aufrufe|vues)"
    m_view = re.search(rf"(\d[\d.,]*\s*(?:[kKmM]|tr|Tr)?)\s*{tu_khoa_views}", txt, re.IGNORECASE)
    if not m_view:
        m_view = re.search(rf"{tu_khoa_views}[:\s]*(\d[\d.,]*\s*(?:[kKmM]|tr|Tr)?)", txt, re.IGNORECASE)
    if not m_view:
        m_view = re.search(r"(?:▶|►)\s*(\d[\d.,]*\s*(?:[kKmM]|tr|Tr)?)", txt, re.IGNORECASE)
    views = _parse_so(m_view.group(1)) if m_view else 0
    txt_no_view = re.sub(rf"\d[\d.,]*\s*(?:[kKmM]|tr|Tr)?\s*{tu_khoa_views}", " ", txt, flags=re.IGNORECASE)
    txt_no_view = re.sub(rf"{tu_khoa_views}[:\s]*\d[\d.,]*\s*(?:[kKmM]|tr|Tr)?", " ", txt_no_view, flags=re.IGNORECASE)
    txt_no_view = re.sub(r"(?:▶|►)\s*\d[\d.,]*\s*(?:[kKmM]|tr|Tr)?", " ", txt_no_view, flags=re.IGNORECASE)

    # 2. Bình luận có nhãn rõ ràng
    tu_khoa_cmt = r"(?:comments?|bình\s*luận|kommentarer|reacties|kommentare)"
    m_cmt = re.search(rf"(\d[\d.,]*\s*[kKmM]?)\s*{tu_khoa_cmt}", txt_no_view, re.IGNORECASE)
    comments = _parse_so(m_cmt.group(1)) if m_cmt else None

    # 3. Chia sẻ có nhãn rõ ràng
    tu_khoa_share = r"(?:shares?|chia\s*sẻ|delinger|weergaves|delningar)"
    m_share = re.search(rf"(\d[\d.,]*\s*[kKmM]?)\s*{tu_khoa_share}", txt_no_view, re.IGNORECASE)
    shares = _parse_so(m_share.group(1)) if m_share else None

    # 4. Trích xuất cảm xúc / Likes:
    # Cắt trước các nút hành động (Like, Comment, Share...)
    m = re.search(r'(?:All reactions|Tất cả cảm xúc):?\s*([\s\S]*?)(?:Like|Comment|Share|Thích|Bình\s*luận|Chia\s*sẻ|$)', txt_no_view, re.IGNORECASE)
    text_so = m.group(1) if m else txt_no_view

    # Gỡ các cụm bình luận và chia sẻ đã nhận diện ra khỏi vùng tìm số like để tránh đếm trùng
    if m_cmt:
        text_so = text_so.replace(m_cmt.group(0), " ")
    if m_share:
        text_so = text_so.replace(m_share.group(0), " ")

    cac_so = re.findall(r"\d[\d.,]*\s*[kKmM]?", text_so)
    if not cac_so:
        return 0, (comments or 0), (shares or 0), views

    likes = _parse_so(cac_so[0])
    if comments is None:
        comments = _parse_so(cac_so[1]) if len(cac_so) > 1 else 0
    if shares is None:
        shares = _parse_so(cac_so[2]) if len(cac_so) > 2 else 0

    return likes, comments, shares, views


def _post_id_tu_link(link: str) -> str | None:
    # link trang profile: profile.php?id=...&story_fbid=123&id=...
    m = re.search(r"[?&]story_fbid=(\d+)", link)
    if m:
        return m.group(1)
    m = re.search(r"/(?:posts|reel|reels|videos|photos|photo\.php)[/\?]?([\w]+)", link)
    if m:
        return m.group(1)
    m = re.search(r"(pfbid\w+)", link)
    return m.group(1) if m else None


def _la_reel(link: str, co_video=None, imgs=None) -> bool:
    """Nhận diện bài dạng REEL/VIDEO → chỉ để ĐÁNH DẤU, không bỏ qua nữa.

    Reel được xử lý như 'reel tĩnh': lấy ảnh bìa (poster) từ DOM cho vào pipeline
    sửa ảnh như bài thường, rồi tự dựng lại video + nhạc bằng reel.tao_reel().
    """
    h = (link or "").split("?")[0]
    if re.search(r"/reels?(?:/|$|\?)", h):
        return True
    if "/videos/" in h and not imgs:
        return True
    if co_video and not imgs:
        return True
    return False


# Dòng FB hiện ra khi feed đã hết bài (cuộn tới đáy). Nhiều trang chỉ hiện
# đúng 1 dòng này — nhận ra nó để DỪNG sớm thay vì cuộn vô ích.
# Chỉ so với text NGẮN (<160 ký tự) của các phần tử riêng lẻ để không dính
# trường hợp caption bài viết có nhắc tới cụm từ này.
_HET_BAI_PATTERNS = (
    "đó là tất cả",
    "that's all",
    "that's all the posts",
    "no more posts",
    "not many more posts",
    "không còn bài viết nào",
    "bạn đã xem hết",
)


def _den_dich(page) -> bool:
    """Page đang hiện dòng 'hết bài' của Facebook (đã cuộn tới đáy).

    Dùng textContent (KHÔNG innerText) để không phải tính lại layout cho
    từng phần tử — trang FB có hàng nghìn node, innerText sẽ làm chậm hẳn
    vòng cào. Text ngắn (<160 ký tự) tự lọc được các node cha to.
    """
    try:
        return bool(page.evaluate("""(pats) => {
          const els = document.querySelectorAll('div, span, h2, h3, h4, h5, p');
          for (const el of els) {
            let t = '';
            try { t = el.textContent || ''; } catch (e) { continue; }
            if (!t || t.length > 160) continue;
            t = t.trim().toLowerCase();
            if (!t) continue;
            for (const p of pats) if (t.indexOf(p) !== -1) return true;
          }
          return false;
        }""", list(_HET_BAI_PATTERNS)))
    except Exception:
        return False


def _cuon_feed(page, buoc: int = 2400) -> int:
    """Cuộn feed bằng MỌI cách có thể.

    Lý do phải làm đủ kiểu: Facebook có nhiều layout — có layout cuộn theo
    window, có layout cuộn theo một <div> riêng (virtualized). Ngoài ra wheel
    chỉ ăn khi con trỏ ĐANG ĐỨNG TRÊN CỘT FEED; để mặc định ở góc (0,0) thì
    nhiều trang không nạp thêm bài nào (đó là lý do tool "dừng ở 1 bài").
    """
    # Đưa chuột vào giữa cột feed để wheel có tác dụng.
    try:
        page.locator('div[role="feed"]').first.hover(timeout=1000)
    except Exception:
        try:
            page.mouse.move(600, 450)
        except Exception:
            pass
    # 1) Wheel thật (giống người dùng) — nhiều cú ngắn, có nghỉ giữa.
    for _ in range(3):
        try:
            page.mouse.wheel(0, buoc)
            page.wait_for_timeout(300)
        except Exception:
            break
    # 2) Phím End.
    try:
        page.keyboard.press("End")
    except Exception:
        pass
    # 3) JS: cuộn window + cuộn LUÔN cái div cuộn lớn nhất của FB.
    try:
        page.evaluate("""(step) => {
          window.scrollBy(0, step * 3);
          let best = null, bestArea = 0;
          document.querySelectorAll('div').forEach(d => {
            const st = d.scrollHeight - d.clientHeight;
            if (st > 500 && d.clientHeight > 300) {
              const area = d.clientWidth * d.clientHeight;
              if (area > bestArea) { bestArea = area; best = d; }
            }
          });
          if (best) best.scrollBy({top: step * 3, behavior: 'instant'});
          const f = document.querySelector('div[role="feed"]');
          if (f) {
            let n = f;
            for (let k = 0; k < 8 && n; k++) {
              if (n.scrollHeight - n.clientHeight > 500)
                n.scrollBy({top: step * 3, behavior: 'instant'});
              n = n.parentElement;
            }
          }
        }""", buoc)
    except Exception:
        pass
    return _do_cao_dom(page)


def _do_cao_dom(page) -> int:
    """Chiều cao + vị trí cuộn hiện tại của trang (dùng để phát hiện DOM lớn
    thêm khi FB nạp bài mới, kể cả khi selector đếm bài không đổi)."""
    try:
        v = page.evaluate("""() => {
          const de = document.scrollingElement || document.documentElement || {};
          let div = 0;
          document.querySelectorAll('div').forEach(d => {
            if (d.scrollHeight > div && d.clientHeight > 300) div = d.scrollHeight;
          });
          return Math.max((de.scrollHeight || 0) + (de.scrollTop || 0), div);
        }""")
        return int(v or 0)
    except Exception:
        return 0


def _dang_nhap_ep_buoc(page) -> bool:
    """Facebook đang chặn bằng màn hình ĐĂNG NHẬP ép buộc (chuyển hướng sang /login hoặc /checkpoint)."""
    try:
        url = (page.url or "").lower()
        if "/login" in url or "/checkpoint" in url or "login.php" in url:
            return True
        # Nếu trên trang vẫn render được bài viết ([role="article"]) thì KHÔNG phải bị chặn
        has_articles = page.evaluate("() => document.querySelectorAll('[role=\"article\"]').length > 0")
        if has_articles:
            return False
        co = page.evaluate("""() => {
          const t = (document.body && (document.body.textContent || '')).slice(0, 20000).toLowerCase();
          if (t && (t.includes('bạn phải đăng nhập để tiếp tục') || t.includes('you must log in to continue')))
            return true;
          return !!(t && t.includes('bạn quên tài khoản') && t.includes('tạo tài khoản mới')
                    && !document.querySelector('[role="article"]'));
        }""")
        return bool(co)
    except Exception:
        return False


def _session_bi_tu_choi(ctx, cookies_da_nap) -> bool:
    """FB TỰ TAY XÓA cookie đăng nhập (c_user) ngay khi tải trang = phiên đã
    bị vô hiệu hóa server-side (hết hạn / đổi mật khẩu / checkpoint). Bất cứ
    lần cào nào sau đó chỉ là xem bản logged-out — vô ích."""
    try:
        if not any((c or {}).get("name") == "c_user" for c in (cookies_da_nap or [])):
            return False   # file cookie vốn không có c_user -> không kết luận được
        hien = {c["name"] for c in ctx.cookies()}
        return "c_user" not in hien
    except Exception:
        return False


# Cờ báo hiệu "phiên cào bị Facebook từ chối" — đặt khi cào_trang phát hiện
# cookies bị xóa/đòi đăng nhập. run_cào đọc cờ này để DỪNG CẢ LẦN CÀO ngay
# (đừng mở lượt 30s × 80 trang vô ích) và in nguyên nhân gốc rõ ràng.
PHIEN_BI_TU_CHOI = False


def dat_lai_cophien():
    """Xóa cờ trước mỗi lần chạy cào (module được import 1 lần, chạy nhiều lần)."""
    global PHIEN_BI_TU_CHOI
    PHIEN_BI_TU_CHOI = False


def lam_sach_text_bai(text: str) -> str:
    """Làm sạch caption bài viết cào từ Facebook, loại bỏ triệt để rác header/metadata:
    - Bỏ thông báo hệ thống FB: đã cập nhật ảnh bìa, ảnh đại diện
    - Bỏ header metadata ở đầu: Tên trang, Tài khoản đã xác minh, thời gian, Đã chia sẻ với Công khai...
    - Bỏ đuôi rác: Tất cả cảm xúc, Like/Comment/Share, Xem thêm/See more/Ẩn bớt
    - Nếu bài không có nội dung thực sự (< 10 ký tự), trả về "" để loại bỏ.
    """
    if not text:
        return ""
    t = re.sub(r"\s+", " ", str(text).replace("\xa0", " ")).strip()

    # 1. Bỏ thông báo cập nhật ảnh bìa / đại diện
    if re.search(r"đã cập nhật ảnh (?:bìa|đại diện)|updated (?:their|his|her) (?:cover photo|profile picture)", t, re.IGNORECASE):
        return ""

    # 2. Cắt bỏ Header metadata (điểm chốt là "Đã chia sẻ với Công khai" hoặc "Shared with Public")
    pat_chiase = r"·?\s*(?:Đã chia sẻ với|Shared with)\s*(?:Công khai|Bạn bè|Chỉ mình tôi|Những người bạn đã chọn|Tùy chỉnh|Public|Friends|Only me)"
    m = re.search(pat_chiase, t, re.IGNORECASE)
    if m and m.start() <= 250:
        sau = t[m.end():].strip()
        sau = re.sub(r"^\d+\s*", "", sau)
        t = sau

    # 3. Trường hợp header không có "Đã chia sẻ với", nhưng có nhãn AI hoặc "Tài khoản đã xác minh"
    m2 = re.search(r"^.{0,100}?(?:Tài khoản đã xác minh|Verified account|Nội dung do AI tạo)\s*(?:\d+[\s\w]+(?:trước|ago|\.))?\s*·?\s*", t, re.IGNORECASE)
    if m2 and len(m2.group(0)) > 5:
        t = t[m2.end():].strip()

    # 4. Trường hợp chỉ có tên trang + thời gian (VD: "HBOLD.dk6 giờ trước6 giờ")
    m3 = re.match(r"^.{0,80}?\b\d+\s*(?:giờ|ngày|phút|giây|h|m|d)\s*trước\s*(?:\d+\s*(?:giờ|ngày|phút|giây|h|m|d))?\s*·?\s*$", t, re.IGNORECASE)
    if m3:
        return ""

    # 5. Đuôi: bỏ ".../Tất cả cảm xúc:<số>ThíchBình luận" và nhãn UI Facebook
    t = re.sub(r"/?\s*Tất cả cảm xúc:\s*\d+\s*(?:Thích|Bình luận|Chia sẻ)+\s*$", "", t)
    t = re.sub(r"\s*(?:\.\.\.|\…)?\s*(?:Xem thêm|See more|Ẩn bớt|View more|Thêm)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()

    if len(t) < 10:
        return ""

    return t


def la_caption_hop_le(text: str) -> bool:
    """Kiểm tra caption bài viết có hợp lệ hay không.
    Trả về False nếu bài không có caption, caption rỗng, hoặc chỉ là header metadata/đổi ảnh bìa/avatar của Facebook.
    """
    if not text:
        return False
    t = lam_sach_text_bai(text).strip()
    if len(t) < 10:
        return False
    if re.search(r"đã cập nhật ảnh (?:bìa|đại diện)|updated (?:their|his|her) (?:cover photo|profile picture)", t, re.IGNORECASE):
        return False
    if re.match(r"^.{0,80}?\b\d+\s*(?:giờ|ngày|phút|giây|h|m|d)\s*trước\b", t, re.IGNORECASE):
        return False
    return True


def lay_du_lieu_dom(page, so_bai: int) -> list:
    """Đọc toàn bộ bài viết hiện có trong DOM, trả về list dict."""
    js = """
    () => {
      const rectTop = el => Math.round(el.getBoundingClientRect().top);

      // Toolbar bài viết: FB đổi nhãn theo NGÔN NGỮ giao diện của tài khoản.
      // Trước đây chỉ đoán tiếng Việt -> trang hiển thị tiếng Anh đếm ra 0 bài,
      // tool hiểu nhầm "hết bài" và dừng ở 1 bài. Dùng * để bắt cả 2 thứ tiếng.
      const CAC_SEL_TOOLBAR = '[aria-label*="bình luận"], [aria-label*="comment"]';
      const NODE_TOOLBAR = /aria-label="(Thích|Like|Thích nh\u00ect|Bình luận|Comment|Chia s\u1ebb|Share)/;
      const CAC_SEL_XEM_THEM = ['Xem thêm', 'See more', 'View more',
                                'Th\u00eam', 'See more translations'];

      // Ảnh FB: FB lazy-load nên naturalWidth có thể = 0 lúc đọc (chưa decode).
      // - anhFb(): lấy link THẬT từ src / data-src / srcset (chọn bản lớn nhất) —
      //   vì src lúc mới render đôi khi là placeholder nhỏ, ảnh thật nằm ở srcset.
      // - kichThuoc(): đo bằng naturalWidth LẪN kích thước đang hiển thị
      //   (getBoundingClientRect) để KHÔNG loại nhầm ảnh chưa tải xong.
      const co_fbcdn = (u) => u && u.includes('fbcdn');
      const anhFb = (el) => {
        const cac = [];
        const them = (u) => { if (co_fbcdn(u)) cac.push(u); };
        if (el.tagName === 'VIDEO') {
          // Video/reel của FB: ảnh bìa nằm ở `poster` (src là file .mp4 — không lấy nhầm).
          them(el.getAttribute('poster') || '');
          // src dự phòng chỉ dùng khi không phải file video trực tiếp
          if (!cac.length) {
            const s = el.getAttribute('src') || '';
            if (s && !s.includes('.mp4') && !s.includes('.m3u8')) them(s);
          }
        } else {
          them(el.getAttribute('src') || '');
          them(el.getAttribute('data-src') || '');
          const srcset = el.getAttribute('srcset') || '';
          srcset.split(',').forEach((p) => them((p.trim().split(/\\s+/)[0] || '')));
          them(el.currentSrc || '');
        }
        return cac[cac.length - 1] || '';
      };
      // Ảnh FB đôi khi là CSS background (link-preview / cover video) — không có thẻ <img>
      const anhNen = (el) => {
        const st = el.getAttribute('style') || '';
        const m = st.match(/background(?:-image)?\\s*:\\s*url\\(['"]?([^'")]+)/i);
        return (m && co_fbcdn(m[1])) ? m[1] : '';
      };
      const kichThuoc = (img) => {
        const nw = img.naturalWidth || 0;
        const bw = Math.round(img.getBoundingClientRect().width) || 0;
        const at = parseInt(img.getAttribute('width') || '0') || 0;
        return Math.max(nw, bw, at);
      };

      // --- 1. Text bài ---
      const previews = [];
      document.querySelectorAll('[data-ad-comet-preview]').forEach(el => {
        const top = rectTop(el);
        // ảnh media: trong block cha, bỏ avatar nhỏ và ảnh nằm trên text
        let n = el, imgs = [], coVideo = false;
        for (let k = 0; k < 14 && n; k++) {
          n.querySelectorAll('img, video').forEach(node => {
            if (node.tagName === 'VIDEO' && kichThuoc(node) >= 60) coVideo = true;
            const u = anhFb(node);
            if (!u) return;
            const w = kichThuoc(node);
            if (w < 60) return;                      // avatar / icon / placeholder nhỏ
            if (rectTop(node) < top - 80) return;    // ảnh nằm trên text = avatar/header
            if (imgs.indexOf(u) === -1) imgs.push(u);
          });
          n.querySelectorAll('[style*="background"]').forEach(node => {
            const u = anhNen(node);
            if (!u) return;
            const w = kichThuoc(node);
            if (w < 100) return;                     // bỏ decor/cover nhỏ
            if (rectTop(node) < top - 80) return;    // ảnh nằm trên text = avatar/header
            if (imgs.indexOf(u) === -1) imgs.push(u);
          });
          n = n.parentElement;
        }
        previews.push({top, text: (el.innerText || '').trim(), imgs, coVideo});
      });

      // --- 1b. Layout khác: [role="article"] bọc cả bài (không có preview).
      // Mỗi article = 1 bài hoàn chỉnh: text, ảnh, toolbar nằm bên trong.
      const articles = [];
      document.querySelectorAll('[role="article"]').forEach(el => {
        // Bỏ qua nếu là comment lồng bên trong bài hoặc widget comment
        if (el.parentElement && el.parentElement.closest('[role="article"]')) return;
        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
        if (aria.startsWith('comment') || aria.startsWith('bình luận') || aria.startsWith('reply') || aria.startsWith('trả lời')) return;

        const top = rectTop(el);
        let toolbarText = '';
        el.querySelectorAll(CAC_SEL_TOOLBAR).forEach(tb => {
          let n = tb;
          for (let k = 0; k < 14 && n; k++) {
            if (NODE_TOOLBAR.test(n.outerHTML || '')) break;
            n = n.parentElement;
          }
          if (n) toolbarText = (n.innerText || '').replace(/\\s+/g, ' ').trim();
        });
        // Bổ sung: Chế độ khách (Guest mode không login) hiển thị "All reactions: ..."
        if (!toolbarText) {
          el.querySelectorAll('div, span').forEach(n => {
            const t = (n.innerText || '').trim();
            if (/All reactions|Tất cả cảm xúc/i.test(t)) {
              let p = n;
              for (let k = 0; k < 6 && p; k++) {
                const pt = (p.innerText || '').trim();
                if (/\\d+/.test(pt) && pt.length < 250) {
                  toolbarText = pt.replace(/\\s+/g, ' ');
                  if (/Like|Comment|Bình luận|Share/i.test(pt)) break;
                }
                p = p.parentElement;
              }
            }
          });
        }
        // Bổ sung: Chế độ khách Facebook hiển thị nút Like trực tiếp có số kèm theo
        if (!toolbarText) {
          const likeBtns = Array.from(el.querySelectorAll('div, span')).filter(n => {
            const t = (n.innerText || '').trim();
            return (t === 'Like' || t === 'Thích') && n.children.length === 0;
          });
          for (const lb of likeBtns) {
            let p = lb.parentElement;
            for (let k = 0; k < 8 && p; k++) {
              const pt = (p.innerText || '').trim();
              if (pt && pt.length < 250 && (pt.includes('Like') || pt.includes('Thích'))) {
                if (/\\d+/.test(pt)) {
                  toolbarText = pt.replace(/\\s+/g, ' ');
                  break;
                } else if (!toolbarText) {
                  toolbarText = pt.replace(/\\s+/g, ' ');
                }
              }
              p = p.parentElement;
            }
            if (/\\d+/.test(toolbarText)) break;
          }
        }
        // bản sao đã GỠ toolbar để text bài không dính số liệu tương tác
        const clone = el.cloneNode(true);
        clone.querySelectorAll(CAC_SEL_TOOLBAR).forEach(tb => {
          let n = tb;
          for (let k = 0; k < 14 && n; k++) {
            if (NODE_TOOLBAR.test(n.outerHTML || '')) break;
            n = n.parentElement;
          }
          if (n) n.remove();
        });
        clone.querySelectorAll('div, span').forEach(n => {
          const t = (n.innerText || '').trim();
          if (/^(All reactions|Tất cả cảm xúc)/i.test(t)) {
            try { n.remove(); } catch(e) {}
          }
          if (['Like', 'Comment', 'Share', 'Thích', 'Bình luận', 'Chia sẻ'].includes(t)) {
            try { n.remove(); } catch(e) {}
          }
        });
        const imgs = [];
        let coVideo = false;
        el.querySelectorAll('img, video').forEach(node => {
          if (node.tagName === 'VIDEO' && kichThuoc(node) >= 60) coVideo = true;
          const u = anhFb(node);
          if (!u) return;
          const w = kichThuoc(node);
          if (w < 60) return;
          if (imgs.indexOf(u) === -1) imgs.push(u);
        });
        el.querySelectorAll('[style*="background"]').forEach(node => {
          const u = anhNen(node);
          if (!u) return;
          const w = kichThuoc(node);
          if (w < 100) return;                       // bỏ decor/cover nhỏ
          if (rectTop(node) < top - 80) return;      // ảnh nằm trên text = avatar/header
          if (imgs.indexOf(u) === -1) imgs.push(u);
        });
        // Ưu tiên text của container tin nhắn [data-ad-comet-preview] — bản SẠCH,
        // không dính tên trang + metadata AI ("Chiefs Dynasty FansNội dung do AI tạo · n ngày ·")
        // như clone.innerText. Vẫn cần clone để gỡ toolbar trước khi đo.
        var noi_dung = '';
        clone.querySelectorAll('[data-ad-comet-preview], [data-ad-preview="message"]').forEach(function(m) {
          var mt = (m.innerText || '').trim();
          if (mt.length > noi_dung.length) noi_dung = mt;
        });
        // Chỉ lấy text bài viết thực sự. Nếu không có noi_dung từ container bài, kiểm tra clone.innerText
        // nhưng loại trừ triệt để nếu chỉ là header/thời gian/chia sẻ của FB (bài không có caption)
        var text_bai = noi_dung;
        if (!text_bai) {
          // Cách 2: Tìm khối văn bản dir="auto" bên trong bài (chuẩn Facebook feed mới)
          var dirAutos = [];
          clone.querySelectorAll('div[dir="auto"], span[dir="auto"]').forEach(function(d) {
            if (d.closest('h2, h3, h4, header') || d.querySelector('h2, h3, h4, header')) return;
            var dt = (d.innerText || '').trim();
            if (dt.length >= 15 && !/^(Like|Comment|Share|Thích|Bình luận|Chia sẻ|All reactions)/i.test(dt)) {
              dirAutos.push(dt);
            }
          });
          if (dirAutos.length > 0) {
            dirAutos.sort(function(a, b) { return b.length - a.length; });
            text_bai = dirAutos[0];
          }
        }
        if (!text_bai) {
          var cloneText = (clone.innerText || '').trim();
          var mChiaSe = cloneText.match(/(?:·|\\b)\\s*(?:Đã chia sẻ với|Shared with|·\\s*\\d+\\s*(?:h|m|d|hrs?|mins?|days?|giờ|phút|ngày))\\s*(?:[\\s\\S]*?)(?:\\n|$)/i);
          if (mChiaSe && mChiaSe.index < 300) {
            var sauCs = cloneText.substring(mChiaSe.index + mChiaSe[0].length).replace(/^\\d+\\s*/, '').trim();
            if (sauCs.length >= 10) {
              text_bai = sauCs;
            }
          }
        }

        // Tìm link bài viết trực tiếp ngay bên trong thẻ article:
        let insideLink = '';
        let timeLabel = '';
        el.querySelectorAll('a[href]').forEach(a => {
          const href = a.href || '';
          if (!href.startsWith('https://www.facebook.com/') || href.includes('/stories/')) return;
          if (href.includes('/posts/pfbid') || href.includes('/reel/') || href.includes('/reels/') ||
              href.includes('story_fbid=') || href.includes('fbid=') || href.includes('/videos/') || href.includes('/photos/')) {
            let clean = href.split('?')[0].replace(/\\/+$/, '');
            if (href.includes('story_fbid=')) {
              const mId = href.match(/[?&]id=(\\d+)/);
              const mFbid = href.match(/[?&]story_fbid=([^&]+)/);
              if (mId && mFbid) {
                clean = 'https://www.facebook.com/permalink.php?story_fbid=' + mFbid[1] + '&id=' + mId[1];
              }
            }
            if (!insideLink) insideLink = clean || href;
            const t = (a.innerText || '').trim();
            if (t && t.length < 30 && !timeLabel) timeLabel = t;
          }
        });

        // Bóc tách Lượt xem (Views / Plays) trong thẻ article (overlay video, nhãn aria, span)
        let viewText = '';
        const REG_VIEW = /(\\d[\\d.,]*\\s*(?:[kKM]|tr|Tr)?)\\s*(?:views?|plays?|lượt\\s*xem|lượt\\s*phát|visningar|weergaven|aufrufe|vues)/i;
        const REG_VIEW_PRE = /(?:views?|plays?|lượt\\s*xem|lượt\\s*phát|visningar|weergaven|aufrufe|vues)[:\\s]*(\\d[\\d.,]*\\s*(?:[kKM]|tr|Tr)?)/i;
        const REG_PLAY_ICON = /(?:▶|►)\\s*(\\d[\\d.,]*\\s*(?:[kKM]|tr|Tr)?)/i;

        el.querySelectorAll('[aria-label]').forEach(al => {
          if (viewText) return;
          const val = (al.getAttribute('aria-label') || '').trim();
          if (REG_VIEW.test(val) || REG_VIEW_PRE.test(val)) viewText = val;
        });
        if (!viewText) {
          el.querySelectorAll('span, div, a').forEach(node => {
            if (viewText) return;
            if (node.children.length === 0) {
              const t = (node.innerText || '').trim();
              if (REG_VIEW.test(t) || REG_VIEW_PRE.test(t) || REG_PLAY_ICON.test(t)) {
                viewText = t;
              }
            }
          });
        }

        articles.push({top, text: text_bai, imgs, toolbarText, coVideo, insideLink, timeLabel, viewText});
      });

      // --- 2. Thanh tương tác (toolbar): cảm xúc / bình luận / chia sẻ ---
      const toolbars = [];
      document.querySelectorAll(CAC_SEL_TOOLBAR).forEach(tb => {
        let node = tb;
        for (let k = 0; k < 14 && node; k++) {
          if (NODE_TOOLBAR.test(node.outerHTML || '')) break;
          node = node.parentElement;
        }
        if (!node) return;
        toolbars.push({top: rectTop(node),
                       text: (node.innerText || '').replace(/\\s+/g, ' ').trim()});
      });

      // --- 3. Link bài + thời gian (riêng, ghép theo vị trí sau) ---
      const links = [];
      document.querySelectorAll('a[href*="/posts/"], a[href*="/reel"], a[href*="/reels/"], '
        + 'a[href*="/videos/"], a[href*="/photos/"], a[href*="photo.php"], '
        + 'a[href*="story_fbid"]').forEach(a => {
        let h = (a.href || '').split('?')[0].replace(/\\/+$/, '');
        if (!h.startsWith('https://www.facebook.com/')) return;
        if (h.includes('/stories/')) return;
        const sau = h.split('/').pop() || '';
        const rawHref = a.href || '';
        const co_id = h.includes('/posts/pfbid')
          || ((h.includes('/reel/') || h.includes('/reels/')
               || h.includes('/videos/') || h.includes('/photos/')) && /\\d+/.test(sau))
          || h.includes('/photo.php?fbid=')
          || rawHref.includes('story_fbid=')
          || rawHref.includes('fbid=');
        if (!co_id) return;
        let clean = h;
        if (rawHref.includes('story_fbid=')) {
          const mId = rawHref.match(/[?&]id=(\\d+)/);
          const mFbid = rawHref.match(/[?&]story_fbid=([^&]+)/);
          if (mId && mFbid) {
            clean = 'https://www.facebook.com/permalink.php?story_fbid=' + mFbid[1] + '&id=' + mId[1];
          }
        }
        links.push({top: rectTop(a), href: clean,
                    timeText: (a.innerText || '').trim()});
      });

      const times = [];
      document.querySelectorAll('[aria-label*="Thứ "], [aria-label*="Chủ "], '
        + '[aria-label*="Hôm nay"], [aria-label*="Hôm qua"], '
        + '[aria-label*="lúc "], [aria-label*="giờ"], [aria-label*="ngày"]').forEach(el => {
        const lb = (el.getAttribute('aria-label') || '').trim();
        if (!lb) return;
        // thẻ thời gian thường là CHÍNH anchor trỏ tới bài viết (permalink)
        times.push({top: rectTop(el), label: lb,
                    href: el.tagName === 'A' ? (el.href || '') : ''});
      });

      // --- 4. Bóc tách Reel Grid (Thước phim dạng lưới / tab Reels a[href*="/reel/"]) ---
      const reels_grid = [];
      const seenReels = new Set();
      document.querySelectorAll('a[href*="/reel/"]').forEach(a => {
        let h = (a.href || '').split('?')[0].replace(/\\/+$/, '');
        if (!h || !h.includes('/reel/') || seenReels.has(h)) return;
        seenReels.add(h);

        let imgUrl = '';
        const imgNode = a.querySelector('img, video');
        if (imgNode) {
          imgUrl = anhFb(imgNode) || imgNode.getAttribute('src') || '';
        }
        
        const rawText = (a.innerText || '').trim();
        const ariaText = (a.getAttribute('aria-label') || '').trim();
        const combined = (rawText + '\\n' + ariaText).split(/\\n+/).map(l => l.trim()).filter(Boolean);

        reels_grid.push({
          top: rectTop(a),
          href: h,
          text: rawText,
          aria: ariaText,
          lines: combined,
          img: imgUrl
        });
      });

      return {previews, toolbars, links, times, articles, reels_grid};
    }
    """
    du_lieu = page.evaluate(js)
    previews, toolbars = du_lieu["previews"], du_lieu["toolbars"]
    links, times = du_lieu["links"], du_lieu["times"]
    articles = du_lieu["articles"]
    reels_grid = du_lieu.get("reels_grid") or []

    # --- 4. Ghép: mỗi toolbar = 1 bài; text/link/time gần nhất phía trên ---
    def tim_gan_nhat(danh_sach, top, toi_da=NGUONG_GHEP, da_dung=None,
                     cho_phep_duoi=0):
        """Phần tử gần top nhất (trên: 0..toi_da; có thể dưới: -cho_phep_duoi..0),
        chưa bị dùng (da_dung = set index)."""
        ket_qua, khoang = None, None
        for i, phan_tu in enumerate(danh_sach):
            if da_dung is not None and i in da_dung:
                continue
            d = top - phan_tu["top"]
            if -cho_phep_duoi <= d <= toi_da:
                dd = abs(d)
                if khoang is None or dd < khoang:
                    khoang, ket_qua = dd, i
        return (danh_sach[ket_qua], ket_qua) if ket_qua is not None else (None, None)

    cac_bai = []
    dung_preview = set()
    dung_link = set()
    dung_time = set()

    if not toolbars and articles:
        # Layout B: không có thanh tương tác riêng — mỗi [role="article"] = 1 bài
        for art in articles:
            # Bỏ qua article rỗng (sidebar, widget không phải bài viết)
            if not art.get("text", "").strip() and not art.get("imgs"):
                continue
            _url = art.get("insideLink") or ""
            link_tt = {"href": _url} if _url else None
            if not _url:
                link_tt, chi_so_l = tim_gan_nhat(links, art["top"], toi_da=700,
                                                 da_dung=dung_link, cho_phep_duoi=2500)
                if link_tt is not None:
                    dung_link.add(chi_so_l)
                    _url = link_tt.get("href") or ""
            time_tt, chi_so_t = tim_gan_nhat(times, art["top"], toi_da=700,
                                             da_dung=dung_time, cho_phep_duoi=2500)
            if time_tt is not None:
                dung_time.add(chi_so_t)
            if not _url and time_tt and time_tt.get("href"):
                _url = time_tt["href"]
                link_tt = {"href": _url}
            likes, comments, shares, views_tb = _trich_so_lieu(art.get("toolbarText") or "")
            views_art = 0
            vt = art.get("viewText") or ""
            if vt:
                tu_khoa_views = r"(?:views?|plays?|lượt\s*xem|lượt\s*phát|visningar|weergaven|aufrufe|vues)"
                m_v = re.search(rf"(\d[\d.,]*\s*(?:[kKM]|tr|Tr)?)\s*{tu_khoa_views}", vt, re.IGNORECASE)
                if not m_v:
                    m_v = re.search(rf"{tu_khoa_views}[:\s]*(\d[\d.,]*\s*(?:[kKM]|tr|Tr)?)", vt, re.IGNORECASE)
                if not m_v:
                    m_v = re.search(r"(?:▶|►)\s*(\d[\d.,]*\s*(?:[kKM]|tr|Tr)?)", vt, re.IGNORECASE)
                if m_v:
                    views_art = _parse_so(m_v.group(1))
            views = max(views_tb, views_art)
            post_id = _post_id_tu_link(_url) or (
                "bai_" + hashlib.md5(
                    (art["text"][:80] or str(art["top"])).encode()).hexdigest()[:12])
            lbl_time = (time_tt["label"] if time_tt else "") or (art.get("timeLabel") or "")
            time_obj = _parse_thoi_gian(lbl_time)
            cac_bai.append({
                "post_id": post_id,
                "time": time_obj.isoformat() if time_obj else lbl_time,
                "text": lam_sach_text_bai(art["text"]),
                "images": art["imgs"],
                "post_url": _url,
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "is_reel": _la_reel(_url, art.get("coVideo"), art["imgs"]),
            })
    else:
        for tb in toolbars:
            bai_text, chi_so = tim_gan_nhat(previews, tb["top"])
            if bai_text is None:
                continue  # bài chưa kịp render text
            dung_preview.add(chi_so)

            link_tt, chi_so_l = tim_gan_nhat(links, bai_text["top"], toi_da=700,
                                             da_dung=dung_link, cho_phep_duoi=2500)
            if link_tt is None:
                link_tt, chi_so_l = tim_gan_nhat(links, tb["top"], toi_da=1200,
                                                 da_dung=dung_link, cho_phep_duoi=300)
            if link_tt is not None:
                dung_link.add(chi_so_l)
            time_tt, chi_so_t = tim_gan_nhat(times, bai_text["top"], toi_da=700,
                                             da_dung=dung_time, cho_phep_duoi=2500)
            if time_tt is None:
                time_tt, chi_so_t = tim_gan_nhat(times, tb["top"], toi_da=1200,
                                                 da_dung=dung_time, cho_phep_duoi=300)
            if time_tt is not None:
                dung_time.add(chi_so_t)

            if link_tt is None and time_tt and time_tt.get("href"):
                link_tt = {"href": time_tt["href"]}
            likes, comments, shares, views_tb = _trich_so_lieu(tb["text"])
            
            # Bổ sung bóc tách Views từ article hoặc preview tương ứng trong Layout A
            views_art = 0
            art_tt, _ = tim_gan_nhat(articles, tb["top"], toi_da=1200, cho_phep_duoi=1200)
            if art_tt and art_tt.get("viewText"):
                vt = art_tt["viewText"]
                tu_khoa_views = r"(?:views?|plays?|lượt\s*xem|lượt\s*phát|visningar|weergaven|aufrufe|vues)"
                m_v = re.search(rf"(\d[\d.,]*\s*(?:[kKM]|tr|Tr)?)\s*{tu_khoa_views}", vt, re.IGNORECASE)
                if not m_v:
                    m_v = re.search(rf"{tu_khoa_views}[:\s]*(\d[\d.,]*\s*(?:[kKM]|tr|Tr)?)", vt, re.IGNORECASE)
                if not m_v:
                    m_v = re.search(r"(?:▶|►)\s*(\d[\d.,]*\s*(?:[kKM]|tr|Tr)?)", vt, re.IGNORECASE)
                if m_v:
                    views_art = _parse_so(m_v.group(1))
            views = max(views_tb, views_art)

            post_id = _post_id_tu_link(link_tt["href"] if link_tt else "")
            if not post_id:
                post_id = "bai_" + hashlib.md5(
                    (bai_text["text"][:80] or str(bai_text["top"])).encode()).hexdigest()[:12]
            time_obj = _parse_thoi_gian(time_tt["label"] if time_tt else None)
            _url = link_tt["href"] if link_tt else ""
            cac_bai.append({
                "post_id": post_id,
                "time": time_obj.isoformat() if time_obj else
                        (time_tt["label"] if time_tt else ""),
                "text": lam_sach_text_bai(bai_text["text"]),
                "images": bai_text["imgs"],
                "post_url": _url,
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "is_reel": _la_reel(_url, bai_text.get("coVideo"), bai_text["imgs"]),
            })

    # Bổ sung các bài từ Reel Grid (Thước phim dạng lưới / tab Reels) nếu chưa có trong cac_bai
    seen_urls = {b.get("post_url") for b in cac_bai if b.get("post_url")}
    seen_pids = {b.get("post_id") for b in cac_bai if b.get("post_id")}
    tu_khoa_views = r"(?:views?|plays?|lượt\s*xem|lượt\s*phát|visningar|weergaven|aufrufe|vues)"
    for rg in reels_grid:
        r_url = rg.get("href") or ""
        r_pid = _post_id_tu_link(r_url) or ("reel_" + hashlib.md5(r_url.encode()).hexdigest()[:10])
        if r_url in seen_urls or r_pid in seen_pids:
            continue
        seen_urls.add(r_url)
        seen_pids.add(r_pid)

        # Bóc tách Lượt xem từ lines của Reel
        views_reel = 0
        for l in rg.get("lines", []):
            l_clean = l.strip()
            # Bỏ qua nhãn giao diện và thời gian (19m, 14m, 2h, 3d, 21 phút)
            if not l_clean or any(k in l_clean.lower() for k in ["bản xem trước", "thước phim", "reels", "reel", "phút", "giờ", "ngày", "min", "sec", "hour", "day"]):
                continue
            if re.match(r"^\d+\s*[smhdw]$", l_clean):
                continue
            # Dạng số lượt xem Reel của Facebook: 15K, 47K, 1,2K hoặc 1.5M (M hoa)
            m_k = re.match(r"^(\d[\d.,]*\s*[kKM])$", l_clean)
            if m_k:
                views_reel = _parse_so(m_k.group(1))
                break
            # Dạng số nguyên thuần túy (ví dụ 1,200 hoặc 500)
            m_num = re.match(r"^(\d{1,3}(?:[.,]\d{3})+|\d{2,})$", l_clean)
            if m_num:
                views_reel = _parse_so(m_num.group(1))
                break
            # Dạng kèm chữ views/plays: 15K views, 1.5M plays, 15000 lượt xem
            m_v = re.search(rf"(\d[\d.,]*\s*(?:[kKM]|tr|Tr)?)\s*{tu_khoa_views}", l_clean, re.IGNORECASE)
            if not m_v:
                m_v = re.search(rf"{tu_khoa_views}[:\s]*(\d[\d.,]*\s*(?:[kKM]|tr|Tr)?)", l_clean, re.IGNORECASE)
            if m_v:
                views_reel = _parse_so(m_v.group(1))
                break

        caption_reel = rg.get("aria") or rg.get("text") or ""
        # Bỏ caption nếu chỉ là số view hoặc nhãn giao diện
        if caption_reel.strip() in [l.strip() for l in rg.get("lines", []) if re.match(r"^\d[\d.,]*\s*(?:[kKmM]|tr|Tr)?$", l.strip())]:
            caption_reel = ""

        cac_bai.append({
            "post_id": r_pid,
            "time": "",
            "text": lam_sach_text_bai(caption_reel),
            "images": [rg["img"]] if rg.get("img") else [],
            "post_url": r_url,
            "views": views_reel,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "is_reel": True,
        })

    return cac_bai


def cai_dat_bypass_popup(page, ctx=None):
    """Cài đặt bypass toàn diện popup đăng nhập Facebook (Tầng 2 DOM/CSS & Tầng 3 Network Route).

    1. Inject CSS cưỡng chế: ẩn vĩnh viễn dialog, aria-modal, backdrop; ép mở overflow-y: scroll.
    2. MutationObserver: diệt mọi dialog popup ngay khi vừa chèn vào DOM.
    3. Chặn history.pushState / replaceState dẫn tới /login.
    4. Network route abort: chặn đứng mọi request chuyển hướng sang /login.
    """
    init_js = """
    // 1. Inject CSS cưỡng chế mở cuộn và ẩn dialog/backdrop
    const injectBypassStyle = () => {
        if (document.getElementById('fb-bypass-popup-css')) return;
        const style = document.createElement('style');
        style.id = 'fb-bypass-popup-css';
        style.textContent = `
            div[role="dialog"],
            div[aria-modal="true"],
            div[class*="backdrop"],
            div[data-nosnippet] {
                display: none !important;
                pointer-events: none !important;
                visibility: hidden !important;
            }
            html, body {
                overflow: auto !important;
                overflow-y: scroll !important;
                position: static !important;
                height: auto !important;
                max-height: none !important;
            }
        `;
        if (document.head || document.documentElement) {
            (document.head || document.documentElement).appendChild(style);
        }
    };
    injectBypassStyle();
    document.addEventListener('DOMContentLoaded', injectBypassStyle);

    // 2. MutationObserver diệt dialog ngay khi vừa chèn vào DOM
    try {
        const obs = new MutationObserver(() => {
            const closeBtn = document.querySelector('[aria-label="Đóng"], [aria-label="Close"]');
            if (closeBtn) { try { closeBtn.click(); } catch(e) {} }

            const dialogs = document.querySelectorAll('div[role="dialog"], div[aria-modal="true"]');
            dialogs.forEach(el => { try { el.remove(); } catch(e) {} });

            if (document.body && document.body.style.overflow === 'hidden') {
                document.body.style.overflow = 'auto';
            }
            if (document.documentElement && document.documentElement.style.overflow === 'hidden') {
                document.documentElement.style.overflow = 'auto';
            }
        });
        obs.observe(document.documentElement, { childList: true, subtree: true });
    } catch(e) {}

    // 3. Khóa chuyển hướng sang /login qua pushState/replaceState
    try {
        const origPush = history.pushState;
        history.pushState = function(state, title, url) {
            if (url && (String(url).includes('/login') || String(url).includes('login_form') || String(url).includes('login.php'))) {
                return;
            }
            return origPush.apply(this, arguments);
        };
        const origReplace = history.replaceState;
        history.replaceState = function(state, title, url) {
            if (url && (String(url).includes('/login') || String(url).includes('login_form') || String(url).includes('login.php'))) {
                return;
            }
            return origReplace.apply(this, arguments);
        };
    } catch(e) {}
    """
    if ctx:
        try:
            ctx.add_init_script(init_js)
        except Exception:
            pass

    try:
        page.add_init_script(init_js)
    except Exception:
        pass

    # Chặn network request chuyển hướng sang login (Tầng 3)
    def _chan_login(route):
        u = route.request.url.lower()
        if "/login" in u or "login_form" in u or "login.php" in u:
            route.abort()
        else:
            route.continue_()

    try:
        page.route("**/login/**", _chan_login)
        page.route("**/login.php*", _chan_login)
        page.route("**/*login_form*", _chan_login)
    except Exception:
        pass


def _dong_popup_va_mo_khoa_cuon(page, log=None):
    """Đóng modal/dialog đăng nhập và mở khóa cuộn trang cho chế độ khách (Guest mode)."""
    try:
        page.evaluate("""() => {
            const dialogs = document.querySelectorAll('div[role="dialog"], div[aria-modal="true"]');
            dialogs.forEach(d => {
                const closeBtn = d.querySelector('[aria-label="Close"], [aria-label="Đóng"], div[role="button"]');
                if (closeBtn) { try { closeBtn.click(); } catch(e) {} }
                try { d.remove(); } catch(e) {}
            });
            document.documentElement.style.overflow = 'auto';
            document.body.style.overflow = 'auto';
        }""")
        for sel in [
            '[aria-label="Đóng"]',
            '[aria-label="Close"]',
            'div[role="dialog"] [aria-label="Đóng"]',
            'div[role="dialog"] [aria-label="Close"]',
            'div[aria-label="Close"]',
            'div[aria-label="Đóng"]',
            'div[role="dialog"] [role="button"]'
        ]:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=300):
                btn.click()
                if log: log("    [i] Đã đóng popup đăng nhập.")
                page.wait_for_timeout(500)
                break
    except Exception:
        pass


def cào_trang(page_name: str, so_bai: int, so_lan_cuon: int, delay: float,
              cookies: list = None, stop_flag=None, callback=None,
              kiem_tra_pool=None, proxy: str = None, browser=None) -> list:
    """Cào một trang bằng trình duyệt thật, trả về danh sách bài.

    `kiem_tra_pool`: đối tượng KiemTraPool (content_pool.py) — khi được cung cấp,
    bài đã có trong kho (trùng post_id hoặc caption) sẽ bị BỎ QUA và KHÔNG tính
    vào hạn mức `so_bai`: tool cuộn tiếp cho tới khi đủ `so_bai` bài MỚI.

    `proxy`: 'http://user:pass@host:port' — mọi traffic của PHIÊN NÀY (cả trình
    duyệt lẫn DOM) đi qua IP đó. Ghép sticky 1:1 với cookie để Facebook thấy
    "một tài khoản = một IP cố định", không phải một IP đổi tài khoản liên tục.
    """
    from playwright.sync_api import sync_playwright
    global PHIEN_BI_TU_CHOI

    def log(msg):
        print(msg)
        if callback:
            callback({"loai": "log", "noi_dung": msg})

    px = chuan_hoa_proxy(proxy) if proxy else ""
    log(f"    [i] Mở trình duyệt để cào '{page_name}'"
        + (f" qua proxy {_ru_mat_khau(px)} ..." if px else " ..."))

    cac_bai = []

    def _thuc_hien_cao(b):
        global PHIEN_BI_TU_CHOI
        nonlocal cac_bai
        ctx = b.new_context(
            user_agent=DEFAULT_UA, locale="vi-VN",
            # Có proxy VN thì khai luôn timezone VN cho khớp IP (lệch múi là
            # một trong những dấu hiệu bot dễ thấy nhất).
            timezone_id="Asia/Ho_Chi_Minh" if px else None,
            viewport={"width": 1280, "height": 900},
        )
        if cookies:
            ctx.add_cookies(cookies)
        # Chặn video/font tự tải (DOM vẫn đủ dữ liệu, ảnh vẫn tải để lấy URL)
        chan_tai_nguyen_nang(ctx, chan_anh=False)
        page = ctx.new_page()
        try:
            # Kích hoạt bypass popup đa tầng (Tầng 2 DOM/CSS & Tầng 3 Anti-Redirect)
            cai_dat_bypass_popup(page, ctx)
            # Chuẩn hóa URL trang: chấp nhận cả full URL (https://www.facebook.com/...), profile.php?id=..., hoặc username
            page_clean = str(page_name).strip()
            if page_clean.startswith("http://") or page_clean.startswith("https://"):
                url_trang = page_clean
            elif page_clean.startswith("profile.php"):
                url_trang = f"https://www.facebook.com/{page_clean}"
            else:
                url_trang = f"https://www.facebook.com/{page_clean.strip('/')}/"
            page.goto(url_trang, timeout=60000, wait_until="domcontentloaded")

            # chờ 5s đầu theo từng phần nhỏ để bấm Dừng được dừng ngay
            for _ in range(10):
                if stop_flag and stop_flag.is_set():
                    log(f"    [i] Đã dừng theo yêu cầu tại '{page_name}'")
                    return []
                page.wait_for_timeout(500)

            # --- PHÁT HIỆN SỚM PHIÊN BỊ TỪ CHỐI (chỉ khi nạp cookie) ---
            if cookies and _session_bi_tu_choi(ctx, cookies):
                PHIEN_BI_TU_CHOI = True
                log("    [✗] FACEBOOK TỪ CHỐI PHIÊN CÀO NÀY — vừa mở trang là "
                    "cookie đăng nhập (c_user) bị xóa sạch.\n"
                    "        Mọi trang từ đây chỉ xem được BẢN KHÁCH (1-3 bài, "
                    "cuộn không bao giờ ăn) nên KHÔNG cào tiếp trang khác nữa.\n"
                    "        Cách xử lý: mở Facebook bằng trình duyệt thường → "
                    "đăng nhập lại → xuất lại cookies.txt (F12 > Application > "
                    "Cookies, hoặc extension Get cookies.txt) → chạy lại.")
                return []

            # --- ĐÓNG POPUP ĐĂNG NHẬP & MỞ KHÓA CUỘN (cho chế độ khách & có cookie) ---
            _dong_popup_va_mo_khoa_cuon(page, log)

            # Chờ tối đa 10s cho bài xuất hiện (không bỏ cuộc ngay — nhiều trang
            # tải bài chậm, nhất là profile/trang nhỏ; vòng cuộn phía dưới sẽ tự
            # xử lý tiếp nếu lúc này chưa có gì).
            for _ in range(10):
                if stop_flag and stop_flag.is_set():
                    log(f"    [i] Đã dừng theo yêu cầu tại '{page_name}'")
                    return []
                try:
                    # layout A: [data-ad-comet-preview] — layout B: [role="article"]
                    page.wait_for_selector('[data-ad-comet-preview], [role="article"], '
                                           '[aria-label*="bình luận"], '
                                           '[aria-label*="comment"]', timeout=1000)
                    break
                except Exception:
                    continue

            # Facebook "virtualize" feed: bài đã cuộn qua sẽ bị gỡ khỏi DOM.
            # Vì vậy phải đọc bài SAU MỖI LẦN CUỘN rồi gộp dần (loại trùng theo post_id).
            cac_bai = []
            da_thay = set()
            so_dem_kho = 0
            # Mỗi lần cuộn chỉ tải ~3-5 bài, và khi gặp bài cũ trong kho thì
            # "số bài mới" không tăng dù feed vẫn còn dài. Vì vậy KHÔNG được
            # dừng chỉ vì vài lượt cuộn không tăng -> chỉ dừng khi:
            #   (1) đủ so_bai bài mới, hoặc
            #   (2) chạm ĐÁY trang (dòng "Đó là tất cả..." hoặc feed đứng hình),
            #   (3) hết lượt an toàn `toi_da_lan`.
            # Ô "số lần cuộn" trên giao diện là SỐ LẦN CUỘN TỐI THIỂU (mỗi đợt
            # kiểm tra) chứ không phải trần tuyệt đối: chưa đủ bài mới mà trang
            # vẫn còn tải => tool tự cuộn tiếp. Đặt 0 = tool tự tính.
            dot_cuon = max(1, int(so_lan_cuon or 0) or 3)
            he_so_cuon = 8 if kiem_tra_pool is not None else 4
            toi_da_lan = min(300, max(dot_cuon,
                                      ((so_bai or 20) * he_so_cuon + 60)))
            lan = 0
            da_dich = False     # đã chạm đáy trang
            dem_dung_hinh = 0   # số lần cuộn liên tiếp mà DOM không tăng
            while lan < toi_da_lan:
                lan += 1
                if stop_flag and stop_flag.is_set():
                    log(f"    [i] Đã dừng theo yêu cầu tại '{page_name}'")
                    break

                # Đóng popup và mở khóa cuộn nếu xuất hiện trong lúc cuộn
                _dong_popup_va_mo_khoa_cuon(page)

                # mở rộng các bài dài ("Xem thêm") để lấy đủ text trước khi đọc
                try:
                    page.evaluate("""() => {
                      const NS = ['Xem thêm', 'See more', 'View more', 'Thêm'];
                      const els = Array.from(document.querySelectorAll('div[role="button"], span[role="button"], div, span'));
                      for (const el of els) {
                        const t = (el.innerText || '').trim();
                        if (NS.includes(t) && el.children.length === 0) {
                          try { el.click(); } catch(e) {}
                        }
                      }
                    }""")
                    page.wait_for_timeout(600)
                except Exception:
                    pass

                # DỌN BỘ NHỚ định kỳ: vòng cào chạy hàng trăm lượt, mỗi lượt
                # tạo hàng nghìn object; nếu không gc, tiến trình phình lên rồi
                # Windows "bóp" RAM -> page.evaluate trả None giữa chừng
                # ("NoneType ... is not iterable") và trang bị bỏ trống 0 bài.
                if lan % 4 == 0:
                    _gc.collect()

                # GỌN DOM: gỡ các bài cũ + comment ra khỏi trang để memory
                # trình duyệt không phồng tới mức tự sập khi cuộn sâu.
                # Chỉ gỡ node con đã bị FB "virtualize" (không hiển thị),
                # nên không mất bài nào đang đọc được.
                try:
                    page.evaluate("""() => {
                      let dem = 0;
                      document.querySelectorAll('[role="article"]').forEach(a => {
                        try {
                          const r = a.getBoundingClientRect();
                          if (r.height === 0 && !a.textContent.trim()) { a.remove(); dem++; }
                        } catch (e) {}
                      });
                      document.querySelectorAll('[role="article"] [aria-label*="bình luận"],' +
                                              ' [role="article"] [aria-label*="comment"]')
                        .forEach(c => { try { if (c.children.length) { c.remove(); dem++; } } catch (e) {} });
                      return dem;
                    }""")
                except Exception:
                    pass

                bai_moi = lay_du_lieu_dom(page, so_bai)
                them = 0
                so_lap_cuon = 0
                so_trung_kho = 0
                so_reel = 0
                for b in bai_moi:
                    # BỎ QUA TRIỆT ĐỂ BÀI KHÔNG CÓ CAPTION HOẶC CHỈ CÓ RÁC HEADER / ĐỔI AVATAR
                    if not la_caption_hop_le(b.get("text") or ""):
                        continue
                    # Reel: GIỮ LẠI nếu cào được ảnh bìa (poster) → làm 'reel tĩnh'
                    # (sửa ảnh rồi dựng lại video+nhạc). Bỏ reel không có ảnh nào.
                    if b.get("is_reel"):
                        if not b.get("images"):
                            # Chưa tải kịp ảnh bìa (FB lazy-load): KHÔNG đánh dấu
                            # đã thấy — lượt cuộn sau poster xuất hiện sẽ được lấy.
                            so_reel += 1
                            continue
                        b["loai_bai"] = "reel"
                    if b["post_id"] in da_thay:
                        # FB lazy-load: lần đọc trước có thể chưa kịp tải ảnh, giờ bài
                        # render lại kèm ảnh -> bổ sung vào bản đang giữ.
                        so_lap_cuon += 1
                        for cu in cac_bai:
                            if (cu.get("post_id") == b["post_id"]
                                    and not cu.get("images") and b.get("images")):
                                cu["images"] = b["images"]
                                them += 1
                                break
                        continue
                    # bài đôi khi được render 2 lần với post_id khác nhau
                    # (bản đầy đủ hơn sau khi bấm "Xem thêm") -> so text
                    la_trung = False
                    binh_thuong = re.sub(r"\s+", " ", b["text"] or "")
                    binh_thuong = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]",
                                         "", binh_thuong)
                    # Bỏ prefix page name + metadata để so sánh chính xác hơn
                    # (VD: "Chiefs Dynasty FansNội dung do AI tạo  · 3 giờ  · ...")
                    binh_thuong_core = re.sub(
                        r"^.{0,80}·\s*Đã chia sẻ với.*?(?:khai|hạn chế)\s*",
                        "", binh_thuong, count=1)
                    if not binh_thuong_core:
                        binh_thuong_core = binh_thuong
                    for cu in cac_bai:
                        binh_cu = re.sub(r"\s+", " ", cu["text"] or "")
                        binh_cu = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]",
                                         "", binh_cu)
                        binh_cu_core = re.sub(
                            r"^.{0,80}·\s*Đã chia sẻ với.*?(?:khai|hạn chế)\s*",
                            "", binh_cu, count=1)
                        if not binh_cu_core:
                            binh_cu_core = binh_cu
                        # Kiểm tra trùng bằng nội dung gần như giống hệt
                        # (tránh gộp nhầm 2 bài khác nhau có chung mở đầu/template)
                        if binh_thuong_core and binh_cu_core and _cung_mot_bai(
                                binh_thuong_core, binh_cu_core):
                            # giữ bản có link; nếu bản mới có link hơn thì thay;
                            # nếu bản cũ chưa có ảnh mà bản mới có -> bổ sung ảnh
                            if not cu["post_url"] and b["post_url"]:
                                cac_bai[cac_bai.index(cu)] = b
                                da_thay.add(b["post_id"])
                                them += 1
                            elif not cu.get("images") and b.get("images"):
                                cu["images"] = b["images"]
                                them += 1
                            la_trung = True
                            break
                    if la_trung:
                        so_lap_cuon += 1
                        continue
                    # Bài đã có trong KHO từ các phiên cào trước -> bỏ qua,
                    # KHÔNG tính vào hạn mức so_bai (yêu cầu: đủ 10 bài MỚI).
                    if kiem_tra_pool is not None and kiem_tra_pool.la_trung(
                            b["post_id"], b["text"] or ""):
                        so_trung_kho += 1
                        continue
                    da_thay.add(b["post_id"])
                    cac_bai.append(b)
                    them += 1

                phan_trung = []
                if so_lap_cuon:
                    phan_trung.append(f"{so_lap_cuon} lặp khi cuộn")
                if so_trung_kho:
                    phan_trung.append(f"{so_trung_kho} trùng kho cũ")
                str_trung = (", " + ", ".join(phan_trung)) if phan_trung else ""

                log(f"    [i] Lần {lan}/{toi_da_lan}: đọc {len(bai_moi)} bài, "
                    f"+{them} mới{str_trung}"
                    + (f", {so_reel} reel bỏ qua" if so_reel else "")
                    + f" (tổng {len(cac_bai)})")

                if so_bai and len(cac_bai) >= so_bai:
                    break
                if them == 0:
                    # Chưa thấy ĐÍCH trang -> lượt cuộn này có thể chỉ là FB tải
                    # chậm hoặc DOM chưa render xong. Không được bỏ cuộc: cuộn
                    # tiếp, chỉ đếm để báo cáo.
                    so_dem_kho += 1
                    if so_dem_kho == 1:
                        log("    [i] Chưa có bài MỚI lượt này (toàn bài cũ trong "
                            "kho) → vẫn cuộn tiếp tìm bài mới...")
                else:
                    so_dem_kho = 0

                # "Đáy thực dụng": cuộn sâu mà toàn gặp bài cũ trong kho ->
                # coi như trang đã cạn bài MỚI, dừng để dành thời gian cho
                # trang khác (trang nhỏ/cũ rất hay rơi vào trường hợp này).
                if so_dem_kho >= 25:
                    log(f"    [i] {so_dem_kho} lượt cuộn liên tiếp chỉ gặp bài "
                        f"cũ đã có trong kho → coi như trang '{page_name}' hết "
                        f"bài mới, dừng ở {len(cac_bai)} bài mới.")
                    break

                # Đếm bài bằng CẢ HAI cách: layout A/B có toolbar "[aria-label
                # =" (kể cả bản tiếng Anh "Write a comment..." — bản cũ chỉ đoán
                # tiếng Việt nên trang tiếng Anh đếm ra 0 => tool nghĩ "hết bài"
                # sau 1 bài). Layout C (trang bị giới hạn, không cho xem): đếm
                # [role="article"] + chiều cao DOM.
                sel_dem = ('[aria-label*="bình luận"], [aria-label*="comment"], '
                           '[aria-label*="Comment"], [role="article"]')
                so_truoc = page.locator(sel_dem).count()
                do_cao_truoc = _do_cao_dom(page)
                # Cuộn: wheel ngắn + End + scrollBy cả window lẫn div cuộn
                # của FB (mỗi layout nghe một kiểu khác nhau).
                _cuon_feed(page)
                # Chờ: (a) bài MỚI xuất hiện (số element TĂNG / DOM cao lên), hoặc
                #      (b) dòng "Đó là tất cả ..." (hết trang).
                # Chờ tối đa delay giây (tối thiểu 6s) + thêm 2 vòng kiểm tra.
                cho = 0
                toi_da_cho = max(delay, 8.0) * 1000
                while cho < toi_da_cho:
                    if stop_flag and stop_flag.is_set():
                        break
                    if _den_dich(page):
                        da_dich = True
                        break
                    if (page.locator(sel_dem).count() > so_truoc
                            or _do_cao_dom(page) > do_cao_truoc):
                        break
                    # Chưa thấy gì mới sau ~5s -> cuộn THÊM lần nữa trong cùng
                    # một lượt (nhiều trang chỉ nạp khi có cú scroll thứ hai).
                    if cho and cho % 5000 == 0:
                        _cuon_feed(page)
                    page.wait_for_timeout(500)
                    cho += 500
                if da_dich:
                    log("    [i] Facebook báo đã hết bài → dừng cuộn.")
                    break
                # Feed ĐỨNG HẲN: không có bài mới + cuộn không ăn (scroll
                # không tăng => đã tới đáy). Cho phép tối đa 4 lần đứng hình
                # liên tiếp để vẫn chịu được lúc mạng chập chờn.
                so_sau = page.locator(sel_dem).count()
                do_cao_sau = _do_cao_dom(page)
                if so_sau > so_truoc or do_cao_sau > do_cao_truoc:
                    dem_dung_hinh = 0
                else:
                    dem_dung_hinh += 1
                    if dem_dung_hinh >= 4:
                        log(f"    [i] Feed đứng hình {dem_dung_hinh} lần liên "
                            f"tiếp (không tải thêm bài) → đã tới đáy "
                            f"trang '{page_name}'.")
                        da_dich = True
                        break
                # Trang hiện màn ĐĂNG NHẬP BẮT BUỘC (khi dùng cookies mà bị hết hạn) ->
                # dừng ngay, báo rõ nguyên nhân thay vì cuộn vô ích.
                if cookies and _dang_nhap_ep_buoc(page):
                    PHIEN_BI_TU_CHOI = True
                    log("    [✗] Facebook đòi ĐĂNG NHẬP — cookies đã hết hạn. "
                        "Cập nhật lại cookies.txt rồi chạy tiếp.")
                    break

            if not cac_bai:
                # Thực sự không lấy được bài nào — phân tích lý do để báo rõ
                body = ""
                try:
                    body = page.evaluate("() => (document.body.innerText || '')")
                except Exception:
                    pass
                if "không xem được nội dung" in body:
                    log("    [!] Trang này KHÔNG cho tài khoản cào xem nội dung "
                        "(trang giới hạn khu vực/quyền riêng tư, hoặc bị chặn).")
                elif "Không có bài viết" in body:
                    log("    [!] Trang hiển thị 'Không có bài viết' — tài khoản cào "
                        "không xem được feed của trang này (giới hạn khu vực/quyền riêng tư).")
                elif PHIEN_BI_TU_CHOI:
                    # Đã phát hiện sớm ở đầu trang: cookie bị xóa / đòi đăng nhập.
                    log("    [!] Không thấy bài viết nào — PHIÊN CÀO ĐÃ BỊ TỪ CHỐI "
                        "(cookie đăng nhập bị xóa, chỉ xem được bản khách 1-3 bài). "
                        "Đăng nhập lại Facebook trên trình duyệt thường rồi xuất "
                        "lại cookies.txt — KHÔNG phải trang này lỗi.")
                elif "Đăng nhập" in body or "đăng nhập" in body:
                    log("    [!] Không thấy bài viết nào — cookies có thể đã hết hạn.")
                else:
                    log("    [!] Không thấy bài viết nào — feed chưa tải xong "
                        "(mạng chậm) hoặc cookies hết hạn.")
        finally:
            don_dep_trang(page, ctx)
        return cac_bai

    if browser is not None:
        cac_bai = _thuc_hien_cao(browser) or []
    else:
        with sync_playwright() as p:
            b_local = mo_trinh_duyet(p, cookies, px)
            try:
                cac_bai = _thuc_hien_cao(b_local) or []
            finally:
                try:
                    b_local.close()
                except Exception:
                    pass


    log(f"    [✓] Đã đọc {len(cac_bai)} bài từ trang '{page_name}'")
    return cac_bai[:so_bai] if so_bai else cac_bai
