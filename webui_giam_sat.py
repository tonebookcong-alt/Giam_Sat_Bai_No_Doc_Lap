"""WebUI Độc Lập cho Hệ Thống Giám Sát Bài Nổ 24/24 & Cảnh Báo 0 Comment.
Chạy độc lập trên cổng 5005, kết nối trực tiếp Telegram Bot & bóc tách view Reels Facebook.
"""

import os
import sys
import json
import logging
import threading
import tempfile
from urllib.parse import urlsplit as _urlsplit
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory

from config import DUONG_DAN
import kiem_tra_bai_no
import cao_fb

PROXIES_FILE = os.path.join(DUONG_DAN, "proxies.txt")
MAX_PROXIES = 50


def _proxy_lines(raw):
    if not isinstance(raw, str) or len(raw) > 16000:
        raise ValueError("Danh sách proxy phải là văn bản tối đa 16.000 ký tự.")
    lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) > MAX_PROXIES:
        raise ValueError(f"Tối đa {MAX_PROXIES} proxy mỗi lần lưu hoặc kiểm tra.")
    for i, line in enumerate(lines, 1):
        if cao_fb.phai_dong_local(line):
            raise ValueError(f"Dòng {i}: để trống danh sách để dùng IP máy, không nhập 'local'.")
        url = cao_fb.chuan_hoa_proxy(line)
        try:
            parsed = urlsplit(url)
            port = parsed.port
            if (parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname
                    or not port or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                    or (parsed.username is None) != (parsed.password is None)):
                raise ValueError("invalid")
        except ValueError:
            raise ValueError(f"Dòng {i}: định dạng proxy không hợp lệ; dùng host:port hoặc host:port:user:pass.") from None
    return lines


def _read_proxy_lines():
    if not os.path.exists(PROXIES_FILE):
        return []
    with open(PROXIES_FILE, "r", encoding="utf-8-sig") as f:
        return [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]


def _proxy_label(raw):
    parsed = urlsplit(cao_fb.chuan_hoa_proxy(raw))
    return f"{parsed.hostname}:{parsed.port}"

app = Flask(__name__, template_folder=os.path.join(DUONG_DAN, "templates"),
            static_folder=os.path.join(DUONG_DAN, "static"))
app.config["JSON_AS_ASCII"] = False
# Allow embedding in the authenticated Orbit dashboard from this local origin.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# This control panel has no user authentication and contains sensitive operations.
# Only accept loopback clients so the app is not exposed to the LAN/Internet.
@app.before_request
def chi_cho_phep_truy_cap_local():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "thong_bao": "Chỉ cho phép truy cập từ máy chạy công cụ."}), 403

@app.after_request
def cho_phep_nhung_trong_orbit(response):
    # Flask 3 defaults to X-Frame-Options: SAMEORIGIN; Orbit runs on port 8080.
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' http://localhost:8080 http://127.0.0.1:8080"
    origin = request.headers.get("Origin", "")
    allowed = {"http://localhost:8080", "http://127.0.0.1:8080"}
    if origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Requested-With"
        response.headers["Vary"] = "Origin"
    return response

# Tắt log spam của Werkzeug
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)


@app.route("/")
def trang_chu():
    return render_template("index.html")


@app.route("/api/bai_no/trang_thai", methods=["GET"])
def api_bai_no_trang_thai():
    st = kiem_tra_bai_no.doc_trang_thai()
    st["log"] = st.get("logs", [])
    resp = jsonify(st)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/bai_no/cau_hinh", methods=["GET", "POST"])
