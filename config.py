import os

# Thư mục gốc của Module Giám Sát Bài Nổ Độc Lập
DUONG_DAN = os.path.dirname(os.path.abspath(__file__))

def duong_dan_config():
    return os.path.join(DUONG_DAN, "config.json")
