"""Module Giám Sát Bài Nổ 24/24 & Cảnh Báo 0 Cmt (Chưa Gắn Link Báo)
Tích hợp gửi thông báo qua OpenClaw về Telegram Group/Chat.
Hỗ trợ quét đa luồng song song theo Proxy, trích xuất chính xác URL bài viết thật,
chống báo rác và tự động khớp link báo tương ứng từ Master & Content Pool.
"""

import os
import re
import json
import time
import queue
import hashlib
import threading
import subprocess
import concurrent.futures
from datetime import datetime
import openpyxl

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

from playwright.sync_api import sync_playwright

import cao_fb

from config import DUONG_DAN
THU_MUC_HIENTAI = DUONG_DAN
THU_MUC_TRAFFIC = os.path.join(THU_MUC_HIENTAI, "du_lieu_traffic")
FILE_CAU_HINH = os.path.join(THU_MUC_TRAFFIC, "cau_hinh_bai_no.json")
FILE_TRANG_THAI = os.path.join(THU_MUC_TRAFFIC, "trang_thai_bai_no.json")
FILE_LICH_SU = os.path.join(THU_MUC_TRAFFIC, "lich_su_bai_no.json")

KHOA = threading.RLock()
STOP_EVENT = threading.Event()
GIAM_SAT_THREAD = None


def _tao_fingerprints_bai(ten_prof: str, post_id: str, post_url: str, caption: str) -> list[str]:
    """Tạo tập khóa nhận diện duy nhất cho 1 bài viết để CHỐNG SPAM / THÔNG BÁO LẶP LẠI (bài chỉ báo 1 lần)."""
    keys = []
    p_id_str = str(post_id or "").strip()
    if p_id_str and not p_id_str.startswith("http"):
        keys.append(f"id:{p_id_str}")
        keys.append(p_id_str)

    p_url_str = str(post_url or "").strip()
    if p_url_str and ("posts/" in p_url_str or "story_fbid=" in p_url_str or "reel/" in p_url_str or "permalink" in p_url_str or "fbid=" in p_url_str):
        keys.append(f"url:{p_url_str}")
        clean_url = p_url_str.split("?")[0].rstrip("/")
        keys.append(f"url:{clean_url}")

    # Khóa kết hợp: Profile + 10 từ đầu caption (bền vững, không phụ thuộc URL động của Facebook)
    muoi_tu = lay_10_tu_dau(caption).strip().lower()
    p_clean = str(ten_prof or "").strip().lower()
    if muoi_tu and len(muoi_tu) >= 10:
        keys.append(f"txt:{p_clean}::{muoi_tu}")
        h = hashlib.md5(f"{p_clean}::{muoi_tu}".encode()).hexdigest()[:16]
        keys.append(f"hash:{h}")
    elif p_id_str:
        keys.append(f"txt:{p_clean}::{p_id_str}")

    return list(set(keys))


def _lay_reel_id_chuan(u: str) -> str:
    """Trích xuất ID chuẩn từ link bài viết Facebook (Reel / Post / Permalink) hoặc chuỗi ID."""
    if not u:
        return ""
    u_str = str(u).strip()
    for prefix in ["id:", "url:", "txt:", "hash:"]:
        if u_str.startswith(prefix):
            u_str = u_str[len(prefix):].strip()
    m = re.search(r"/reel/(\d+)", u_str)
    if m:
        return m.group(1)
    m = re.search(r"/posts/(\d+)", u_str)
    if m:
        return m.group(1)
    m = re.search(r"[?&](?:story_fbid|fbid)=(\d+)", u_str)
    if m:
        return m.group(1)
    if u_str.isdigit() and len(u_str) >= 8:
        return u_str
    return ""


def kiem_tra_da_danh_dau_ok(fps: list[str] = None, link_fb: str = "", post_id: str = "") -> bool:
    """Kiểm tra bài viết đã được người dùng đánh dấu OK (đã gắn cmt) hay chưa.
    Nếu đã đánh dấu OK, hệ thống sẽ KHÔNG BAO GIỜ gửi thông báo bài này nữa."""
    ls = doc_lich_su()
    da_ok = set(ls.get("da_danh_dau_ok", []))
    if not da_ok:
        return False

    candidates = set()
    if post_id:
        p_str = str(post_id).strip()
        candidates.add(p_str)
        candidates.add(f"id:{p_str}")
        rid_p = _lay_reel_id_chuan(p_str)
        if rid_p:
            candidates.add(rid_p)
            candidates.add(f"id:{rid_p}")

    if link_fb:
        l_str = str(link_fb).strip()
        candidates.add(l_str)
        candidates.add(f"url:{l_str}")
        rid_l = _lay_reel_id_chuan(l_str)
        if rid_l:
            candidates.add(rid_l)
            candidates.add(f"id:{rid_l}")

    if fps:
        for fp in fps:
            fp_str = str(fp).strip()
            candidates.add(fp_str)
            rid_fp = _lay_reel_id_chuan(fp_str)
            if rid_fp:
                candidates.add(rid_fp)
                candidates.add(f"id:{rid_fp}")

    if any(c in da_ok for c in candidates if c):
        return True

    for ok_item in da_ok:
        rid_ok = _lay_reel_id_chuan(ok_item)
        if rid_ok and (rid_ok in candidates or (link_fb and rid_ok in str(link_fb)) or (post_id and rid_ok == str(post_id))):
            return True

    return False


def _da_tung_bao(fps: list[str], tap_hop_da_bao) -> bool:
    """Kiểm tra bài viết đã từng được gửi cảnh báo trước đó chưa."""
    for k in fps:
        if k in tap_hop_da_bao:
            return True
    return False


def kiem_tra_can_bao_no(fps: list[str], current_metric: int, da_bao_metric: dict, buoc_tang: int = 500) -> tuple[bool, int, int]:
    """Kiểm tra xem bài viết có cần gửi thông báo bài nổ hay không (giám sát theo View / Like):
    - Lần đầu tiên (chưa từng báo): (True, 0, current_metric)
    - Đã từng báo:
        + Nếu current_metric - max(metric_cu) >= buoc_tang: (True, max(metric_cu), current_metric - max(metric_cu))
        + Nếu chưa tăng đủ buoc_tang (mặc định 500 view): (False, max(metric_cu), 0)
    """
    vals_da_bao = [da_bao_metric[k] for k in fps if k in da_bao_metric]
    if not vals_da_bao:
        return True, 0, current_metric

    max_cu = max(vals_da_bao)
    if current_metric - max_cu >= buoc_tang:
        return True, max_cu, current_metric - max_cu

    return False, max_cu, 0


# ==========================================
# CẤU HÌNH & TRẠNG THÁI
# ==========================================

