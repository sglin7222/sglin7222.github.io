# -*- coding: utf-8 -*-
"""
QR Code 導引到網頁 範例程式
============================
功能：
  1. make   ：把網址做成 QR Code 圖片（PNG），可加文字標籤與中央 Logo，
              做完立刻用 OpenCV 解碼驗證內容正確。
  2. decode ：讀取任一張 QR Code 圖片，印出裡面的內容。
  3. serve  ：在自己電腦架一個小網站（site/ 目錄），自動偵測區網 IP，
              產生指向  http://<你的IP>:<port>/  的 QR Code，
              手機連同一個 Wi-Fi 掃描後就會打開你的網頁。
              每一次有人連進來都會記錄（時間、來源 IP、瀏覽器），按 Ctrl+C 結束後寫成紀錄檔。
  4. 每次執行都把實際結果寫到 results/ 目錄（.md 與 .json），寫報告時直接引用。

安裝套件：
  pip install "qrcode[pil]" opencv-python-headless

用法：
  python qr_web.py make --url https://www.google.com --out qr_google.png --label "Google"
  python qr_web.py make --url https://example.com --out qr.png --logo logo.png --ec H
  python qr_web.py decode qr.png
  python qr_web.py serve --port 8000            # 架站並產生 QR，Ctrl+C 結束
"""
from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
SITE_DIR = BASE_DIR / "site"


# ----------------------------------------------------------------------------
# 執行結果紀錄
# ----------------------------------------------------------------------------
@dataclass
class RunResult:
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str = ""
    mode: str = ""
    steps: list[dict] = field(default_factory=list)
    qr_info: dict = field(default_factory=dict)
    visits: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def step(self, name: str, ok: bool, detail: str, seconds: float = 0.0) -> None:
        self.steps.append({"name": name, "ok": ok, "detail": detail, "seconds": round(seconds, 3)})
        print(f"[{'OK ' if ok else 'ERR'}] {name}: {detail}")
        if not ok:
            self.errors.append(f"{name}: {detail}")


def result_to_markdown(r: RunResult) -> str:
    L = [f"## 執行紀錄：{r.mode} 模式，{r.started_at} ~ {r.finished_at}", "",
         f"- Python：{sys.version.split()[0]}", ""]
    if r.qr_info:
        L += ["| 項目 | 實際值 |", "|---|---|"]
        for k, v in r.qr_info.items():
            L.append(f"| {k} | {v} |")
        L.append("")
    L += ["| # | 步驟 | 結果 | 說明 | 耗時(秒) |", "|---|---|---|---|---|"]
    for i, s in enumerate(r.steps, 1):
        L.append(f"| {i} | {s['name']} | {'成功' if s['ok'] else '失敗'} | {s['detail']} | {s['seconds']} |")
    L.append("")
    if r.visits:
        L += [f"共 {len(r.visits)} 次連線：", "", "| # | 時間 | 來源 IP | 路徑 | 瀏覽器 (User-Agent) |", "|---|---|---|---|---|"]
        for i, v in enumerate(r.visits, 1):
            L.append(f"| {i} | {v['time']} | {v['ip']} | {v['path']} | {v['ua'][:80]} |")
        L.append("")
    if r.errors:
        L += ["錯誤："] + [f"- {e}" for e in r.errors] + [""]
    return "\n".join(L)


def save_result(r: RunResult, out_dir: Path = BASE_DIR / "results") -> Path:
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md = out_dir / f"qr_{r.mode}_{stamp}.md"
    md.write_text(result_to_markdown(r), encoding="utf-8")
    (out_dir / f"qr_{r.mode}_{stamp}.json").write_text(json.dumps(asdict(r), ensure_ascii=False, indent=2), encoding="utf-8")
    return md


# ----------------------------------------------------------------------------
# QR Code 產生與解碼
# ----------------------------------------------------------------------------
EC_LEVELS = {"L": "ERROR_CORRECT_L", "M": "ERROR_CORRECT_M", "Q": "ERROR_CORRECT_Q", "H": "ERROR_CORRECT_H"}