def api_bai_no_cau_hinh():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        cfg = kiem_tra_bai_no.doc_cau_hinh()
        if "telegram_target" in data:
            val = str(data["telegram_target"]).strip()
            if val == "5314274362":
                val = "-5314274362"
            cfg["telegram_target"] = val
        if "nguong_view" in data:
            cfg["nguong_view"] = max(1, int(data["nguong_view"] or 1000))
        if "nguong_like" in data:
            cfg["nguong_like"] = max(1, int(data["nguong_like"] or 10))
        if "so_bai_quet" in data:
            cfg["so_bai_quet"] = max(1, int(data["so_bai_quet"] or 10))
        if "chu_ky_phut" in data:
            cfg["chu_ky_phut"] = max(1, int(data["chu_ky_phut"] or 5))
        if "song_song" in data:
            cfg["song_song"] = max(1, min(6, int(data["song_song"] or 3)))
        if "masters_kich_hoat" in data:
            cfg["masters_kich_hoat"] = data["masters_kich_hoat"]
        if "danh_sach_profile" in data:
            cfg["danh_sach_profile"] = data["danh_sach_profile"]
        if "link_xac_minh_cmt" in data:
            link = str(data["link_xac_minh_cmt"] or "").strip()
            if link and not kiem_tra_bai_no._ten_mien_link(link):
                return jsonify({"ok": False, "thong_bao": "Link xác minh phải là URL http(s):// có tên miền hợp lệ."}), 400
            cfg["link_xac_minh_cmt"] = link
        kiem_tra_bai_no.luu_cau_hinh(cfg)
        if "danh_sach_profile" in data and kiem_tra_bai_no.GIAM_SAT_THREAD and kiem_tra_bai_no.GIAM_SAT_THREAD.is_alive():
            kiem_tra_bai_no.DOI_DANH_SACH_EVENT.set()
        return jsonify({"ok": True, "thong_bao": "Đã lưu cấu hình giám sát bài nổ!", "cau_hinh": cfg})

    cfg = kiem_tra_bai_no.doc_cau_hinh()
    kq_3m = kiem_tra_bai_no.nap_danh_sach_profile_tu_3_master(cfg.get("masters_kich_hoat"))
    cfg["so_luong_theo_master"] = kq_3m.get("so_luong_theo_master", {})
    return jsonify(cfg)


@app.route("/api/bai_no/proxies", methods=["GET", "POST"])
def api_bai_no_proxies():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "thong_bao": "Cấu hình proxy chỉ truy cập được từ máy cục bộ."}), 403
    if request.method == "GET":
        resp = jsonify({"ok": True, "proxies": "\n".join(_read_proxy_lines())})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    data = request.get_json(silent=True) or {}
    try:
        lines = _proxy_lines(data.get("proxies"))
    except ValueError as e:
        return jsonify({"ok": False, "thong_bao": str(e)}), 400
    # Thay file nguyên tử: worker đang quét sẽ dùng cấu hình cũ tới vòng kế tiếp.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=DUONG_DAN,
                                         prefix=".proxies-", suffix=".tmp", delete=False) as f:
            temp_path = f.name
            f.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(temp_path, PROXIES_FILE)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    kiem_tra_bai_no.cap_nhat_trang_thai(log_moi=f"🔒 Đã lưu {len(lines)} proxy (không ghi tài khoản vào log); áp dụng ở vòng quét tiếp theo.")
    return jsonify({"ok": True, "so_luong": len(lines),
                    "thong_bao": f"Đã lưu {len(lines)} proxy. Áp dụng từ vòng quét tiếp theo; để trống dùng IP máy."})


