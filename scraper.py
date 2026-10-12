#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cào dữ liệu Facebook Page (text + ảnh) bằng thư viện facebook-scraper.

Cách dùng (CLI):
    python scraper.py --page ten-trang --pages 5
    python scraper.py --page trangA trangB trangC --per-page 50
    python scraper.py --page ten-trang --cookies cookies.txt

Hoặc qua giao diện web:
    python webui.py   (mở http://127.0.0.1:5000)

Kết quả:
    - <output>.xlsx              : 1 file Excel chứa tất cả (sheet Tổng quan + 1 sheet/trang)
    - <output>_<trang>.json      : dữ liệu thô từng trang
    - du_lieu_images/<giờ-cào>_images/ : TẤT CẢ ảnh của lần cào (1 thư mục duy nhất
                                         theo giờ cào, nằm trong du_lieu_images/)
"""

import argparse
import concurrent.futures
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime

import requests
try:
    from facebook_scraper import get_posts, set_cookies
except ImportError:
    get_posts = None
    set_cookies = None

import cao_fb  # engine cào mới: mở trình duyệt thật (Playwright + Edge/Chrome)
from config import DUONG_DAN

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Danh sách tên phiên bị Facebook từ chối trong LẦN CÀO GẦN NHẤT
# (do run_cào nạp lại mỗi lần chạy). webui/luong đọc biến này để báo đúng
# nguyên nhân "0 bài" thay vì nói oan "trang lỗi / cookies hết hạn".
PHIEN_TU_CHOI_LAN_CAO = []


def in_tu_page(page_arg: str) -> str:
    """Trích tên/ID trang từ URL hoặc tên trực tiếp.

    Xử lý cả link dạng profile.php?id=... (không bị cắt mất phần ?id=)."""
    m = re.search(r"facebook\.com/(profile\.php\?id=\d+)", page_arg)
    if m:
        return m.group(1)
    m = re.search(r"facebook\.com/([^/?]+)", page_arg)
    return m.group(1) if m else page_arg.strip("/").split("/")[-1]


def nghi_stop(giay: float, stop_flag=None):
    """Ngủ theo từng phần nhỏ (0.2s) để bấm Dừng là dừng được ngay,
    không phải đợi hết cả khoảng nghỉ."""
    if giay <= 0:
        return
    da_ngu = 0.0
    while da_ngu < giay:
        if stop_flag and stop_flag.is_set():
            return
        time.sleep(min(0.2, giay - da_ngu))
        da_ngu += 0.2


def load_cookies(path: str):
    """Đọc cookies.txt — hỗ trợ 3 định dạng:
    1. JSON (Cookie-Editor / Firefox / 'Get cookies.txt LOCALLY' dạng mới):
       [{"name": "...", "value": "...", "domain": "...", ...}]
    2. Netscape (extension cũ): tab-separated, 7 cột
    3. document.cookie từ DevTools: cac cap ten=giatri phan cach bang ';'
    """
    if not path or not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        noi_dung = f.read().strip()
    cookies = {}

    # --- 1. Định dạng JSON ---
    if noi_dung.startswith("["):
        try:
            danh_sach = json.loads(noi_dung)
            if isinstance(danh_sach, list):
                for c in danh_sach:
                    if isinstance(c, dict) and c.get("name") and c.get("value"):
                        cookies[c["name"]] = c["value"]
        except json.JSONDecodeError:
            cookies = {}  # không phải JSON hợp lệ -> thử định dạng khác

    # --- 2. Netscape hoặc document.cookie ---
    if not cookies:
        for line in noi_dung.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:  # dinh dang Netscape
                cookies[parts[5]] = parts[6]
            elif "=" in line:    # dinh dang document.cookie
                for cap in line.split(";"):
                    cap = cap.strip()
                    if "=" in cap:
                        k, v = cap.split("=", 1)
                        cookies[k.strip()] = v.strip()

    if cookies:
        if set_cookies:
            set_cookies(cookies)
        print(f"  [i] Đã nạp {len(cookies)} cookie từ {path}")
        # kiểm tra các cookie quan trọng để cào được
        thieu = [c for c in ("c_user", "xs", "datr") if c not in cookies]
        if thieu:
            print(f"  [!] Cảnh báo: thiếu cookie quan trọng: {', '.join(thieu)} "
                  f"— có thể vẫn không cào được")
        return cookies
    else:
        print(f"  [!] Không đọc được cookie nào từ {path}")
        return {}


def an_toan_ten_file(name: str) -> str:
    """Loại ký tự không hợp lệ trong tên file."""
    return re.sub(r'[\\/:*?"<>|]', "_", name)


# ------------------------------------------------------- nhiều phiên (cookie + proxy)
def _doc_cau_hinh_proxy(ti_le_mac_dinh: float = 0.5):
    """Đọc cấu hình proxy từ config.json (im lặng nếu thiếu/không đọc được).

    Trả về (duong_dan_file | False, ti_le_phien).
    """
    try:
        from config import load_config
        cfg = (load_config() or {}).get("proxy") or {}
    except Exception:
        return False, ti_le_mac_dinh
    if not cfg.get("bat", True):
        return False, float(cfg.get("ti_le_phien") or ti_le_mac_dinh)
    return (cfg.get("proxies_file") or "proxies.txt"), float(
        cfg.get("ti_le_phien") or ti_le_mac_dinh)


def loc_proxy_song(ds_proxy: list, callback=None, max_timeout=4.0) -> list:
    """Kiểm tra nhanh kết nối proxy trực tiếp tới Facebook (song song trong ~2-3s).
    Bỏ các proxy chết/hết hạn để tránh Chromium bị treo net::ERR_TIMED_OUT 60s.
    Nếu toàn bộ proxy đều chết -> tự động cảnh báo và trả về [] để fallback về IP máy."""
    if not ds_proxy:
        return []
    import requests
    from concurrent.futures import ThreadPoolExecutor

    def _test(px):
        if not px:
            return True
        for target in ("https://www.facebook.com", "https://mbasic.facebook.com", "http://icanhazip.com"):
            try:
                r = requests.get(
                    target,
                    proxies={"http": px, "https": px},
                    timeout=max_timeout,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                )
                if r.status_code in (200, 301, 302, 403):
                    return True
            except Exception:
                continue
        return False

    with ThreadPoolExecutor(max_workers=min(15, len(ds_proxy))) as ex:
        results = list(ex.map(_test, ds_proxy))

    song = [px for px, ok in zip(ds_proxy, results) if ok]
    so_chet = len(ds_proxy) - len(song)

    if so_chet > 0:
        msg = (f"⚠️ [PROXY] Phát hiện {so_chet}/{len(ds_proxy)} proxy bị CHẾT hoặc HẾT HẠN (không kết nối được)! "
               f"Đã tự động loại bỏ để tránh lỗi net::ERR_TIMED_OUT.")
        print(f"\n{msg}")
        phat_su_kien(callback, {"loai": "log", "noi_dung": msg})

    if not song and ds_proxy:
        canh_bao = ("🚨 [CẢNH BÁO] TOÀN BỘ PROXY trong proxies.txt ĐÃ HẾT HẠN HOẶC MẤT KẾT NỐI! "
                    "Hệ thống TỰ ĐỘNG CHUYỂN SANG IP MÁY THẬT để phiên cào chạy bình thường, không gián đoạn.")
        print(f"\n{canh_bao}")
        phat_su_kien(callback, {"loai": "canh_bao", "noi_dung": canh_bao})

    return song


def nap_cac_phien(cookies: str = None, proxies: str = "proxies.txt",
                  ti_le: float = 0.5, so_phien_toi_da: int = 50,
                  callback=None, kiem_tra_song: bool = False) -> list:
    """Dựng danh sách PHIÊN cào: mỗi phiên = 1 bộ cookie + 1 IP proxy (sticky 1:1).

    File cookie tìm theo thứ tự (nếu cookies được chỉ định):
        1. file `cookies` được truyền vào -> phiên 1
        2. cookies_2.txt, cookies2.txt, ck2.txt, acc2.txt, ...
    proxies.txt: dòng i là IP của phiên i+1 (cùng thứ tự).
    """
    thu_tu = []
    if cookies:
        if os.path.exists(cookies):
            thu_tu.append(cookies)
        for i in range(2, 21):
            for mau in (f"cookies_{i}.txt", f"cookies{i}.txt",
                        f"ck{i}.txt", f"acc{i}.txt", f"tai_khoan_{i}.txt"):
                if os.path.exists(mau) and mau not in thu_tu:
                    thu_tu.append(mau)
        thu_tu = thu_tu[:max(1, so_phien_toi_da or 50)]

    ds_proxy_raw = cao_fb.doc_proxies(proxies) if proxies else []
    if kiem_tra_song and ds_proxy_raw:
        ds_proxy = loc_proxy_song(ds_proxy_raw, callback=callback)
    else:
        ds_proxy = ds_proxy_raw

    # Nếu có proxy: chia phiên theo proxy (chế độ chính - hỗ trợ cào không cần cookie)
    if ds_proxy:
        n = min(len(ds_proxy), max(1, so_phien_toi_da or 50))
        ds_phien = []
        for i in range(n):
            px = ds_proxy[i]
            ck = None
            # Trích xuất port hiển thị (vd: 12776)
            try:
                port_str = px.split(":")[-1].split("@")[0].split("/")[0]
            except Exception:
                port_str = str(i + 1)
            ten_phien = f"Proxy {i+1} (:{port_str})"
            if i < len(thu_tu):
                try:
                    c_read = cao_fb.doc_cookies(thu_tu[i])
                    if c_read:
                        ck = c_read
                        ten_phien = f"{os.path.basename(thu_tu[i])} (Proxy {i+1})"
                except Exception:
                    pass
            tl = 1.0 if n == 1 else (round(1.0 / n, 3) if not ck else (ti_le if i == 0 else (1.0 - ti_le) / (n - 1)))
            ds_phien.append({"ten": ten_phien, "cookies": ck, "proxy": px, "ti_le": tl})
            print(f"    [i] Phiên {len(ds_phien)}: {ten_phien} | {cao_fb._ru_mat_khau(px)} | {round(tl * 100)}% số trang")
        return ds_phien

    if not thu_tu:
        return [{"ten": "Phiên Trực Tiếp (Không Cookie)", "cookies": None,
                 "proxy": None, "ti_le": 1.0}]

    ds_phien = []
    n = len(thu_tu)
    ti_le = min(max(float(ti_le or 0), 0.0), 0.95)
    for i, tk in enumerate(thu_tu):
        try:
            ck = cao_fb.doc_cookies(tk)
        except Exception as e:
            print(f"    [!] Bỏ qua {tk} (không đọc được: {e})")
            continue
        if not ck:
            print(f"    [!] Bỏ qua {tk} (rỗng / không nhận diện được cookie)")
            continue
        if n == 1:
            tl = 1.0
        elif i == 0:
            tl = ti_le
        else:
            tl = (1.0 - ti_le) / (n - 1)
        px = ds_proxy[i] if i < len(ds_proxy) else None
        ds_phien.append({"ten": os.path.basename(tk), "cookies": ck,
                         "proxy": px, "ti_le": tl})
        print(f"    [i] Phiên {len(ds_phien)}: {os.path.basename(tk)} "
              f"({len(ck)} cookie) | "
              + (cao_fb._ru_mat_khau(px) if px else "IP máy (KHÔNG proxy)")
              + f" | {round(tl * 100)}% bài mỗi trang")
    return ds_phien


def phat_su_kien(callback, su_kien: dict):
    """Gửi sự kiện cho callback (web UI) nếu có — CLI thì bỏ qua."""
    if callback:
        callback(su_kien)


def phan_chia_trang(so_trang: int, ds_phien: list) -> list:
    """Gán mỗi trang nguồn cho MỘT phiên (sticky: không đổi IP giữa trang).

    Trả về danh sách chỉ số phiên, độ dài = so_trang.
    """
    n = len(ds_phien or [])
    if n <= 1 or so_trang <= 1:
        return [0] * so_trang
    tl0 = ds_phien[0].get("ti_le")
    tl0 = 0.5 if tl0 is None else float(tl0)
    if tl0 <= 0:
        tl0 = 1.0 / n                       # 0 = chia đều tuyệt đối
    phan_dau = min(so_trang, max(1, round(so_trang * tl0)))
    thu_tu = [0] * phan_dau
    con_lai = so_trang - phan_dau
    so_phien_sau = n - 1
    for k in range(so_phien_sau):
        thu_tu += [k + 1] * (con_lai // so_phien_sau)
    while len(thu_tu) < so_trang:          # phần lẻ -> phiên cuối nhận
        thu_tu.append(so_phien_sau)
    return thu_tu[:so_trang]


def tai_anh(url: str, thumuc: str, ten_file: str, proxy: str = None) -> str | None:
    """Tải một ảnh về thư mục, trả về đường dẫn đã lưu (None nếu lỗi).

    `proxy`: nếu có, ảnh cũng tải qua đúng IP của tài khoản đang cào —
    để Facebook không thấy "cào bằng IP VN nhưng tải ảnh bằng IP máy chủ".
    """
    proxies = None
    if proxy:
        px = cao_fb.chuan_hoa_proxy(proxy)
        proxies = {"http": px, "https": px}
    try:
        resp = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=30,
                            proxies=proxies)
        resp.raise_for_status()
    except Exception as e:
        print(f"    [!] Lỗi tải ảnh {ten_file}: {e}")
        return None

    ext = ".jpg"
    ct = resp.headers.get("Content-Type", "")
    if "png" in ct:
        ext = ".png"
    elif "gif" in ct:
        ext = ".gif"
    elif "webp" in ct:
        ext = ".webp"

    path = os.path.join(thumuc, ten_file + ext)
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


def xu_ly_post(post: dict, thumuc_anh: str | None, stt: int, tong: int,
               callback=None, proxy: str = None) -> dict:
    """Chuyển một post của facebook-scraper thành dict gọn, sạch và lưu ảnh."""
    post_id = post.get("post_id") or f"post_{stt}"
    text = (post.get("text") or "").strip()
    hashtags = re.findall(r"#[^\s#]+", text)

    # --- Ảnh: lấy tất cả link ảnh trong bài ---
    image_urls = list(dict.fromkeys(post.get("images") or []))

    anh_da_luu = []
    if thumuc_anh and image_urls:
        os.makedirs(thumuc_anh, exist_ok=True)
        for i, url in enumerate(image_urls):
            print(f"    [↓] Tải ảnh {i + 1}/{len(image_urls)} ...")
            # Tất cả ảnh nằm chung 1 thư mục (tên file có post_id để khỏi trùng)
            path = tai_anh(url, thumuc_anh,
                           f"{an_toan_ten_file(str(post_id))}_{i + 1}", proxy=proxy)
            if path:
                anh_da_luu.append(path)
            phat_su_kien(callback, {
                "loai": "anh", "post_id": post_id,
                "so_thu_tu": i + 1, "tong": len(image_urls),
                "duong_dan": path,
            })

    time_post = post.get("time")
    if isinstance(time_post, datetime):
        time_post = time_post.isoformat()

    print(
        f"  [✓] #{stt} | ID: {post_id} | {time_post} | "
        f"text {len(text)} ký tự | {len(image_urls)} ảnh | "
        f"{len(hashtags)} hashtag"
    )

    return {
        "post_id": post_id,
        "time": time_post,
        "text": text,
        "hashtags": hashtags,          # ví dụ: ['#PUBG', '#PUBGVN']
        "images": image_urls,          # link ảnh gốc trên Facebook
        "images_da_tai": anh_da_luu,   # đường dẫn ảnh đã tải về máy
        "is_reel": bool(post.get("is_reel")),   # bài reel → dựng 'reel tĩnh' ở bước 4
        "post_url": post.get("post_url"),
        "likes": post.get("likes"),        # tổng số cảm xúc
        "comments": post.get("comments"),  # số bình luận
        "shares": post.get("shares"),      # số lượt chia sẻ
        "diem_tiem_nang": 0,               # điểm tương tác (tính sau)
        "muc_tiem_nang": "—",              # CAO / TRUNG_BINH / THAP (tính sau)
    }


def danh_gia_tiem_nang(posts: list):
    """Chấm điểm tương tác và xếp hạng tiềm năng cho từng bài.

    Công thức: diem = likes + comments*2 + shares*3
    (bình luận và chia sẻ nặng hơn like vì thể hiện tương tác sâu).
    Xếp hạng theo thứ hạng trong chính trang đó:
    - Top 25%  -> CAO
    - 25-60%   -> TRUNG_BINH
    - còn lại  -> THAP
    """
    if len(posts) < 5:
        return  # quá ít bài, không đủ cơ sở xếp hạng
    for p in posts:
        like = p.get("likes") or 0
        cmt = p.get("comments") or 0
        share = p.get("shares") or 0
        p["diem_tiem_nang"] = like + cmt * 2 + share * 3

    posts.sort(key=lambda p: p["diem_tiem_nang"], reverse=True)
    if not posts or posts[0]["diem_tiem_nang"] == 0:
        return  # tất cả đều 0 tương tác -> không xếp hạng

    n = len(posts)
    for i, p in enumerate(posts):
        phan_tram = (i + 1) / n
        if phan_tram <= 0.25:
            p["muc_tiem_nang"] = "CAO"
        elif phan_tram <= 0.60:
            p["muc_tiem_nang"] = "TRUNG_BINH"
        else:
            p["muc_tiem_nang"] = "THAP"


def in_top_tiem_nang(posts: list, top_n: int = 5):
    """In danh sách bài tiềm năng nhất ra màn hình."""
    print(f"\n  🏆 Top {min(top_n, len(posts))} bài TIỀM NĂNG nhất:")
    for i, p in enumerate(posts[:top_n]):
        print(
            f"    {i + 1}. [{p['muc_tiem_nang']}] điểm {p['diem_tiem_nang']} "
            f"(cảm xúc {p['likes']}, bình luận {p['comments']}, chia sẻ {p['shares']})"
        )
        if p["text"]:
            print(f"       {p['text'][:100]}{'...' if len(p['text']) > 100 else ''}")
        print(f"       {p['post_url']}")


def ghi_json(posts: list, ten_file: str):
    """Ghi file JSON lưu trữ dữ liệu thô."""
    json_path = f"{ten_file}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
    return json_path


def ghi_excel(ket_qua: dict, ten_file: str) -> str:
    """Xuất TOÀN BỘ dữ liệu ra 1 file Excel .xlsx (engine xlsxwriter).

    - Sheet 'Tổng quan': thống kê từng trang + Top 10 bài tiềm năng nhất toàn bộ
    - 1 sheet cho mỗi trang: từng bài kèm cảm xúc, bình luận, chia sẻ,
      điểm tiềm năng (tô màu: CAO xanh lá / TRUNG_BINH vàng / THAP xám),
      đường dẫn ảnh đã tải, link bài bấm được.
    """
    import xlsxwriter

    cot = [
        ("STT", 5), ("post_id", 16), ("Thời gian", 20), ("Nội dung bài viết", 65),
        ("Cảm xúc", 11), ("Bình luận", 11), ("Chia sẻ", 10),
        ("Điểm tiềm năng", 13), ("Mức tiềm năng", 14),
        ("Ảnh đã tải (đường dẫn)", 50), ("Link bài viết", 16),
    ]

    xlsx_path = f"{ten_file}.xlsx"
    thumuc = os.path.dirname(xlsx_path)
    if thumuc:
        os.makedirs(thumuc, exist_ok=True)

    wb = xlsxwriter.Workbook(xlsx_path)

    # ---------- Các định dạng (format) ----------
    header_format = wb.add_format({
        "bold": True, "font_color": "#FFFFFF", "bg_color": "#1F4E78",
        "align": "center", "valign": "vcenter",
        "border": 1, "border_color": "#D9D9D9",
    })
    text_format = wb.add_format({
        "valign": "top", "border": 1, "border_color": "#D9D9D9",
    })
    num_format = wb.add_format({
        "valign": "top", "border": 1, "border_color": "#D9D9D9",
    })
    wrap_format = wb.add_format({
        "text_wrap": True, "valign": "top", "border": 1, "border_color": "#D9D9D9",
    })
    link_format = wb.add_format({
        "font_color": "#0563C1", "underline": 1,
        "valign": "top", "border": 1, "border_color": "#D9D9D9",
    })
    title_format = wb.add_format({"bold": True, "font_size": 14})
    bold_13 = wb.add_format({"bold": True, "font_size": 13})
    italic_gray = wb.add_format({"italic": True, "font_color": "#64748B"})
    tier_format = {
        "CAO": wb.add_format({"bg_color": "#C6EFCE", "font_color": "#006100",
                              "bold": True, "valign": "top",
                              "border": 1, "border_color": "#D9D9D9"}),
        "TRUNG_BINH": wb.add_format({"bg_color": "#FFEB9C", "font_color": "#9C6500",
                                     "bold": True, "valign": "top",
                                     "border": 1, "border_color": "#D9D9D9"}),
        "THAP": wb.add_format({"bg_color": "#F2F2F2", "font_color": "#808080",
                               "valign": "top", "border": 1, "border_color": "#D9D9D9"}),
    }

    def ke_dong_dau(ws, headers):
        """Tô header, kẻ khung, lọc tự động, đóng băng dòng đầu."""
        for c, (tieu_de, _) in enumerate(headers):
            ws.write(0, c, tieu_de, header_format)
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, 0, len(headers) - 1)
        for c, (_, rong) in enumerate(headers):
            ws.set_column(c, c, rong)

    def ghi_mot_bai(ws, r, post):
        """Ghi 1 bài vào dòng r (0-based; dòng 0 là header)."""
        ws.write_number(r, 0, r, num_format)  # STT
        ws.write(r, 1, post.get("post_id"), text_format)
        ws.write(r, 2, post.get("time"), text_format)
        # Hashtag được ghi NGAY TRONG ô nội dung, xuống dòng phía dưới bài viết
        text = post.get("text") or ""
        hashtags = " ".join(post.get("hashtags") or [])
        noi_dung = text + ("\n\n" + hashtags if hashtags else "")
        ws.write(r, 3, noi_dung, wrap_format)
        # Chiều cao dòng theo lượng chữ — nếu không đặt, Excel chỉ hiện 1 dòng
        # đầu của mỗi bài (text vẫn đầy đủ trong ô nhưng bị ẩn).
        # Cột rộng 65 -> ~60 ký tự/dòng. Excel giới hạn dòng 409.5pt.
        so_dong = max(1, -(-len(noi_dung) // 60))
        chieu_cao = min(so_dong * 15 + 4, 409.5)
        ws.write_number(r, 4, post.get("likes") or 0, num_format)
        ws.write_number(r, 5, post.get("comments") or 0, num_format)
        ws.write_number(r, 6, post.get("shares") or 0, num_format)
        ws.write_number(r, 7, post.get("diem_tiem_nang") or 0, num_format)
        muc = post.get("muc_tiem_nang") or "—"
        if muc in tier_format:
            ws.write(r, 8, muc, tier_format[muc])
        else:
            ws.write(r, 8, muc, text_format)
        # Đường dẫn ẢNH ĐẦY ĐỦ trên máy
        # (VD: C:\traffic fb\du_lieu_images\du_lieu_2026-08-24_09-27-08_images\post_id_1.jpg)
        cac_anh = [os.path.abspath(x) for x in (post.get("images_da_tai") or [])]
        ws.write(r, 9, "\n".join(cac_anh), wrap_format)
        if cac_anh:
            # dòng cao thêm nếu nhiều ảnh
            chieu_cao = max(chieu_cao, min(len(cac_anh) * 15 + 4, 409.5))
        # Link bài viết: dán URL THẬT vào ô (bấm vẫn mở được Facebook)
        url = post.get("post_url")
        if url:
            ws.write_url(r, 10, url, link_format, url)
        else:
            ws.write(r, 10, "—", text_format)
        ws.set_row(r, chieu_cao)

    # ================= Sheet Tổng quan =================
    ws = wb.add_worksheet("Tổng quan")
    ws.write(0, 0, "TỔNG QUAN CÁC TRANG ĐÃ CÀO", title_format)

    tieu_de_tong_quan = ["Trang", "Số bài", "Tổng cảm xúc", "Tổng bình luận",
                         "Tổng chia sẻ", "Tổng điểm", "Bài hay nhất"]
    for c, td in enumerate(tieu_de_tong_quan):
        ws.write(2, c, td, header_format)
    ws.set_column(0, 0, 30)
    for col in range(1, 6):
        ws.set_column(col, col, 15)
    ws.set_column(6, 6, 45)

    r = 3
    tat_ca = []
    for page, posts in ket_qua.items():
        tong_diem = sum((p.get("diem_tiem_nang") or 0) for p in posts)
        bai_hay = posts[0] if posts and posts[0].get("post_url") else None
        ws.write(r, 0, page, text_format)
        ws.write_number(r, 1, len(posts), num_format)
        ws.write_number(r, 2, sum(p.get("likes") or 0 for p in posts), num_format)
        ws.write_number(r, 3, sum(p.get("comments") or 0 for p in posts), num_format)
        ws.write_number(r, 4, sum(p.get("shares") or 0 for p in posts), num_format)
        ws.write_number(r, 5, tong_diem, num_format)
        if bai_hay:
            ws.write_url(r, 6, bai_hay["post_url"], link_format,
                         (bai_hay.get("text") or "")[:60] or bai_hay["post_id"])
        tat_ca.extend((p | {"_trang": page}) for p in posts)
        r += 1

    r += 1
    ws.write(r, 0, "→ Cảm xúc / bình luận / chia sẻ CỦA TỪNG BÀI: mở sheet riêng của mỗi trang "
                   "(tab phía dưới, đặt tên theo trang)", italic_gray)
    r += 1
    ws.write(r, 0, "TOP 10 BÀI TIỀM NĂNG NHẤT TOÀN BỘ", bold_13)
    r += 1
    tieu_de_top = ["STT", "Trang", "post_id", "Nội dung", "Điểm", "Mức", "Link"]
    for c, td in enumerate(tieu_de_top):
        ws.write(r, c, td, header_format)
    r += 1
    for i, p in enumerate(sorted(tat_ca, key=lambda x: x.get("diem_tiem_nang") or 0, reverse=True)[:10], 1):
        ws.write_number(r, 0, i, num_format)
        ws.write(r, 1, p.get("_trang"), text_format)
        ws.write(r, 2, p.get("post_id"), text_format)
        ws.write(r, 3, (p.get("text") or "")[:100], text_format)
        ws.write_number(r, 4, p.get("diem_tiem_nang") or 0, num_format)
        muc = p.get("muc_tiem_nang") or "—"
        if muc in tier_format:
            ws.write(r, 5, muc, tier_format[muc])
        else:
            ws.write(r, 5, muc, text_format)
        if p.get("post_url"):
            ws.write_url(r, 6, p["post_url"], link_format, "Mở bài viết")
        r += 1

    # ================= Sheet từng trang =================
    ten_sheet_da_dung = set()
    for page, posts in ket_qua.items():
        ten_sheet = re.sub(r'[\\/*?:\[\]]', "_", page)[:31] or "trang"
        # Tránh trùng tên sheet (VD: 2 profile page có prefix giống nhau)
        if ten_sheet in ten_sheet_da_dung:
            duoi = 2
            while f"{ten_sheet[:27]}_{duoi}" in ten_sheet_da_dung:
                duoi += 1
            ten_sheet = f"{ten_sheet[:27]}_{duoi}"
        ten_sheet_da_dung.add(ten_sheet)
        ws = wb.add_worksheet(ten_sheet)
        ke_dong_dau(ws, cot)
        for i, post in enumerate(posts, 1):  # dòng 1 = dòng dữ liệu đầu (0-based)
            ghi_mot_bai(ws, i, post)

    wb.close()
    return xlsx_path


def cào_mot_trang(page: str, pages_scroll: int, limit: int, thumuc_anh: str | None,
                  delay: float, callback=None, stop_flag=None,
                  cookies_playwright: list = None, kiem_tra_pool=None,
                  proxy: str = None, browser=None) -> list:
    """Cào một trang, trả về danh sách post đã xử lý.

    - Có cookies -> dùng engine mới (cao_fb): mở trình duyệt thật, cuộn, đọc DOM.
    - Không có cookies -> thử thư viện facebook-scraper cũ (thường bị FB chặn).
    - `kiem_tra_pool`: KiemTraPool — bài đã có trong kho bị bỏ qua, không tính
      vào hạn mức `limit` (chỉ đếm bài MỚI).
    - `proxy`: IP đi kèm bộ cookie của phiên này (sticky 1:1).
    """
    posts = []
    phat_su_kien(callback, {"loai": "trang_bat_dau", "page": page, "gioi_han": limit})
    try:
        # Luôn dùng Playwright engine thật (cao_fb), hỗ trợ cả có cookie lẫn chế độ khách (Guest mode)
        bai_tho = cao_fb.cào_trang(
            page, so_bai=limit or 0, so_lan_cuon=pages_scroll,
            delay=delay, cookies=cookies_playwright,
            stop_flag=stop_flag, callback=callback,
            kiem_tra_pool=kiem_tra_pool, proxy=proxy, browser=browser,
        )
        for bai in bai_tho:
            # Bỏ qua hoàn toàn các bài không có caption hợp lệ
            if not cao_fb.la_caption_hop_le(bai.get("text") or ""):
                continue
            if stop_flag and stop_flag.is_set():
                print(f"  [i] Đã dừng theo yêu cầu tại trang {page}")
                break
            try:
                post_da_xu_ly = xu_ly_post(bai, thumuc_anh, len(posts) + 1,
                                           limit or "?", callback=callback,
                                           proxy=proxy)
                posts.append(post_da_xu_ly)
                phat_su_kien(callback, {
                    "loai": "post", "page": page, "post": post_da_xu_ly,
                    "stt": len(posts), "tong": limit or None,
                })
            except Exception as ep:
                print(f"  [!] Lỗi xử lý bài {bai.get('post_id', '?')}: {ep}")
                continue
            if limit and len(posts) >= limit:
                break
            nghi_stop(delay, stop_flag)
        if not posts:
            print(f"  [i] Trang {page}: Không có bài mới trong kho (feed đã hết hoặc bài cũ).")
    except Exception as e:
        print(f"\n[!] Lỗi khi cào: {e}")
        phat_su_kien(callback, {"loai": "loi", "page": page, "noi_dung": str(e)})
        raise
    phat_su_kien(callback, {"loai": "trang_xong", "page": page, "so_bai": len(posts)})
    return posts


def run_cào(trang_ho: list, pages: int = 3, limit: int = 0, per_page: int = 0,
            output: str = "du_lieu", no_images: bool = False, images_dir: str = None,
            cookies: str = None, delay: float = 3.0,
            callback=None, stop_flag=None, bo_qua_bai_cu: bool = False,
            proxies_path=None, ti_le_phien: float = 0.5):
    """Phần lõi cào — dùng chung cho CLI (main) và Web UI.

    Trả về (ket_qua, xlsx_path) với ket_qua = {trang: [posts]}.
    Mỗi bước đều gửi sự kiện qua callback nếu có.

    `bo_qua_bai_cu=True`: nạp KiemTraPool (kho hiện tại) — bài đã có trong kho
    bị bỏ qua ngay lúc cào và KHÔNG tính vào hạn mức `limit`/`per_page`
    (đủ N bài = N bài MỚI).

    `proxies_path`:
        None  -> lấy theo config.json mục "proxy" (khuyên dùng)
        False -> TẮT proxy, cào bằng IP máy
        str   -> dùng đúng file này
    """
    if proxies_path is None:
        proxies_path, ti_le_phien = _doc_cau_hinh_proxy(ti_le_phien)
    print(f"=== Cào {len(trang_ho)} trang: {', '.join(trang_ho)} ===")
    print(f"    {pages} lần cuộn/trang, tối đa {per_page or limit or 'không'} bài/trang")
    # Nạp các PHIÊN (mỗi phiên = 1 bộ cookie + 1 IP proxy, ghép 1:1).
    # Không có proxies.txt -> vẫn chạy đúng như trước (1 phiên, IP máy).
    ds_phien = nap_cac_phien(cookies, proxies_path, ti_le_phien, callback=callback, kiem_tra_song=True)
    if not ds_phien:
        cookies_playwright = cao_fb.doc_cookies(cookies) if cookies else None
        if cookies_playwright:
            ds_phien = [{"ten": os.path.basename(cookies),
                         "cookies": cookies_playwright, "proxy": None,
                         "ti_le": 1.0}]
        else:
            ds_phien = [{"ten": "IP Máy (Không Cookie)",
                         "cookies": None, "proxy": None,
                         "ti_le": 1.0}]
    elif len(ds_phien) > 1:
        print(f"    [i] Dùng {len(ds_phien)} phiên (cookie + proxy) luân phiên "
              f"theo trang nguồn.")

    # Chia trang nguồn cho từng phiên (sticky: 1 trang = 1 phiên từ đầu tới cuối)
    gan_nhu_xong = phan_chia_trang(len(trang_ho), ds_phien)
    cao_fb.dat_lai_cophien()   # xóa cờ "phiên bị từ chối" của lần chạy trước
    global PHIEN_TU_CHOI_LAN_CAO
    PHIEN_TU_CHOI_LAN_CAO = []
    if ds_phien and len(ds_phien) > 1 and len(trang_ho) > 1:
        for pi, ph in enumerate(ds_phien):
            so = gan_nhu_xong.count(pi)
            print(f"    [i] {ph['ten']} -> {so} trang"
                  + (f" ({cao_fb._ru_mat_khau(ph['proxy'])})" if ph["proxy"] else ""))

    # 1 lần cào = 1 giờ cào — dùng chung cho cả thư mục ảnh và tên file Excel
    thoi_gian_cao = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    # TẤT CẢ ảnh của lần cào nằm trong MỘT thư mục đặt tên theo giờ cào,
    # và thư mục đó nằm BÊN TRONG du_lieu_images/
    # (VD: du_lieu_images/du_lieu_2026-08-24_09-52-42_images/)
    core_img = os.path.join(DUONG_DAN, "core", "du_lieu_images")
    base_img = core_img if os.path.isdir(os.path.join(DUONG_DAN, "core")) else os.path.join(DUONG_DAN, "du_lieu_images")
    thumuc_anh = None if no_images else (
        images_dir or os.path.join(base_img,
                                   f"{output}_{thoi_gian_cao}_images"))

    # Bộ kiểm tra kho: bỏ qua bài đã có (không tính vào hạn mức bài MỚI)
    kiem_tra_pool = None
    if bo_qua_bai_cu:
        try:
            from content_pool import KiemTraPool
            kiem_tra_pool = KiemTraPool()
            print(f"    [i] Lọc trùng kho: bỏ qua {len(kiem_tra_pool.ds_id)} ID "
                  f"+ {len(kiem_tra_pool.ds_caption)} caption đã có.")
        except Exception as e:
            print(f"    [!] Không nạp được KiemTraPool ({e}) — cào bình thường.")

    ket_qua = {}
    phien_truoc = None
    lock_kq = threading.Lock()
    lock_cb = threading.Lock()

    # KÍCH HOẠT CHẾ ĐỘ CÀO ĐA LUỒNG SONG SONG THEO PROXY:
    # Điều kiện: Có từ 2 phiên (proxy) trở lên VÀ có từ 2 trang cần cào trở lên!
    so_luong_phien = len(ds_phien) if ds_phien else 1
    chay_song_song = (so_luong_phien > 1 and len(trang_ho) > 1)

    if chay_song_song:
        # Mỗi worker = 1 trình duyệt thật (~500-700MB) -> giới hạn theo RAM
        # còn trống để tránh tràn RAM khi proxies.txt có nhiều proxy.
        tran_theo_ram = cao_fb.gioi_han_trinh_duyet_theo_ram()
        so_worker = min(so_luong_phien, len(trang_ho), tran_theo_ram)
        thong_bao_multi = (
            f"🚀 [MULTI-PROXY] Kích hoạt cào {so_worker} luồng song song "
            f"(tương ứng {so_worker} proxy độc lập) cho {len(trang_ho)} trang!"
        )
        print(f"\n{thong_bao_multi}")
        phat_su_kien(callback, {"loai": "log", "noi_dung": thong_bao_multi})

        hang_doi = queue.Queue()
        for idx_trang, p_url in enumerate(trang_ho):
            hang_doi.put((idx_trang, p_url))

        def worker_loop(w_idx):
            from playwright.sync_api import sync_playwright
            phien_w = ds_phien[w_idx % len(ds_phien)]
            ten_p = phien_w.get("ten", f"Proxy {w_idx+1}")
            px_w = phien_w.get("proxy")
            ck_w = phien_w.get("cookies")

            # TÁI SỬ DỤNG 1 TRÌNH DUYỆT DUY NHẤT CHO WORKER SUỐT QUÁ TRÌNH CÀO
            with sync_playwright() as p:
                browser = None
                try:
                    browser = cao_fb.mo_trinh_duyet(p, cookies=ck_w, proxy=px_w)
                except Exception as e_br:
                    with lock_cb:
                        print(f"  ❌ [{ten_p}] Lỗi khởi tạo trình duyệt ({e_br})")
                    return

                so_trang_worker = 0
                try:
                    while not hang_doi.empty():
                        if stop_flag and stop_flag.is_set():
                            break
                        try:
                            i_tg, p_url = hang_doi.get_nowait()
                        except queue.Empty:
                            break

                        with lock_cb:
                            print(f"\n----- [{ten_p}] Bắt đầu cào trang ({i_tg + 1}/{len(trang_ho)}): {p_url} -----")
                            phat_su_kien(callback, {
                                "loai": "phien", "stt": i_tg + 1, "page": p_url,
                                "ten": ten_p, "proxy": bool(px_w), "worker": w_idx + 1
                            })

                        core_fb = os.path.join(DUONG_DAN, "core", "du_lieu_fb")
                        thumuc_fb = core_fb if os.path.isdir(os.path.join(DUONG_DAN, "core")) else os.path.join(DUONG_DAN, "du_lieu_fb")
                        os.makedirs(thumuc_fb, exist_ok=True)
                        page_id_sach = an_toan_ten_file(in_tu_page(p_url))
                        ten_file_base = output if len(trang_ho) == 1 else f"{output}_{page_id_sach}"
                        ten_file = os.path.join(thumuc_fb, ten_file_base)

                        posts = []
                        try:
                            posts = cào_mot_trang(
                                p_url, pages, per_page or limit, thumuc_anh, delay,
                                callback=callback, stop_flag=stop_flag,
                                cookies_playwright=ck_w, browser=browser,
                                kiem_tra_pool=kiem_tra_pool, proxy=px_w
                            )
                        except Exception as e:
                            loi_trang = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
                            with lock_cb:
                                print(f"\n[!] [{ten_p}] {p_url}: LỖI khi cào ({loi_trang}) -> bỏ qua trang này.")
                                phat_su_kien(callback, {
                                    "loai": "loi", "page": p_url, "bo_qua_trang": True,
                                    "noi_dung": f"Trang {p_url} cào thất bại ({loi_trang}) — đã BỎ QUA."
                                })
                            posts = []

                        with lock_kq:
                            ket_qua[p_url] = posts

                        if posts:
                            with lock_cb:
                                danh_gia_tiem_nang(posts)
                                in_top_tiem_nang(posts)
                        else:
                            with lock_cb:
                                phat_su_kien(callback, {"loai": "khong_bai", "page": p_url})

                        try:
                            json_path = ghi_json(posts, ten_file)
                            with lock_cb:
                                print(f"  ✅ [{ten_p}] {p_url}: {len(posts)} bài -> {json_path}")
                        except Exception:
                            pass

                        hang_doi.task_done()
                        if delay > 0 and not hang_doi.empty():
                            nghi_stop(delay, stop_flag)

                finally:
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
                    try:
                        import gc
                        gc.collect()
                    except Exception:
                        pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=so_worker) as executor:
            futures = [executor.submit(worker_loop, w_i) for w_i in range(so_worker)]
            concurrent.futures.wait(futures)
        try:
            import gc
            gc.collect()
        except Exception:
            pass

    else:
        # Phiên nào bị Facebook từ chối (cookie bị xóa / đòi đăng nhập) -> đánh dấu
        # ở đây để KHÔNG mở trình duyệt cho các trang của phiên đó nữa. Nếu chỉ có
        # MỘT phiên thì coi như dừng cả lần cào.
        phien_chet = {}
        for i, page in enumerate(trang_ho):
            pi = gan_nhu_xong[i] if i < len(gan_nhu_xong) else 0
            pi = min(pi, len(ds_phien) - 1) if ds_phien else 0
            phien = ds_phien[pi] if ds_phien else None
            ten_phien = (phien or {}).get("ten") or (cookies or "cookies.txt")
            if phien_chet.get(ten_phien):
                print(f"\n----- Trang {i + 1}/{len(trang_ho)}: {page} -----"
                      f"\n  [—] BỎ QUA: phiên '{ten_phien}' đã bị Facebook từ chối từ "
                      f"trước; cào trang này cũng chỉ ra BẢN KHÁCH (1-3 bài).")
                ket_qua[page] = []
                continue
            if phien is None:
                cookies_playwright = cao_fb.doc_cookies(cookies) if cookies else None
                px = None
            else:
                cookies_playwright = phien["cookies"]
                px = phien["proxy"]
            print(f"\n----- Trang {i + 1}/{len(trang_ho)}: {page} -----")
            if len(ds_phien) > 1 and phien_truoc is not None and pi != phien_truoc:
                nghi = max(delay, 20.0)
                print(f"  ... đổi phiên sang '{phien['ten']}', nghỉ {nghi:.0f}s "
                      f"(mỗi phiên là một 'người dùng' khác nhau)")
                nghi_stop(nghi, stop_flag)
            phien_truoc = pi
            if phien and len(ds_phien) > 1:
                phat_su_kien(callback, {"loai": "phien", "stt": i + 1,
                                        "page": page, "ten": phien["ten"],
                                        "proxy": bool(px)})
            core_fb = os.path.join(DUONG_DAN, "core", "du_lieu_fb")
            thumuc_fb = core_fb if os.path.isdir(os.path.join(DUONG_DAN, "core")) else os.path.join(DUONG_DAN, "du_lieu_fb")
            os.makedirs(thumuc_fb, exist_ok=True)
            page_id_sach = an_toan_ten_file(in_tu_page(page))
            ten_file_base = output if len(trang_ho) == 1 else f"{output}_{page_id_sach}"
            ten_file = os.path.join(thumuc_fb, ten_file_base)

            # MỘT trang lỗi (Playwright timeout, crash trình duyệt, Facebook chẹn)
            # KHÔNG được khai tử cả lần cào: dữ liệu các trang trước đã nằm trong
            # `ket_qua` và mới sắp được ghi Excel/nạp pool. Bỏ qua trang lỗi, ghi rõ
            # lý do để người dùng biết trang nào trắng, rồi chạy tiếp trang sau.
            try:
                posts = cào_mot_trang(page, pages, per_page or limit, thumuc_anh, delay,
                                      callback=callback, stop_flag=stop_flag,
                                      cookies_playwright=cookies_playwright,
                                      kiem_tra_pool=kiem_tra_pool, proxy=px)
            except Exception as e:
                loi_trang = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
                print(f"\n[!] {page}: LỖI khi cào ({loi_trang}) -> bỏ qua trang này, "
                      f"giữ nguyên {sum(len(v) for v in ket_qua.values())} bài đã cào.")
                phat_su_kien(callback, {
                    "loai": "loi", "page": page, "bo_qua_trang": True,
                    "noi_dung": f"Trang này cào thất bại ({loi_trang}) — đã BỎ QUA, "
                                "các trang khác vẫn chạy, dữ liệu đã cào KHÔNG mất."})
                ket_qua[page] = []
                if stop_flag and stop_flag.is_set():
                    break
                if i < len(trang_ho) - 1 and delay > 0:
                    nghi_stop(delay, stop_flag)
                continue

            if posts:
                danh_gia_tiem_nang(posts)
                in_top_tiem_nang(posts)
            else:
                phat_su_kien(callback, {"loai": "khong_bai", "page": page})
                if getattr(cao_fb, "PHIEN_BI_TU_CHOI", False):
                    # Phiên này đã chết — ghi nhớ để bỏ qua các trang CÒN LẠI của nó.
                    phien_chet[ten_phien] = True
                    if ten_phien not in PHIEN_TU_CHOI_LAN_CAO:
                        PHIEN_TU_CHOI_LAN_CAO.append(ten_phien)
                    con_song = [p["ten"] for p in ds_phien if not phien_chet.get(p["ten"])]
                    if len(ds_phien) <= 1 or not con_song:
                        print("\n[✗] DỪNG CẢ LẦN CÀO: Facebook đã vô hiệu hóa PHIÊN "
                              "CÀO (cookie đăng nhập bị xóa / đòi đăng nhập).\n"
                              f"    Trang '{page}' chỉ xem được BẢN KHÁCH (1-3 bài, "
                              "cuộn không ăn) — đó KHÔNG phải lỗi trang hay 'trang "
                              "hết bài'.\n"
                              "    Mở Facebook bằng trình duyệt thường → đăng nhập "
                              "lại → xuất lại cookies.txt → chạy lại.")
                        phat_su_kien(callback, {
                            "loai": "loi", "page": page, "dung_ca_lan": True,
                            "noi_dung": "MỌI PHIÊN CÀO ĐỀU BỊ FACEBOOK TỪ CHỐI "
                                        "(cookie đăng nhập bị xóa ngay khi mở trang). "
                                        "Các trang chỉ xem được bản khách (1-3 bài, "
                                        "cuộn không ăn) — KHÔNG phải lỗi trang. Cần "
                                        "ĐĂNG NHẬP LẠI và xuất lại file cookies.txt."})
                        ket_qua[page] = posts
                        break
                    print(f"\n[✗] Phiên '{ten_phien}' bị Facebook TỪ CHỐI (cookie "
                          "bị xóa / đòi đăng nhập) → bỏ qua mọi trang còn lại của "
                          f"phiên này, tiếp tục với {len(con_song)} phiên còn tốt: "
                          f"{', '.join(con_song)}")
                    phat_su_kien(callback, {
                        "loai": "canh_bao", "page": page,
                        "noi_dung": f"Phiên '{ten_phien}' bị Facebook từ chối "
                                    "(cookie hết hạn/bị vô hiệu hóa) → bỏ qua các "
                                    "trang của phiên này. "
                                    f"Còn {len(con_song)} phiên vẫn chạy tiếp."})

            json_path = ghi_json(posts, ten_file)
            print(f"  ✅ {page}: {len(posts)} bài -> {json_path}")
            if thumuc_anh:
                print(f"     Ảnh: {thumuc_anh}")
            ket_qua[page] = posts

            if i < len(trang_ho) - 1 and delay > 0:
                print(f"  ... nghỉ {delay}s trước khi sang trang tiếp theo")
                nghi_stop(delay, stop_flag)

    # Lưu file excel lịch sử cào thô vào trong du_lieu_fb/lich_su_cao/ để KHÔNG làm rác thư mục du_lieu_exel
    thumuc_ls = os.path.join(DUONG_DAN, "du_lieu_fb", "lich_su_cao")
    os.makedirs(thumuc_ls, exist_ok=True)
    ten_xlsx = os.path.join(thumuc_ls, f"{output}_{thoi_gian_cao}")
    try:
        xlsx_path = ghi_excel(ket_qua, ten_xlsx)
    except Exception as e:
        # Lỗi ghi Excel KHÔNG được làm mất dữ liệu bài đã cào — vẫn trả ket_qua
        print(f"  ⚠️ Lỗi ghi Excel: {e}")
        phat_su_kien(callback, {"loai": "log",
                                "noi_dung": f"⚠️ Lỗi ghi file Excel (dữ liệu bài vẫn còn): {e}"})
        xlsx_path = f"{ten_xlsx}.xlsx"

    print(f"\n=== HOÀN THÀNH: {sum(len(p) for p in ket_qua.values())} bài viết từ {len(ket_qua)} trang ===")
    for page, posts in ket_qua.items():
        print(f"  - {page}: {len(posts)} bài")
    print(f"\n📊 MỞ FILE EXCEL: {xlsx_path}")
    print("   (sheet 'Tổng quan' = thống kê + top 10 tiềm năng; 1 sheet/trang)")
    return ket_qua, xlsx_path


def main():
    parser = argparse.ArgumentParser(
        description="Cào bài viết + ảnh từ nhiều Facebook Page (trang public)."
    )
    parser.add_argument(
        "--page", nargs="+", required=True,
        help="Tên hoặc URL trang — ghi được NHIỀU trang, cách nhau bằng dấu cách",
    )
    parser.add_argument(
        "--pages", type=int, default=3,
        help="Số lần cuộn cho mỗi trang (mỗi lần ~200 bài, mặc định 3)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Giới hạn số bài (0 = không giới hạn)",
    )
    parser.add_argument(
        "--per-page", type=int, default=0,
        help="Giới hạn số bài cho MỖI trang (0 = dùng --limit)",
    )
    parser.add_argument(
        "--output", default="du_lieu",
        help="Tiền tố tên file đầu ra, mặc định: du_lieu (file: du_lieu_<trang>.json + du_lieu.xlsx)",
    )
    parser.add_argument(
        "--images-dir", default=None,
        help="Thư mục lưu ảnh (mặc định: du_lieu_images/<tiền tố>_<giờ-cào>_images — tất cả ảnh 1 lần cào)",
    )
    parser.add_argument(
        "--no-images", action="store_true",
        help="Không tải ảnh, chỉ lấy link ảnh",
    )
    parser.add_argument(
        "--cookies", default=None,
        help="File cookies.txt (bắt buộc vì FB chặn truy cập ẩn danh)",
    )
    parser.add_argument(
        "--delay", type=float, default=3.0,
        help="Giây nghỉ giữa mỗi lần cuộn và giữa các trang (giảm rủi ro bị chặn)",
    )
    parser.add_argument(
        "--proxies", default=None,
        help="File proxies.txt: dòng i ghép bộ cookie i (sticky 1:1). "
             "Mặc định lấy theo config.json mục proxy; --no-proxy để tắt.",
    )
    parser.add_argument(
        "--no-proxy", action="store_true",
        help="Bỏ qua proxies.txt, cào bằng IP máy như cũ",
    )
    parser.add_argument(
        "--phien-ty-le", type=float, default=0.5,
        help="Phần trang nguồn phiên 1 gánh (0-0.95), phần còn lại chia cho "
             "các phiên cookie phụ. Mặc định 0.5",
    )
    args = parser.parse_args()

    trang_ho = [in_tu_page(p) for p in args.page]
    ket_qua, xlsx_path = run_cào(
        trang_ho, pages=args.pages, limit=args.limit, per_page=args.per_page,
        output=args.output, no_images=args.no_images, images_dir=args.images_dir,
        cookies=args.cookies, delay=args.delay,
        proxies_path=None if args.no_proxy else args.proxies,
        ti_le_phien=args.phien_ty_le,
    )

    if not any(ket_qua.values()):
        print("\n[!] Không lấy được bài nào. Facebook hiện CHẶN truy cập ẩn danh (không đăng nhập).")
        print("    Giải pháp: xuất cookies từ trình duyệt đang đăng nhập Facebook rồi chạy lại:\n")
        print("    Cách 1 - Dùng extension (dễ nhất):")
        print("        Cài 'Get cookies.txt LOCALLY' cho Chrome/Edge (miễn phí trên store),")
        print("        mở trang facebook.com -> bấm icon extension -> Export -> lưu cookies.txt")
        print("    Cách 2 - Tự copy từ DevTools:")
        print("        Mở facebook.com (đã đăng nhập) -> F12 -> Console, dán lệnh:")
        print("        copy(document.cookie) -> lưu vào cookies.txt dạng:  ten_cookie\tgia_tri  (mỗi dòng 1 cặp)\n")
        print(f"    Sau đó chạy:")
        print(f"        python scraper.py --page {' '.join(trang_ho)} --cookies cookies.txt")
        sys.exit(1)


if __name__ == "__main__":
    main()