def make_qr(url: str, out: Path, label: str | None, logo: Path | None, ec: str, box: int, r: RunResult) -> Path:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    t = time.perf_counter()
    ec = ec.upper()
    if logo and ec != "H":
        ec = "H"  # 有 Logo 遮住中間，一定要最高容錯
        r.step("容錯等級調整", True, "加 Logo 時自動改用 H 級容錯 (30%)")
    qr = qrcode.QRCode(error_correction=getattr(qrcode.constants, EC_LEVELS[ec]), box_size=box, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    if logo:
        lg = Image.open(logo).convert("RGBA")
        side = img.size[0] // 5
        lg.thumbnail((side, side))
        pos = ((img.size[0] - lg.size[0]) // 2, (img.size[1] - lg.size[1]) // 2)
        bg = Image.new("RGBA", (lg.size[0] + 12, lg.size[1] + 12), "white")
        img.paste(bg, (pos[0] - 6, pos[1] - 6), bg)
        img.paste(lg, pos, lg)

    if label:
        font = None
        for fp in ("C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/msjhbd.ttc", "/System/Library/Fonts/PingFang.ttc",
                   "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"):
            if Path(fp).exists():
                font = ImageFont.truetype(fp, max(18, img.size[0] // 16))
                break
        font = font or ImageFont.load_default()
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), label, font=font)
        th = bbox[3] - bbox[1]
        canvas = Image.new("RGB", (img.size[0], img.size[1] + th + 24), "white")
        canvas.paste(img, (0, 0))
        d2 = ImageDraw.Draw(canvas)
        d2.text(((canvas.size[0] - (bbox[2] - bbox[0])) // 2, img.size[1] + 4), label, fill="black", font=font)
        img = canvas

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    r.qr_info.update({
        "網址": url, "輸出檔": str(out), "QR 版本 (version)": qr.version,
        "矩陣大小 (modules)": f"{qr.modules_count} x {qr.modules_count}",
        "容錯等級": ec, "box_size": box, "圖片像素": f"{img.size[0]} x {img.size[1]}",
        "檔案大小 (bytes)": out.stat().st_size, "標籤": label or "無", "Logo": str(logo) if logo else "無",
    })
    r.step("產生 QR Code", True, f"{out.name}，version {qr.version}，{qr.modules_count}x{qr.modules_count}，容錯 {ec}",
           time.perf_counter() - t)
    return out


def decode_qr(path: Path, r: RunResult) -> str | None:
    t = time.perf_counter()
    try:
        import cv2
    except ImportError:
        r.step("解碼驗證", False, "未安裝 opencv-python-headless，無法解碼", time.perf_counter() - t)
        return None
    img = cv2.imread(str(path))
    if img is None:
        r.step("解碼驗證", False, f"讀不到圖片 {path}", time.perf_counter() - t)
        return None
    data, points, _ = cv2.QRCodeDetector().detectAndDecode(img)
    if data:
        r.step("解碼驗證", True, f"解出內容：{data}", time.perf_counter() - t)
        r.qr_info["解碼結果"] = data
    else:
        r.step("解碼驗證", False, "圖片中找不到可解碼的 QR Code（Logo 太大或圖太小？）", time.perf_counter() - t)
    return data or None


# ----------------------------------------------------------------------------
# 本機網站 + 掃描紀錄
# ----------------------------------------------------------------------------
def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # 不會真的送資料，只是讓系統選出對外網卡
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


DEFAULT_INDEX = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QR Code 作業網頁</title>
<style>
 body{font-family:"Microsoft JhengHei",sans-serif;max-width:600px;margin:40px auto;padding:0 16px;line-height:1.7}
 h1{color:#1f3864} .box{background:#f2f6fc;border-radius:12px;padding:16px}
</style></head><body>
<h1>掃描成功！</h1>
<div class="box">
 <p>這是 <b>學號：________　姓名：________</b> 的 QR Code 作業網頁。</p>
 <p>你是透過 QR Code 掃描進來的，伺服器已經記錄了這次連線。</p>
 <p id="t"></p>
</div>
<script>document.getElementById('t').textContent='你的裝置時間：'+new Date().toLocaleString();</script>
</body></html>
"""


def ensure_site() -> None:
    SITE_DIR.mkdir(exist_ok=True)
    idx = SITE_DIR / "index.html"
    if not idx.exists():
        idx.write_text(DEFAULT_INDEX, encoding="utf-8")


def serve(port: int, r: RunResult, out: Path) -> None:
    ensure_site()
    ip = lan_ip()
    url = f"http://{ip}:{port}/"
    make_qr(url, out, f"掃我看網頁 {url}", None, "M", 10, r)
    decode_qr(out, r)

    visits = r.visits

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(SITE_DIR), **k)

        def log_message(self, fmt, *args):  # 取代預設 log，改成記錄到 result
            if self.path.endswith(("favicon.ico",)):
                return
            v = {"time": datetime.now().isoformat(timespec="seconds"), "ip": self.client_address[0],
                 "path": self.path, "ua": self.headers.get("User-Agent", "")}
            visits.append(v)
            print(f"  [訪客 #{len(visits)}] {v['time']} {v['ip']} {v['path']}  {v['ua'][:60]}")

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    r.step("啟動網站", True, f"{url}  （網頁目錄 {SITE_DIR}）")
    print(f"\n網站已啟動：{url}\nQR Code 圖片：{out}\n請用手機連同一個 Wi-Fi 掃描，按 Ctrl+C 結束並寫入紀錄檔。\n")
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGBREAK"):  # Windows 的 Ctrl+Break
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    try:
        while not stop.is_set():
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        r.step("關閉網站", True, f"共記錄 {len(visits)} 次連線，其中不同來源 IP {len({v['ip'] for v in visits})} 個")


# ----------------------------------------------------------------------------
# 主程式
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QR Code 導引到網頁")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-save", action="store_true", help="不寫 results/ 紀錄檔")
    sub = ap.add_subparsers(dest="mode", required=True)
    m = sub.add_parser("make", help="把網址做成 QR Code", parents=[common])
    m.add_argument("--url", required=True)
    m.add_argument("--out", default="qr.png")
    m.add_argument("--label", help="圖片下方文字")
    m.add_argument("--logo", help="中央 Logo 圖片路徑")
    m.add_argument("--ec", default="M", choices=list("LMQH"), help="容錯等級 L7%% M15%% Q25%% H30%%")
    m.add_argument("--box", type=int, default=10, help="每個模組的像素數")
    d = sub.add_parser("decode", help="解碼 QR Code 圖片", parents=[common])
    d.add_argument("image")
    s = sub.add_parser("serve", help="架本機網站並產生 QR", parents=[common])
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--out", default="qr_site.png")
    a = ap.parse_args(argv)

    r = RunResult(mode=a.mode)
    try:
        if a.mode == "make":
            out = make_qr(a.url, Path(a.out), a.label, Path(a.logo) if a.logo else None, a.ec, a.box, r)
            data = decode_qr(out, r)
            if data is not None:
                r.step("內容比對", data == a.url, f"解碼內容 {'等於' if data == a.url else '不等於'} 原網址")
        elif a.mode == "decode":
            decode_qr(Path(a.image), r)
        elif a.mode == "serve":
            serve(a.port, r, Path(a.out))
    except Exception as e:
        r.step("例外", False, f"{type(e).__name__}: {e}")
    finally:
        r.finished_at = datetime.now().isoformat(timespec="seconds")
        if not a.no_save:
            print(f"\n執行紀錄已存檔：{save_result(r)}（可直接貼進報告）")
    return 0 if not r.errors else 1


if __name__ == "__main__":
    sys.exit(main())
