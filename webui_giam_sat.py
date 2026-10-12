"""WebUI Độc Lập cho Hệ Thống Giám Sát Bài Nổ 24/24 & Cảnh Báo 0 Comment.
Chạy độc lập trên cổng 5005, kết nối trực tiếp Telegram Bot & bóc tách view Reels Facebook.
"""

import os
import sys
import json
import logging
import threading
import webbrowser
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory

from config import DUONG_DAN
import kiem_tra_bai_no

app = Flask(__name__, template_folder=os.path.join(DUONG_DAN, "templates"),
            static_folder=os.path.join(DUONG_DAN, "static"))
app.config["JSON_AS_ASCII"] = False

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
        kiem_tra_bai_no.luu_cau_hinh(cfg)
        return jsonify({"ok": True, "thong_bao": "Đã lưu cấu hình giám sát bài nổ!", "cau_hinh": cfg})

    cfg = kiem_tra_bai_no.doc_cau_hinh()
    kq_3m = kiem_tra_bai_no.nap_danh_sach_profile_tu_3_master(cfg.get("masters_kich_hoat"))
    cfg["so_luong_theo_master"] = kq_3m.get("so_luong_theo_master", {})
    return jsonify(cfg)


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
    resp = jsonify(kiem_tra_bai_no.doc_lich_su())
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
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


def mo_trinh_duyet():
    import time
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5005")


if __name__ == "__main__":
    print("=" * 65)
    print("🚀 HỆ THỐNG GIÁM SÁT BÀI NỔ 24/24 (BẢN CHẠY RIÊNG ĐỘC LẬP)")
    print("👉 Mở trình duyệt WebUI tại: http://127.0.0.1:5005")
    print("🛡️ Quét view Reels & Timeline, tự phát hiện bài nổ và báo Telegram")
    print("=" * 65)

    threading.Thread(target=mo_trinh_duyet, daemon=True).start()
    app.run(host="0.0.0.0", port=5005, debug=False, threaded=True)