@app.route("/api/bai_no/proxies/kiem_tra", methods=["POST"])
def api_bai_no_kiem_tra_proxies():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "thong_bao": "Chỉ kiểm tra proxy từ máy cục bộ."}), 403
    data = request.get_json(silent=True) or {}
    try:
        lines = _proxy_lines(data.get("proxies"))
    except ValueError as e:
        return jsonify({"ok": False, "thong_bao": str(e)}), 400
    if not lines:
        return jsonify({"ok": False, "thong_bao": "Chưa nhập proxy; để trống sẽ dùng IP máy."}), 400

    def check_one(item):
        index, line = item
        try:
            import requests
            px = cao_fb.chuan_hoa_proxy(line)
            r = requests.get("https://www.facebook.com/", proxies={"http": px, "https": px},
                             timeout=6, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
            ok = r.status_code in (200, 301, 302, 403)
            return {"dong": index, "proxy": _proxy_label(line), "ok": ok,
                    "thong_bao": f"Facebook trả HTTP {r.status_code}"}
        except Exception:
            return {"dong": index, "proxy": _proxy_label(line), "ok": False,
                    "thong_bao": "Không kết nối được tới Facebook trong 6 giây (kiểm tra IP, cổng hoặc tài khoản)."}

    with ThreadPoolExecutor(max_workers=min(5, len(lines))) as pool:
        results = list(pool.map(check_one, enumerate(lines, 1)))
    return jsonify({"ok": True, "ket_qua": results, "so_song": sum(x["ok"] for x in results),
                    "thong_bao": "Thử kết nối HTTPS tới Facebook; không bảo đảm Playwright đọc được bài."})


@app.route("/api/bai_no/bat_dau", methods=["POST"])
def api_bai_no_bat_dau():
    ok, msg = kiem_tra_bai_no.bat_dau_giam_sat()
    return jsonify({"ok": ok, "thong_bao": msg})


@app.route("/api/bai_no/dung", methods=["POST"])
def api_bai_no_dung():
    ok, msg = kiem_tra_bai_no.dung_giam_sat()
    return jsonify({"ok": ok, "thong_bao": msg})


@app.route("/api/bai_no/quet_ngay", methods=["POST"])
def api_bai_no_quet_ngay():
    ok, msg = kiem_tra_bai_no.quet_thu_cong_ngay()
    return jsonify({"ok": ok, "thong_bao": msg})


@app.route("/api/bai_no/gui_test", methods=["POST"])
def api_bai_no_gui_test():
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if not target:
        cfg = kiem_tra_bai_no.doc_cau_hinh()
        target = cfg.get("telegram_target", "").strip()
    if target == "5314274362":
        target = "-5314274362"
    if not target:
        return jsonify({"ok": False, "thong_bao": "Vui lòng nhập Telegram Target (Chat ID / Group ID)"})
    test_msg = (
        "🔔 [TEST KẾT NỐI TELEGRAM BOT]\n"
        "✅ Hệ thống Giám Sát Bài Nổ 24/24 (Bản Chạy Riêng) đã kết nối thành công!\n"
        f"⏰ Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    ok, err = kiem_tra_bai_no.gui_thong_bao_telegram(target, test_msg)
    if ok and target.startswith("-"):
        cfg = kiem_tra_bai_no.doc_cau_hinh()
        if cfg.get("telegram_target") != target:
            cfg["telegram_target"] = target
            kiem_tra_bai_no.luu_cau_hinh(cfg)
    return jsonify({"ok": ok, "thong_bao": err if ok else f"Gửi thất bại: {err}", "target_chuan": target if ok else None})


@app.route("/api/bai_no/nap_tu_master", methods=["POST"])
def api_bai_no_nap_tu_master():
    data = request.get_json(silent=True) or {}
    cfg = kiem_tra_bai_no.doc_cau_hinh()
    masters_kich_hoat = data.get("masters_kich_hoat") or cfg.get("masters_kich_hoat", {"us": True, "us2": True, "global": True})
    cfg["masters_kich_hoat"] = masters_kich_hoat
    kq = kiem_tra_bai_no.nap_danh_sach_profile_tu_3_master(masters_kich_hoat)
    ds = kq.get("danh_sach_kich_hoat", [])
    cfg["danh_sach_profile"] = ds
    kiem_tra_bai_no.luu_cau_hinh(cfg)
    so_us = kq["so_luong_theo_master"].get("us", 0)
    so_us2 = kq["so_luong_theo_master"].get("us2", 0)
    so_glo = kq["so_luong_theo_master"].get("global", 0)
    return jsonify({
        "ok": True,
        "so_luong": len(ds),
        "so_luong_theo_master": kq["so_luong_theo_master"],
        "masters_kich_hoat": masters_kich_hoat,
        "danh_sach": ds,
        "thong_bao": f"Đã nạp {len(ds)} profile từ 3 Master (US: {so_us}, US 2: {so_us2}, Global: {so_glo})!"
    })


@app.route("/api/bai_no/lich_su", methods=["GET"])
def api_bai_no_lich_su():
    """Chỉ gửi một trang cảnh báo; không trả toàn bộ lịch sử mỗi lần làm mới."""
    try:
        page = int(request.args.get("page", "1"))
        per_page = int(request.args.get("per_page", "20"))
    except ValueError:
        return jsonify({"ok": False, "thong_bao": "Trang hoặc số bài/trang không hợp lệ"}), 400
    if page < 1 or page > 100000 or per_page not in (10, 20, 50):
        return jsonify({"ok": False, "thong_bao": "Chỉ hỗ trợ 10, 20 hoặc 50 bài/trang"}), 400
    loai = request.args.get("filter", "tat_ca")
    if loai not in ("tat_ca", "can_cmt", "da_ok"):
        return jsonify({"ok": False, "thong_bao": "Bộ lọc không hợp lệ"}), 400
    # Optional profile/page filter, matched against stored profile name, page name or link.
    profile_filter = request.args.get("profile", "").strip()
    if len(profile_filter) > 300:
        return jsonify({"ok": False, "thong_bao": "Bộ lọc Page không hợp lệ"}), 400
    all_items = kiem_tra_bai_no.doc_lich_su().get("danh_sach", [])
    profile_options = {}
    for item in all_items:
        label = str(item.get("ten_page") or item.get("profile") or "").strip()
        if label:
            key = str(item.get("link_profile") or label).strip()
            profile_options.setdefault(key, label)
    items = all_items
    if profile_filter:
        items = [it for it in items if (str(it.get("link_profile") or "").strip() == profile_filter
                 if profile_filter.startswith("http") else
                 str(it.get("ten_page") or it.get("profile") or "").strip() == profile_filter)]
    can_cmt = lambda it: not it.get("da_gan_cmt") and (it.get("comments") == 0 or it.get("loai") == "BAI_NO_CHUA_LINK")
    counts = {"tat_ca": len(items), "can_cmt": sum(1 for it in items if can_cmt(it)),
              "da_ok": sum(1 for it in items if it.get("da_gan_cmt"))}
    if loai == "can_cmt":
        items = [it for it in items if can_cmt(it)]
    elif loai == "da_ok":
        items = [it for it in items if it.get("da_gan_cmt")]
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    resp = jsonify({"ok": True, "danh_sach": items[start:start + per_page], "total": total,
                    "counts": counts, "profiles": [{"key": key, "label": label} for key, label in sorted(profile_options.items(), key=lambda pair: pair[1].casefold())],
                    "page": page, "per_page": per_page, "total_pages": total_pages})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/bai_no/danh_dau_ok", methods=["POST"])
def api_bai_no_danh_dau_ok():
    data = request.get_json(force=True, silent=True) or {}
    identifier = data.get("id") or data.get("link_fb") or data.get("post_id") or ""
    da_gan_cmt = bool(data.get("da_gan_cmt", True))
    if not identifier:
        return jsonify({"ok": False, "thong_bao": "Thiếu mã định danh bài viết (id/post_id/link_fb)"}), 400
    ok, msg = kiem_tra_bai_no.danh_dau_bai_no_ok(identifier, da_gan_cmt=da_gan_cmt)
    return jsonify({"ok": ok, "thong_bao": msg, "da_gan_cmt": da_gan_cmt})


@app.route("/api/media_proxy")
def media_proxy():
    path = request.args.get("path")
    if not path or not os.path.exists(path):
        return "", 404
    return send_file(path)


if __name__ == "__main__":
    print("=" * 65)
    print("🚀 HỆ THỐNG GIÁM SÁT BÀI NỔ 24/24 (BẢN CHẠY RIÊNG ĐỘC LẬP)")
    print("👉 Mở trình duyệt WebUI tại: http://127.0.0.1:5005")
    print("🛡️ Quét view Reels & Timeline, tự phát hiện bài nổ và báo Telegram")
    print("=" * 65)

    app.run(host="127.0.0.1", port=5005, debug=False, threaded=True)