def doc_cau_hinh() -> dict:
    os.makedirs(THU_MUC_TRAFFIC, exist_ok=True)
    if os.path.exists(FILE_CAU_HINH):
        try:
            with open(FILE_CAU_HINH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if "masters_kich_hoat" not in cfg:
                    cfg["masters_kich_hoat"] = {"us": True, "us2": True, "global": True}
                if "nguong_view" not in cfg:
                    cfg["nguong_view"] = 1000
                return cfg
        except Exception:
            pass
    mac_dinh = {
        "telegram_target": "-5314274362",
        "nguong_view": 1000,
        "nguong_like": 10,
        "so_bai_quet": 6,
        "chu_ky_phut": 5,
        "song_song": 3,
        "masters_kich_hoat": {
            "us": True,
            "us2": True,
            "global": True
        },
        "danh_sach_profile": [],
    }
    luu_cau_hinh(mac_dinh)
    return mac_dinh


def luu_cau_hinh(cfg: dict):
    os.makedirs(THU_MUC_TRAFFIC, exist_ok=True)
    with KHOA:
        with open(FILE_CAU_HINH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


def doc_trang_thai() -> dict:
    st = {}
    if os.path.exists(FILE_TRANG_THAI):
        try:
            with open(FILE_TRANG_THAI, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
            
    # Đồng bộ trạng thái thực tế của luồng ngầm
    is_alive = bool(GIAM_SAT_THREAD and GIAM_SAT_THREAD.is_alive())
    if not is_alive:
        st["dang_chay"] = False
        
    return {
        "dang_chay": st.get("dang_chay", False),
        "lan_quet_cuoi": st.get("lan_quet_cuoi"),
        "so_profile": st.get("so_profile", 0),
        "so_bai_no": st.get("so_bai_no", 0),
        "so_bai_no_co_link": st.get("so_bai_no_co_link", 0),
        "so_bai_no_chua_link": st.get("so_bai_no_chua_link", 0),
        "so_bai_chua_cmt": st.get("so_bai_no_chua_link", st.get("so_bai_chua_cmt", 0)),
        "tien_do": st.get("tien_do", "Chưa kích hoạt"),
        "logs": st.get("logs", []),
    }


def cap_nhat_trang_thai(**kwargs):
    os.makedirs(THU_MUC_TRAFFIC, exist_ok=True)
    with KHOA:
        st = doc_trang_thai()
        for k, v in kwargs.items():
            if k == "log_moi" and v:
                logs = st.get("logs", [])
                gio = datetime.now().strftime("%H:%M:%S")
                logs.append(f"[{gio}] {v}")
                st["logs"] = logs[-300:]  # lưu tối đa 300 dòng log gần nhất
            else:
                st[k] = v
        try:
            with open(FILE_TRANG_THAI, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def doc_lich_su() -> dict:
    os.makedirs(THU_MUC_TRAFFIC, exist_ok=True)
    if os.path.exists(FILE_LICH_SU):
        try:
            with open(FILE_LICH_SU, "r", encoding="utf-8") as f:
                ls = json.load(f)
        except Exception:
            ls = {"da_bao_no": [], "da_bao_no_views": {}, "da_bao_no_likes": {}, "da_danh_dau_ok": [], "danh_sach": []}
    else:
        ls = {"da_bao_no": [], "da_bao_no_views": {}, "da_bao_no_likes": {}, "da_danh_dau_ok": [], "danh_sach": []}

    if not isinstance(ls, dict):
        ls = {"da_bao_no": [], "da_bao_no_views": {}, "da_bao_no_likes": {}, "da_danh_dau_ok": [], "danh_sach": []}

    da_bao_views = ls.setdefault("da_bao_no_views", {})
    if not isinstance(da_bao_views, dict):
        da_bao_views = {}
        ls["da_bao_no_views"] = da_bao_views

    da_bao_likes = ls.setdefault("da_bao_no_likes", {})
    if not isinstance(da_bao_likes, dict):
        da_bao_likes = {}
        ls["da_bao_no_likes"] = da_bao_likes

    da_danh_dau_ok = ls.setdefault("da_danh_dau_ok", [])
    if not isinstance(da_danh_dau_ok, list):
        da_danh_dau_ok = list(da_danh_dau_ok) if isinstance(da_danh_dau_ok, (set, dict)) else []
        ls["da_danh_dau_ok"] = da_danh_dau_ok
    set_da_ok = set(da_danh_dau_ok)

    danh_sach_loc = []
    for item in ls.get("danh_sach", []):
        views = int(item.get("views") or 0)
        likes = int(item.get("likes") or 0)

        # Đồng bộ cờ da_gan_cmt
        p_id = str(item.get("post_id", "")).strip()
        l_fb = str(item.get("link_fb", "")).strip()
        item_rid = _lay_reel_id_chuan(l_fb)
        item_fps = item.get("fingerprints", [])

        la_da_ok = (
            item.get("da_gan_cmt") is True
            or p_id in set_da_ok
            or f"id:{p_id}" in set_da_ok
            or l_fb in set_da_ok
            or f"url:{l_fb}" in set_da_ok
            or (item_rid and (item_rid in set_da_ok or f"id:{item_rid}" in set_da_ok))
            or any(fp in set_da_ok for fp in item_fps)
        )
        item["da_gan_cmt"] = la_da_ok
        danh_sach_loc.append(item)

        fps = item.get("fingerprints") or _tao_fingerprints_bai(
            item.get("profile", ""), item.get("post_id", ""), item.get("link_fb", ""), item.get("muoi_tu_dau", "") or item.get("caption_dau", "")
        )
        for fp in fps:
            if views > 0:
                da_bao_views[fp] = max(da_bao_views.get(fp, 0), views)
            if likes > 0:
                da_bao_likes[fp] = max(da_bao_likes.get(fp, 0), likes)

    ls["da_bao_no"] = list(da_bao_views.keys()) if da_bao_views else list(da_bao_likes.keys())
    ls["da_bao_no_views"] = da_bao_views
    ls["da_bao_no_likes"] = da_bao_likes
    ls["da_danh_dau_ok"] = da_danh_dau_ok
    ls["danh_sach"] = danh_sach_loc
    return ls


def ghi_lich_su_canh_bao(item: dict):
    os.makedirs(THU_MUC_TRAFFIC, exist_ok=True)
    with KHOA:
        ls = doc_lich_su()

        # Tự động đồng bộ trạng thái da_gan_cmt nếu bài đã từng được đánh dấu OK
        da_ok = set(ls.get("da_danh_dau_ok", []))
        item_id = str(item.get("post_id", "")).strip()
        item_url = str(item.get("link_fb", "")).strip()
        item_rid = _lay_reel_id_chuan(item_url)
        fps = item.get("fingerprints") or _tao_fingerprints_bai(
            item.get("profile", ""), item.get("post_id", ""), item.get("link_fb", ""), item.get("muoi_tu_dau", "") or item.get("caption_dau", "")
        )
        la_da_ok = (
            item.get("da_gan_cmt") is True
            or item_id in da_ok
            or f"id:{item_id}" in da_ok
            or item_url in da_ok
            or f"url:{item_url}" in da_ok
            or (item_rid and (item_rid in da_ok or f"id:{item_rid}" in da_ok))
            or any(fp in da_ok for fp in fps)
        )
        item["da_gan_cmt"] = la_da_ok

        ls["danh_sach"].insert(0, item)
        ls["danh_sach"] = ls["danh_sach"][:300]  # giữ 300 cảnh báo gần nhất
        current_views = int(item.get("views") or 0)
        current_likes = int(item.get("likes") or 0)
        da_bao_views = ls.setdefault("da_bao_no_views", {})
        da_bao_likes = ls.setdefault("da_bao_no_likes", {})
        for fp in fps:
            if current_views > 0:
                da_bao_views[fp] = current_views
            if current_likes > 0:
                da_bao_likes[fp] = current_likes
        ls["da_bao_no"] = list(da_bao_views.keys())

        with open(FILE_LICH_SU, "w", encoding="utf-8") as f:
            json.dump(ls, f, ensure_ascii=False, indent=2)


def danh_dau_bai_no_ok(identifier: str, da_gan_cmt: bool = True) -> tuple[bool, str]:
    """Đánh dấu hoặc hủy đánh dấu OK (đã gắn cmt) cho một bài nổ.
    Khi đã đánh dấu OK (da_gan_cmt=True), bot giám sát sẽ KHÔNG BAO GIỜ gửi thông báo bài này nữa."""
    if not identifier:
        return False, "Thiếu định danh bài viết."

    identifier = str(identifier).strip()
    rid = _lay_reel_id_chuan(identifier)

    with KHOA:
        ls = doc_lich_su()
        da_ok = set(ls.get("da_danh_dau_ok", []))

        keys_to_affect = {identifier}
        if rid:
            keys_to_affect.add(rid)
            keys_to_affect.add(f"id:{rid}")

        so_khop = 0
        for item in ls.get("danh_sach", []):
            p_id = str(item.get("post_id", "")).strip()
            l_fb = str(item.get("link_fb", "")).strip()
            item_fps = item.get("fingerprints", [])
            item_rid = _lay_reel_id_chuan(l_fb)

            la_khop = (
                identifier == p_id
                or identifier == l_fb
                or identifier in item_fps
                or (rid and (rid == item_rid or rid == p_id))
            )

            if la_khop:
                so_khop += 1
                item["da_gan_cmt"] = da_gan_cmt
                if p_id:
                    keys_to_affect.add(p_id)
                    keys_to_affect.add(f"id:{p_id}")
                if l_fb:
                    keys_to_affect.add(l_fb)
                    keys_to_affect.add(f"url:{l_fb}")
                if item_rid:
                    keys_to_affect.add(item_rid)
                    keys_to_affect.add(f"id:{item_rid}")
                for fp in item_fps:
                    keys_to_affect.add(fp)

        clean_keys = {k for k in keys_to_affect if k}
        if da_gan_cmt:
            da_ok.update(clean_keys)
        else:
            da_ok.difference_update(clean_keys)

        ls["da_danh_dau_ok"] = sorted(list(da_ok))

        so_chua_cmt = sum(1 for it in ls.get("danh_sach", []) if not it.get("da_gan_cmt") and it.get("comments", 0) == 0)

        try:
            with open(FILE_LICH_SU, "w", encoding="utf-8") as f:
                json.dump(ls, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return False, f"Lỗi lưu file lịch sử: {e}"

        cap_nhat_trang_thai(
            so_bai_no_chua_link=so_chua_cmt,
            so_bai_chua_cmt=so_chua_cmt,
            log_moi=f"✅ Đã đánh dấu OK (đã gắn cmt) bài {identifier} -> Không thông báo lại bài này nữa." if da_gan_cmt else f"ℹ️ Đã hủy đánh dấu OK bài {identifier}."
        )

        msg = "Đã đánh dấu OK (đã gắn cmt) thành công! Hệ thống sẽ không thông báo lại bài này nữa." if da_gan_cmt else "Đã hủy đánh dấu OK."
        return True, msg


# ==========================================
# GỬI THÔNG BÁO QUA TELEGRAM BOT API CHÍNH THỨC
# (100% Python Native, không phụ thuộc OpenClaw / Node.js)
# ==========================================

TELEGRAM_BOT_TOKEN_MAC_DINH = "8982186515:AAElroJOTRdcgupZ4LVOgTl6hAUi-lZaU_I"


def lay_telegram_bot_token() -> str:
    """Lấy botToken từ cau_hinh_bai_no.json hoặc config.json hoặc token mặc định."""
    cfg = doc_cau_hinh()
    tok = cfg.get("telegram_bot_token", "").strip()
    if tok:
        return tok

    # Thử đọc từ config.json
    cfg_file = os.path.join(THU_MUC_HIENTAI, "config.json")
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                c = json.load(f)
            tok_c = c.get("telegram_bot_token", "").strip()
            if tok_c:
                return tok_c
        except Exception:
            pass

    return TELEGRAM_BOT_TOKEN_MAC_DINH


def gui_thong_bao_telegram(target: str, noi_dung: str) -> tuple[bool, str]:
    """Gửi trực tiếp qua Telegram Bot API chính thức.
    Hỗ trợ cả Chat ID cá nhân và Group ID (tự động nhận diện dấu trừ '-').
    Hoạt động 100% độc lập, không cần Node.js hay OpenClaw.
    """
    if not target or not str(target).strip():
        return False, "Chưa cấu hình Telegram Target (Chat ID / Group ID)"
    target = str(target).strip()
    if target == "5314274362":
        target = "-5314274362"

    token = lay_telegram_bot_token()
    if not token:
        return False, "Chưa có Telegram botToken"

    import urllib.request
    import urllib.parse

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Thử gửi với target hiện tại
    for tgt in [target, f"-{target}" if not target.startswith("-") else target]:
        payload = {
            "chat_id": tgt,
            "text": noi_dung,
            "disable_web_page_preview": False
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                res_data = json.loads(res.read().decode("utf-8"))
                if res_data.get("ok"):
                    msg_id = res_data.get("result", {}).get("message_id")
                    return True, f"Gửi Telegram thành công (Msg ID: {msg_id})"
                err_desc = res_data.get("description", "Lỗi không xác định")
                if "chat not found" in err_desc.lower() and tgt == target and not target.startswith("-"):
                    continue
                return False, f"Telegram API lỗi: {err_desc}"
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
                err_desc = err_body.get("description", str(e.reason))
                if "chat not found" in err_desc.lower() and tgt == target and not target.startswith("-"):
                    continue
                return False, f"Telegram HTTP {e.code}: {err_desc}"
            except Exception:
                return False, f"Telegram HTTP {e.code}: {e.reason}"
        except Exception as e:
            return False, f"Lỗi kết nối Telegram: {e}"

    return False, "Không thể gửi tin nhắn Telegram: Chat not found"


# Alias tương thích ngược
gui_thong_bao_openclaw = gui_thong_bao_telegram
gui_thong_bao_telegram_truc_tiep = gui_thong_bao_telegram


# ==========================================
# NẠP MASTER & ĐỐI SOÁT LINK BÁO
# ==========================================

def _lam_sach_tu(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").lower())


def _do_tuong_dong_caption(c1: str, c2: str) -> float:
    w1 = set(_lam_sach_tu(c1)[:18])
    w2 = set(_lam_sach_tu(c2)[:18])
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / max(len(w1), len(w2))


def lay_10_tu_dau(caption: str) -> str:
    if not caption:
        return "(Không có caption)"
    cac_tu = caption.strip().split()
    if len(cac_tu) <= 10:
        return " ".join(cac_tu)
    return " ".join(cac_tu[:10]) + "..."


_CACHE_MASTER = None
_CACHE_POOL = None
_CACHE_TIME = 0

def nap_master_cache(force=False):
    global _CACHE_MASTER, _CACHE_POOL, _CACHE_TIME
    now = time.time()
    if not force and _CACHE_MASTER is not None and (now - _CACHE_TIME < 600):
        return _CACHE_MASTER, _CACHE_POOL

    master_rows = []
    # 1. Master US, US 2 & Global
    for fn, vung in [("du_lieu_exel/MASTER_DANG_BAI_US.xlsx", "us"),
                     ("du_lieu_exel/MASTER_DANG_BAI_US_2.xlsx", "us2"),
                     ("du_lieu_exel/MASTER_DANG_BAI_GLOBAL.xlsx", "global")]:
        full_fn = os.path.join(THU_MUC_HIENTAI, fn)
        if not os.path.exists(full_fn):
            continue
        try:
            wb = openpyxl.load_workbook(full_fn, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if rows:
                headers = rows[0]
                for r in rows[1:]:
                    d = dict(zip(headers, r))
                    prof = str(d.get("PROFILE") or "").strip()
                    if prof and prof.upper() != "PROFILE":
                        master_rows.append({
                            "profile": prof,
                            "nguon": str(d.get("Nguồn") or "").strip(),
                            "ten_page": str(d.get("Tên Page") or "").strip(),
                            "caption_moi": str(d.get("Caption mới") or "").strip(),
                            "caption_goc": str(d.get("Caption gốc") or "").strip(),
                            "link_bao": str(d.get("Link bài báo") or "").strip(),
                            "content_id": str(d.get("Content ID") or "").strip(),
                            "vung": vung,
                        })
            wb.close()
        except Exception:
            pass

    # 2. Content Pool
    pool_file = os.path.join(THU_MUC_TRAFFIC, "content_pool.json")
    pool = []
    if os.path.exists(pool_file):
        try:
            with open(pool_file, "r", encoding="utf-8") as f:
                pool = json.load(f)
        except Exception:
            pass

    _CACHE_MASTER = master_rows
    _CACHE_POOL = pool
    _CACHE_TIME = now
    return master_rows, pool


def doc_profiles_tu_master_va_uid(master_key: str) -> list[dict]:
    """Đọc danh sách profile Live cho 1 master cụ thể: us, us2, hoặc global."""
    res = []
    seen = set()

    if master_key == "us":
        fp_uid = os.path.join(THU_MUC_HIENTAI, "ngoài tool", "danh_sach_uid.xlsx")
        if os.path.exists(fp_uid):
            try:
                wb = openpyxl.load_workbook(fp_uid, data_only=True)
                ws = wb.active
                for r in range(2, ws.max_row + 1):
                    st = str(ws.cell(r, 6).value or "").strip().lower()
                    u = str(ws.cell(r, 2).value or "").strip()
                    prof = str(ws.cell(r, 1).value or "").strip()
                    ten = str(ws.cell(r, 3).value or "").strip()
                    if st == "live" and u.startswith("http") and u.lower() not in seen:
                        seen.add(u.lower())
                        res.append({"url": u, "profile": prof, "ten_page": ten, "master": "us"})
                wb.close()
            except Exception:
                pass
        if not res:
            fp_master = os.path.join(THU_MUC_HIENTAI, "du_lieu_exel", "MASTER_DANG_BAI_US.xlsx")
            if os.path.exists(fp_master):
                try:
                    wb = openpyxl.load_workbook(fp_master, data_only=True)
                    ws = wb.active
                    rows = list(ws.iter_rows(values_only=True))
                    if rows:
                        headers = [str(h or "") for h in rows[0]]
                        for r in rows[1:]:
                            d = dict(zip(headers, r))
                            u = str(d.get("Nguồn") or "").strip()
                            prof = str(d.get("PROFILE") or "").strip()
                            ten = str(d.get("Tên Page") or "").strip()
                            cap = str(d.get("Caption mới") or "").strip()
                            if u.startswith("http") and cap and u.lower() not in seen:
                                seen.add(u.lower())
                                res.append({"url": u, "profile": prof, "ten_page": ten, "master": "us"})
                    wb.close()
                except Exception:
                    pass

    elif master_key == "us2":
        for fn in ["danh_sach_uid_us_2.xlsx", "danh_sach_uid_2.xlsx"]:
            fp_uid = os.path.join(THU_MUC_HIENTAI, "ngoài tool", fn)
            if os.path.exists(fp_uid):
                try:
                    wb = openpyxl.load_workbook(fp_uid, data_only=True)
                    ws = wb.active
                    for r in range(2, ws.max_row + 1):
                        st = str(ws.cell(r, 6).value or "").strip().lower()
                        u = str(ws.cell(r, 2).value or "").strip()
                        prof = str(ws.cell(r, 1).value or "").strip()
                        ten = str(ws.cell(r, 3).value or "").strip()
                        if st == "live" and u.startswith("http") and u.lower() not in seen:
                            seen.add(u.lower())
                            res.append({"url": u, "profile": prof, "ten_page": ten, "master": "us2"})
                    wb.close()
                    break
                except Exception:
                    pass
        if not res:
            fp_master = os.path.join(THU_MUC_HIENTAI, "du_lieu_exel", "MASTER_DANG_BAI_US_2.xlsx")
            if os.path.exists(fp_master):
                try:
                    wb = openpyxl.load_workbook(fp_master, data_only=True)
                    ws = wb.active
                    rows = list(ws.iter_rows(values_only=True))
                    if rows:
                        headers = [str(h or "") for h in rows[0]]
                        for r in rows[1:]:
                            d = dict(zip(headers, r))
                            u = str(d.get("Nguồn") or "").strip()
                            prof = str(d.get("PROFILE") or "").strip()
                            ten = str(d.get("Tên Page") or "").strip()
                            if u.startswith("http") and u.lower() not in seen:
                                seen.add(u.lower())
                                res.append({"url": u, "profile": prof, "ten_page": ten, "master": "us2"})
                    wb.close()
                except Exception:
                    pass

    elif master_key == "global":
        for fn in ["danh_sach_uid_global.xlsx", "danh_sach_uid - global.xlsx"]:
            fp_uid = os.path.join(THU_MUC_HIENTAI, "ngoài tool", fn)
            if os.path.exists(fp_uid):
                try:
                    wb = openpyxl.load_workbook(fp_uid, data_only=True)
                    ws = wb.active
                    for r in range(2, ws.max_row + 1):
                        st = str(ws.cell(r, 6).value or "live").strip().lower()
                        u = str(ws.cell(r, 2).value or "").strip()
                        prof = str(ws.cell(r, 1).value or "").strip()
                        ten = str(ws.cell(r, 3).value or "").strip()
                        if st == "live" and u.startswith("http") and u.lower() not in seen:
                            seen.add(u.lower())
                            res.append({"url": u, "profile": prof, "ten_page": ten, "master": "global"})
                    wb.close()
                    break
                except Exception:
                    pass
        if not res:
            fp_master = os.path.join(THU_MUC_HIENTAI, "du_lieu_exel", "MASTER_DANG_BAI_GLOBAL.xlsx")
            if os.path.exists(fp_master):
                try:
                    wb = openpyxl.load_workbook(fp_master, data_only=True)
                    ws = wb.active
                    rows = list(ws.iter_rows(values_only=True))
                    if rows:
                        headers = [str(h or "") for h in rows[0]]
                        for r in rows[1:]:
                            d = dict(zip(headers, r))
                            u = str(d.get("Nguồn") or "").strip()
                            prof = str(d.get("PROFILE") or "").strip()
                            ten = str(d.get("Tên Page") or "").strip()
                            if u.startswith("http") and u.lower() not in seen:
                                seen.add(u.lower())
                                res.append({"url": u, "profile": prof, "ten_page": ten, "master": "global"})
                    wb.close()
                except Exception:
                    pass

    return res


def nap_danh_sach_profile_tu_3_master(masters_kich_hoat: dict = None) -> dict:
    """Nạp profile của cả 3 master (us, us2, global) và trả về theo nhóm kèm danh sách kích hoạt."""
    if masters_kich_hoat is None:
        cfg = doc_cau_hinh()
        masters_kich_hoat = cfg.get("masters_kich_hoat", {"us": True, "us2": True, "global": True})

    profs_us = doc_profiles_tu_master_va_uid("us")
    profs_us2 = doc_profiles_tu_master_va_uid("us2")
    profs_global = doc_profiles_tu_master_va_uid("global")

    tat_ca = {
        "us": profs_us,
        "us2": profs_us2,
        "global": profs_global
    }

    ds_kich_hoat = []
    seen = set()
    for m_key in ["us", "us2", "global"]:
        if masters_kich_hoat.get(m_key, True):
            for item in tat_ca.get(m_key, []):
                u = item.get("url", "").lower()
                if u and u not in seen:
                    seen.add(u)
                    ds_kich_hoat.append(item)

    return {
        "so_luong_theo_master": {
            "us": len(profs_us),
            "us2": len(profs_us2),
            "global": len(profs_global),
        },
        "masters_kich_hoat": masters_kich_hoat,
        "danh_sach_kich_hoat": ds_kich_hoat,
        "tat_ca_theo_master": tat_ca
    }


def nap_danh_sach_profile_tu_master() -> list[dict]:
    """Tự động trích xuất danh sách profile độc nhất từ 3 Master: US, US 2 & Global."""
    kq = nap_danh_sach_profile_tu_3_master()
    return kq.get("danh_sach_kich_hoat", [])


def tim_link_bao_tuong_ung(profile: str, caption: str) -> tuple[str | None, str]:
    """Khớp caption bài đăng với Master và Content Pool để tìm link bài báo tương ứng.
    Bắt buộc độ tương đồng tối thiểu >= 0.35 để chặn hoàn toàn việc gán nhầm link cho text rác."""
    if not caption or len(caption.strip()) < 25 or len(caption.strip().split()) < 5:
        return None, "Caption quá ngắn hoặc không hợp lệ"

    master_rows, pool = nap_master_cache()

    # 1. Ưu tiên trong Master cùng Profile
    best_link = None
    best_score = 0.30
    best_src = ""

    for r in master_rows:
        link = (r.get("link_bao") or "").strip()
        if not link:
            continue
        is_same_prof = (r.get("profile") == profile)
        cap_m = r.get("caption_moi") or ""
        cap_g = r.get("caption_goc") or ""
        s = max(_do_tuong_dong_caption(caption, cap_m), _do_tuong_dong_caption(caption, cap_g))
        eff_score = s * 1.5 if is_same_prof else s
        if eff_score > best_score:
            best_score = eff_score
            best_link = link
            best_src = f"Master ({r.get('profile')})"

    if best_link and best_score >= 0.35:
        return best_link, best_src

    # 2. Tìm trong Content Pool
    pool_best_link = None
    pool_best_score = 0.35
    pool_best_src = ""
    for p in (pool or []):
        link = str(p.get("Article URL") or "").strip()
        if not link:
            continue
        cap_m = str(p.get("Caption mới") or "").strip()
        cap_g = str(p.get("Caption") or "").strip()
        s = max(_do_tuong_dong_caption(caption, cap_m), _do_tuong_dong_caption(caption, cap_g))
        if s > pool_best_score:
            pool_best_score = s
            pool_best_link = link
            pool_best_src = f"Pool ({p.get('Content ID')})"

    if pool_best_link:
        return pool_best_link, pool_best_src

    return None, "Không tìm thấy trong kho"


# ==========================================
# ENGINE QUÉT PROFILE & ĐỐI SOÁT BÀI
# ==========================================

def lay_chi_tiet_reel(page, reel_url: str) -> dict:
    """Mở nhanh trực tiếp link Reel để bóc tách caption đầy đủ, lượt like, comment và share."""
    if not reel_url or "/reel/" not in reel_url:
        return {}
    try:
        page.goto(reel_url, timeout=15000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        cao_fb._dong_popup_va_mo_khoa_cuon(page)
        
        info = page.evaluate("""() => {
            let cap = '';
            // Tìm caption trong Reel viewer
            const blacklist = ['đăng nhập', 'log in', 'bạn quên', 'forgot account', 'facebook ©', 'create new account', 'sign up', 'see more', 'xem thêm', 'original audio', 'âm thanh gốc'];
            const spans = Array.from(document.querySelectorAll('span, div')).filter(el => {
                const t = (el.innerText || '').trim();
                const low = t.toLowerCase();
                return t.length >= 25 && !blacklist.some(b => low.includes(b));
            });
            if (spans.length > 0) {
                const sorted = spans.slice(0, 10).sort((a, b) => (b.innerText || '').length - (a.innerText || '').length);
                cap = (sorted[0].innerText || '').trim().split('\\n')[0];
            }
            
            // Tìm like, comment, share trong action toolbar bên phải
            const candidates = Array.from(document.querySelectorAll('span, div')).filter(el => {
                const t = (el.innerText || '').trim();
                return /^[\\d.,]+[kKmM]?$/.test(t) && el.children.length === 0;
            }).map(c => c.innerText.trim());
            
            return { cap, candidates };
        }""")
        
        cap = info.get("cap") or ""
        cands = info.get("candidates") or []
        likes = cao_fb._parse_so(cands[0]) if len(cands) > 0 else 0
        cmts = cao_fb._parse_so(cands[1]) if len(cands) > 1 else 0
        shares = cao_fb._parse_so(cands[2]) if len(cands) > 2 else 0
        
        return {
            "caption": cap,
            "likes": likes,
            "comments": cmts,
            "shares": shares
        }
    except Exception:
        return {}


def quet_profile_lay_bai(page_url: str, so_bai_quet: int = 6, proxy: str = None, timeout: int = 30, browser=None, nguong_view: int = 1000) -> list[dict]:
    """Dùng Playwright mở profile ở chế độ khách (Guest mode), cuộn lấy N bài gần nhất qua proxy độc lập.
    Hỗ trợ nhận `browser` dùng chung từ worker để tái sử dụng, tránh mở/đóng tiến trình trình duyệt liên tục."""
    ket_qua = []

    def _quet_tren_browser(b):
        ctx = b.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=cao_fb.DEFAULT_UA,
            locale="en-US"
        )
        # Quét giám sát chỉ cần text + số liệu -> chặn cả ảnh/video/font, tiết kiệm RAM
        cao_fb.chan_tai_nguyen_nang(ctx, chan_anh=True)
        page = ctx.new_page()
        try:
            cao_fb.cai_dat_bypass_popup(page, ctx)
            page.goto(page_url, timeout=timeout * 1000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            cao_fb._dong_popup_va_mo_khoa_cuon(page)

            # Cuộn trang để Facebook render đủ bài
            so_lan_cuon = max(2, min(5, int(so_bai_quet // 2) + 1))
            for _ in range(so_lan_cuon):
                if STOP_EVENT.is_set():
                    break
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(1500)
                cao_fb._dong_popup_va_mo_khoa_cuon(page)

            # Mở "Xem thêm"
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
            except Exception:
                pass

            arts = cao_fb.lay_du_lieu_dom(page, so_bai_quet)

            # KIỂM TRA BỔ SUNG TAB REELS:
            # Đối với Fanpage / Profile dạng New Pages Experience chuyên đi Reels,
            # các video Reel trên timeline KHÔNG hiển thị số lượt xem (views = 0),
            # số lượt xem thực tế hiển thị trên tab Reels (`&sk=reels_tab` hoặc `/reels/`).
            can_quet_reels = (
                not arts 
                or len(arts) < 3 
                or any(("/reel/" in (a.get("post_url") or "") or a.get("is_reel")) and (a.get("views") or 0) == 0 for a in arts)
                or all((a.get("views") or 0) == 0 for a in arts)
            )

            def _lay_reel_id(u):
                if not u: return ""
                m = re.search(r"/reel/(\d+)", u)
                return m.group(1) if m else u.split("?")[0].rstrip("/")

            if can_quet_reels:
                clean_url = page_url.split("?")[0].rstrip("/")
                if "profile.php" in page_url:
                    url_reels = page_url + ("&sk=reels_tab" if "?" in page_url else "?sk=reels_tab")
                else:
                    url_reels = f"{clean_url}/reels/"
                try:
                    page.goto(url_reels, timeout=18000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)
                    cao_fb._dong_popup_va_mo_khoa_cuon(page)
                    for _ in range(2):
                        page.mouse.wheel(0, 1200)
                        page.wait_for_timeout(800)
                    arts_reels = cao_fb.lay_du_lieu_dom(page, so_bai_quet + 4)

                    # 1. Trích xuất bản đồ views từ tab Reels theo reel ID
                    reels_views_map = {}
                    for ar in arts_reels:
                        rid = _lay_reel_id(ar.get("post_url"))
                        v = ar.get("views") or 0
                        if rid and v > 0:
                            reels_views_map[rid] = v

                    # 2. Cập nhật view cho các bài đã lấy trên timeline nếu trùng reel_id
                    for a in arts:
                        rid = _lay_reel_id(a.get("post_url"))
                        if rid in reels_views_map:
                            if (a.get("views") or 0) < reels_views_map[rid]:
                                a["views"] = reels_views_map[rid]

                    # 3. Bổ sung các bài Reel từ tab Reels mà timeline chưa có
                    seen_reel_ids = {_lay_reel_id(a.get("post_url")) for a in arts if a.get("post_url")}
                    for ar in arts_reels:
                        rid = _lay_reel_id(ar.get("post_url"))
                        if rid not in seen_reel_ids:
                            arts.append(ar)
                            seen_reel_ids.add(rid)
                except Exception:
                    pass

            # Kiểm tra bóc tách chi tiết cho các bài Reel nổ mà thiếu caption hoặc chưa có comment
            for a in arts:
                p_url = a.get("post_url") or ""
                v = a.get("views") or 0
                txt = cao_fb.lam_sach_text_bai(a.get("text") or "")
                la_nhan_rac = not txt or any(r in txt.lower() for r in ["bản xem trước", "thước phim", "reels", "reel", "preview", "tile"])
                if v >= nguong_view and "/reel/" in p_url and (la_nhan_rac or a.get("comments", 0) == 0):
                    ct = lay_chi_tiet_reel(page, p_url)
                    if ct.get("caption"):
                        a["text"] = ct["caption"]
                        a["caption"] = ct["caption"]
                    if ct.get("likes"):
                        a["likes"] = ct["likes"]
                    if ct.get("comments"):
                        a["comments"] = ct["comments"]
                    if ct.get("shares"):
                        a["shares"] = ct["shares"]

            for a in arts:
                txt = cao_fb.lam_sach_text_bai(a.get("text") or a.get("caption") or "")
                if not txt and not a.get("post_url"):
                    continue
                ket_qua.append({
                    "post_id": a.get("post_id") or "",
                    "post_url": a.get("post_url") or "",
                    "caption": a.get("caption") or txt,
                    "views": a.get("views") or 0,
                    "likes": a.get("likes") or 0,
                    "comments": a.get("comments") or 0,
                    "shares": a.get("shares") or 0,
                    "is_reel": a.get("is_reel", False),
                })
        finally:
            cao_fb.don_dep_trang(page, ctx)

    if browser is not None:
        _quet_tren_browser(browser)
    else:
        with sync_playwright() as p:
            b_local = cao_fb.mo_trinh_duyet(p, cookies=None, proxy=proxy)
            try:
                _quet_tren_browser(b_local)
            finally:
                try:
                    b_local.close()
                except Exception:
                    pass

    return ket_qua


def xu_ly_mot_vong_quet() -> dict:
    """Quét song song tất cả profile theo proxy, kiểm tra bài nổ và 0 cmt."""
    cfg = doc_cau_hinh()
    masters_kich_hoat = cfg.get("masters_kich_hoat", {"us": True, "us2": True, "global": True})
    ds_profile_all = cfg.get("danh_sach_profile", [])
    if not ds_profile_all:
        kq_3m = nap_danh_sach_profile_tu_3_master(masters_kich_hoat)
        ds_profile_all = kq_3m.get("danh_sach_kich_hoat", [])
        cfg["danh_sach_profile"] = ds_profile_all
        luu_cau_hinh(cfg)

    # Lọc danh sách profile theo các Master đang được BẬT hoặc profile do người dùng tự nhập
    ds_profile = []
    seen = set()
    for p in ds_profile_all:
        if isinstance(p, str):
            p = {"url": p.strip(), "master": "tu_nhap", "profile": "Tự nhập", "tu_nhap": True}
        
        u_raw = str(p.get("url") or "").strip()
        if not u_raw:
            continue
        if not u_raw.startswith("http"):
            u_raw = f"https://www.facebook.com/{u_raw.lstrip('/')}"
        p["url"] = u_raw

        m = (p.get("master") or "").lower()
        if not m:
            prof_name = (p.get("profile") or "").lower()
            if "content new us" in prof_name:
                m = "us2"
            elif any(x in prof_name for x in ["019", "020", "021", "022", "023", "024", "025", "026"]):
                m = "global"
            elif any(x in prof_name for x in ["fb-page-via", "via"]):
                m = "us"
            else:
                m = "tu_nhap"
            p["master"] = m

        la_tu_nhap = p.get("tu_nhap") or m in ("tu_nhap", "custom", "tu_do")
        if la_tu_nhap or masters_kich_hoat.get(m, True):
            u_norm = u_raw.lower()
            if u_norm not in seen:
                seen.add(u_norm)
                ds_profile.append(p)

    nguong_view = int(cfg.get("nguong_view") or 1000)
    nguong_like = int(cfg.get("nguong_like") or 10)
    so_bai_quet = int(cfg.get("so_bai_quet") or 6)
    target_tele = cfg.get("telegram_target", "").strip()

    # Nạp danh sách proxy từ proxies.txt
    proxies = []
    if os.path.exists("proxies.txt"):
        try:
            proxies_raw = cao_fb.doc_proxies("proxies.txt")
            from scraper import loc_proxy_song
            proxies = loc_proxy_song(proxies_raw, max_timeout=3.5)
        except Exception:
            proxies = []

    # GIỚI HẠN SỐ TRÌNH DUYỆT SONG SONG: mỗi worker mở 1 trình duyệt thật
    # (~500-700MB RAM). Trước đây cho tới 20 luồng = 20 trình duyệt => tràn RAM,
    # máy đơ/tự reset. Giờ giới hạn 3 tầng: số proxy, cấu hình, và RAM còn trống.
    tran_theo_ram = cao_fb.gioi_han_trinh_duyet_theo_ram()
    so_worker = min(max(len(proxies), 1),
                    max(1, int(cfg.get("song_song") or 3)),
                    tran_theo_ram)

    ls = doc_lich_su()
    da_bao_views = ls.get("da_bao_no_views", {})
    da_bao_likes = ls.get("da_bao_no_likes", {})

    so_bai_no_vong_nay = 0
    so_bai_no_co_link_vong_nay = 0
    so_bai_no_chua_link_vong_nay = 0

    hang_doi = queue.Queue()
    for idx, p_item in enumerate(ds_profile, 1):
        hang_doi.put((idx, p_item))

    lock_state = threading.Lock()
    so_da_quet = 0

    masters_bat = [k.upper() for k, v in masters_kich_hoat.items() if v]
    masters_str = ", ".join(masters_bat) if masters_bat else "Không chọn Master nào"

    cap_nhat_trang_thai(
        so_profile=len(ds_profile),
        tien_do=f"Bắt đầu quét {len(ds_profile)} profile [{masters_str}] ({so_worker} luồng song song)...",
        log_moi=f"🚀 Bắt đầu quét {len(ds_profile)} profile [{masters_str}] ({so_worker} luồng, ngưỡng view={nguong_view:,}, bài/profile={so_bai_quet})"
    )

    def worker_loop(w_idx):
        nonlocal so_da_quet, so_bai_no_vong_nay, so_bai_no_co_link_vong_nay, so_bai_no_chua_link_vong_nay
        px = proxies[w_idx % len(proxies)] if proxies else None

        # TÁI SỬ DỤNG DUY NHẤT 1 TRÌNH DUYỆT CHO MỖI WORKER XUYÊN SUỐT VÒNG QUÉT
        # Tránh khởi tạo/hủy tiến trình 104 lần gây nghẽn RAM và lỗi BSOD 0x10E
        with sync_playwright() as p:
            browser = None
            try:
                browser = cao_fb.mo_trinh_duyet(p, cookies=None, proxy=px)
            except Exception as e_br:
                with lock_state:
                    cap_nhat_trang_thai(log_moi=f"❌ [W{w_idx+1}] Không mở được trình duyệt: {e_br}")
                return

            so_quet_worker = 0
            try:
                while not hang_doi.empty():
                    if STOP_EVENT.is_set():
                        break
                    try:
                        i, item = hang_doi.get_nowait()
                    except queue.Empty:
                        break

                    url = item.get("url") if isinstance(item, dict) else str(item)
                    ten_prof = (item.get("profile") if isinstance(item, dict) else "") or f"Profile #{i}"
                    ten_page = (item.get("ten_page") if isinstance(item, dict) else "") or "Page"

                    with lock_state:
                        so_da_quet += 1
                        cap_nhat_trang_thai(
                            tien_do=f"Đang quét ({so_da_quet}/{len(ds_profile)}): {ten_prof} ({ten_page})",
                            log_moi=f"🔍 [{so_da_quet}/{len(ds_profile)}] [W{w_idx+1}] Quét {ten_prof} - {ten_page}..."
                        )

                    posts = []
                    try:
                        posts = quet_profile_lay_bai(url, so_bai_quet=so_bai_quet, proxy=px, timeout=35, browser=browser, nguong_view=nguong_view)
                    except Exception as e_prof:
                        # Tự động thử lại 1 lần trực tiếp nếu proxy bị lỗi kết nối
                        try:
                            time.sleep(1.5)
                            posts = quet_profile_lay_bai(url, so_bai_quet=so_bai_quet, proxy=None, timeout=30, nguong_view=nguong_view)
                        except Exception:
                            with lock_state:
                                cap_nhat_trang_thai(log_moi=f"⚠️ [W{w_idx+1}] {ten_prof}: lỗi ({e_prof}) -> bỏ qua")
                            hang_doi.task_done()
                            continue

                    co_bai_no_prof = False

                    for idx_post, p in enumerate(posts):
                        pid = p.get("post_id") or p.get("post_url")
                        views = p.get("views", 0)
                        likes = p.get("likes", 0)
                        cmts = p.get("comments", 0)
                        shares = p.get("shares", 0)
                        caption = p.get("caption", "")
                        post_url = p.get("post_url") or url
                        muoi_tu = lay_10_tu_dau(caption)
                        fps = _tao_fingerprints_bai(ten_prof, pid, post_url, caption)

                        # KIỂM TRA BÀI NỔ (views >= nguong_view) — CẢNH BÁO KHI VƯỢT 1000 VIEW
                        if views >= nguong_view:
                            # 1. KIỂM TRA XEM BÀI ĐÃ ĐƯỢC NGƯỜI DÙNG ĐÁNH DẤU OK (ĐÃ GẮN CMT) CHƯA:
                            # Nếu đã đánh dấu OK -> Tuyệt đối không gửi thông báo lại bài này nữa
                            if kiem_tra_da_danh_dau_ok(fps, link_fb=post_url, post_id=pid):
                                with lock_state:
                                    for k in fps:
                                        da_bao_views[k] = views
                                        da_bao_likes[k] = likes
                                continue

                            with lock_state:
                                can_bao, view_cu, view_tang = kiem_tra_can_bao_no(fps, views, da_bao_views, buoc_tang=500)
                            if can_bao:
                                with lock_state:
                                    for k in fps:
                                        da_bao_views[k] = views
                                        da_bao_likes[k] = likes
                                co_bai_no_prof = True

                                # Tra cứu link báo tương ứng trong Master / Pool
                                link_bao, _ = tim_link_bao_tuong_ung(ten_prof, caption)
                                co_link_cmt = (cmts > 0)

                                if co_link_cmt:
                                    with lock_state:
                                        so_bai_no_vong_nay += 1
                                        so_bai_no_co_link_vong_nay += 1

                                    link_bao_str = f"\n📰 Link bài báo: {link_bao}" if link_bao else ""
                                    if view_cu > 0:
                                        tieu_de_tele = f"🚀 [BÀI NỔ TĂNG +{view_tang:,} VIEW - CÓ CMT]"
                                        dong_view = f"📈 Views: {views:,} (Tăng +{view_tang:,} từ {view_cu:,} view cũ | Ngưỡng: {nguong_view:,})"
                                        log_str = f"🚀 [BÀI NỔ CÓ CMT (+{view_tang:,} VIEW)] {ten_prof} ({ten_page}): {views:,} view, {likes} like, {cmts} cmt"
                                    else:
                                        tieu_de_tele = f"🔥 [BÀI NỔ ĐẠT {views:,} VIEW - ĐÃ CÓ LINK CMT]"
                                        dong_view = f"👁️ Views: {views:,} (Ngưỡng cảnh báo: {nguong_view:,})"
                                        log_str = f"🔥 [BÀI NỔ CÓ CMT] {ten_prof} ({ten_page}): {views:,} view, {likes} like, {cmts} cmt"

                                    msg_tele = (
                                        f"{tieu_de_tele}\n"
                                        f"👤 Profile: {ten_prof}\n"
                                        f"🌐 Link Profile: {url}\n"
                                        f"📄 Tên Page: {ten_page}\n"
                                        f"{dong_view}\n"
                                        f"👍 Likes: {likes} | 💬 Comments: {cmts} | 🔄 Shares: {shares}\n"
                                        f"🔗 Trạng thái cmt: ✅ CÓ LINK CMT ({cmts} bình luận)\n"
                                        f"🔗 Link bài viết: {post_url}\n"
                                        f"📝 Caption: {muoi_tu}"
                                        f"{link_bao_str}"
                                    )
                                    loai_record = "BAI_NO_CO_LINK"
                                else:
                                    with lock_state:
                                        so_bai_no_vong_nay += 1
                                        so_bai_no_chua_link_vong_nay += 1

                                    link_bao_str = f"\n📰 Link báo cần gắn cmt: {link_bao}" if link_bao else "\n📰 Link báo cần gắn cmt: (Chưa tìm thấy trong kho Master/Pool)"
                                    if view_cu > 0:
                                        tieu_de_tele = f"🚀 [BÀI NỔ TĂNG +{view_tang:,} VIEW - K CÓ CMT]"
                                        dong_view = f"📈 Views: {views:,} (Tăng +{view_tang:,} từ {view_cu:,} view cũ | Ngưỡng: {nguong_view:,})"
                                        log_str = f"🚨 [BÀI NỔ K CÓ CMT (+{view_tang:,} VIEW)] {ten_prof} ({ten_page}): {views:,} view, {likes} like, 0 cmt ⚠️ Cần gắn: {link_bao or 'Chưa có link'}"
                                    else:
                                        tieu_de_tele = f"🚨 [BÀI NỔ ĐẠT {views:,} VIEW - CHƯA GẮN LINK CMT]"
                                        dong_view = f"👁️ Views: {views:,} (Ngưỡng cảnh báo: {nguong_view:,})"
                                        log_str = f"🚨 [BÀI NỔ K CÓ CMT] {ten_prof} ({ten_page}): {views:,} view, {likes} like, 0 cmt ⚠️ Cần gắn: {link_bao or 'Chưa có link'}"

                                    msg_tele = (
                                        f"{tieu_de_tele}\n"
                                        f"👤 Profile: {ten_prof}\n"
                                        f"🌐 Link Profile: {url}\n"
                                        f"📄 Tên Page: {ten_page}\n"
                                        f"{dong_view}\n"
                                        f"👍 Likes: {likes} | 💬 Comments: 0 | 🔄 Shares: {shares}\n"
                                        f"⚠️ Trạng thái cmt: ❌ K CÓ CMT (0 bình luận - CẦN GẮN NGAY!)\n"
                                        f"🔗 Link bài viết: {post_url}\n"
                                        f"📝 Caption: {muoi_tu}"
                                        f"{link_bao_str}"
                                    )
                                    loai_record = "BAI_NO_CHUA_LINK"

                                ok, err = gui_thong_bao_telegram(target_tele, msg_tele) if target_tele else (False, "Chưa nhập Target Telegram")

                                record = {
                                    "thoi_gian": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "loai": loai_record,
                                    "post_id": pid,
                                    "views": views,
                                    "view_cu": view_cu,
                                    "view_tang": view_tang,
                                    "likes": likes,
                                    "like_cu": view_cu,
                                    "like_tang": view_tang,
                                    "comments": cmts,
                                    "shares": shares,
                                    "profile": ten_prof,
                                    "link_profile": url,
                                    "ten_page": ten_page,
                                    "muoi_tu_dau": muoi_tu,
                                    "caption_dau": muoi_tu,
                                    "link_fb": post_url,
                                    "link_bao": link_bao,
                                    "gui_tele": ok,
                                    "tele_msg": err,
                                    "fingerprints": fps,
                                    "trang_thai_link": "CO_LINK" if co_link_cmt else "CHUA_LINK",
                                }
                                ghi_lich_su_canh_bao(record)
                                with lock_state:
                                    cap_nhat_trang_thai(log_moi=f"{log_str} | Tele: {'✅' if ok else '❌ ' + err}")

                    if not co_bai_no_prof:
                        with lock_state:
                            if posts:
                                cap_nhat_trang_thai(log_moi=f"✅ [W{w_idx+1}] {ten_prof} ({ten_page}): {len(posts)} bài bình thường (không có bài nổ)")
                            else:
                                cap_nhat_trang_thai(log_moi=f"ℹ️ [W{w_idx+1}] {ten_prof} ({ten_page}): Không tìm thấy bài viết mới trên trang")

                    hang_doi.task_done()
                so_quet_worker += 1
                if so_quet_worker >= 25 and browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
                    try:
                        browser = cao_fb.mo_trinh_duyet(p, cookies=None, proxy=px)
                    except Exception:
                        pass
                    so_quet_worker = 0

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

    st_now = doc_trang_thai()
    chu_ky_nghi = int(cfg.get("chu_ky_phut") or 5)
    if so_bai_no_vong_nay == 0:
        log_ket_thuc = f"✨ [HOÀN THÀNH VÒNG QUÉT] Quét xong {len(ds_profile)} profile ({so_worker} luồng): Tất cả bình thường, KHÔNG phát hiện bài nổ mới."
        tien_do_str = f"Tất cả {len(ds_profile)} profile bình thường (0 bài nổ). Chờ chu kỳ tiếp theo ({chu_ky_nghi} phút)..."
    else:
        log_ket_thuc = f"🏁 Đã quét xong {len(ds_profile)} profile ({so_worker} luồng). Vòng này: +{so_bai_no_vong_nay} bài nổ (+{so_bai_no_co_link_vong_nay} có link cmt, +{so_bai_no_chua_link_vong_nay} chưa gắn link)."
        tien_do_str = f"Hoàn thành 1 vòng (+{so_bai_no_vong_nay} bài nổ: {so_bai_no_co_link_vong_nay} có link, {so_bai_no_chua_link_vong_nay} chưa link). Chờ chu kỳ tiếp theo ({chu_ky_nghi} phút)..."

    cap_nhat_trang_thai(
        lan_quet_cuoi=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        tien_do=tien_do_str,
        so_bai_no=st_now.get("so_bai_no", 0) + so_bai_no_vong_nay,
        so_bai_no_co_link=st_now.get("so_bai_no_co_link", 0) + so_bai_no_co_link_vong_nay,
        so_bai_no_chua_link=st_now.get("so_bai_no_chua_link", 0) + so_bai_no_chua_link_vong_nay,
        so_bai_chua_cmt=st_now.get("so_bai_no_chua_link", 0) + so_bai_no_chua_link_vong_nay,
        log_moi=log_ket_thuc
    )

    return {
        "so_profile": len(ds_profile),
        "so_bai_no": so_bai_no_vong_nay,
        "so_bai_no_co_link": so_bai_no_co_link_vong_nay,
        "so_bai_no_chua_link": so_bai_no_chua_link_vong_nay,
    }


# ==========================================
# QUẢN LÝ VÒNG LẶP 24/24
# ==========================================

def _vong_lap_daemon():
    while not STOP_EVENT.is_set():
        cap_nhat_trang_thai(dang_chay=True)
        try:
            xu_ly_mot_vong_quet()
        except Exception as e:
            cap_nhat_trang_thai(log_moi=f"❌ Lỗi vòng quét: {e}")

        cfg = doc_cau_hinh()
        chu_ky_phut = max(1, int(cfg.get("chu_ky_phut") or 5))
        cap_nhat_trang_thai(
            tien_do=f"Chờ chu kỳ tiếp theo ({chu_ky_phut} phút)...",
            log_moi=f"⏳ Nghỉ {chu_ky_phut} phút trước vòng quét tiếp theo..."
        )
        if STOP_EVENT.wait(timeout=chu_ky_phut * 60):
            break

    cap_nhat_trang_thai(dang_chay=False, tien_do="Đã dừng theo yêu cầu", log_moi="⏹️ Đã dừng giám sát 24/24")


def bat_dau_giam_sat() -> tuple[bool, str]:
    global GIAM_SAT_THREAD
    if GIAM_SAT_THREAD and GIAM_SAT_THREAD.is_alive():
        return False, "Hệ thống giám sát bài nổ 24/24 đang chạy rồi!"

    STOP_EVENT.clear()
    GIAM_SAT_THREAD = threading.Thread(target=_vong_lap_daemon, daemon=True, name="LuongGiamSatBaiNo247")
    GIAM_SAT_THREAD.start()
    cap_nhat_trang_thai(dang_chay=True, tien_do="Đang khởi động vòng quét...", log_moi="▶️ Đã kích hoạt giám sát bài nổ 24/24")
    return True, "Đã khởi động thành công hệ thống giám sát bài nổ 24/24!"


def dung_giam_sat() -> tuple[bool, str]:
    global GIAM_SAT_THREAD
    STOP_EVENT.set()
    cap_nhat_trang_thai(dang_chay=False, tien_do="Đã gửi lệnh dừng...", log_moi="⏹️ Đang dừng tiến trình giám sát...")
    return True, "Đã gửi lệnh dừng giám sát thành công!"


def quet_thu_cong_ngay() -> tuple[bool, str]:
    """Kích hoạt quét thủ công ngay 1 vòng không cần chờ chu kỳ."""
    def _chay():
        try:
            cap_nhat_trang_thai(tien_do="Đang quét thủ công 1 vòng...", log_moi="⚡ Bắt đầu quét thủ công ngay 1 vòng...")
            xu_ly_mot_vong_quet()
        except Exception as e:
            cap_nhat_trang_thai(log_moi=f"❌ Lỗi quét thủ công: {e}")

    t = threading.Thread(target=_chay, daemon=True)
    t.start()
    return True, "Đã kích hoạt quét ngay 1 vòng thành công!"
