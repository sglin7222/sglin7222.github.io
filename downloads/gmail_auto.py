# -*- coding: utf-8 -*-
"""
Gmail 自動讀寫範例程式（使用 16 字元應用程式密碼）
====================================================
功能：
  1. 透過 IMAP (imap.gmail.com:993, SSL) 讀取信件
  2. 透過 SMTP (smtp.gmail.com:465, SSL) 寄送信件
  3. 每一步驟的實際執行結果都會印在畫面上，並寫入 results/ 目錄的紀錄檔，
     供同學撰寫報告時引用（報告內容必須依實際結果）。

認證方式（16 字元應用程式密碼）：
  Google 帳戶 → 安全性 → 開啟「兩步驟驗證」 → 搜尋「應用程式密碼」 → 建立
  → 會得到形如  abcd efgh ijkl mnop  的 16 字元密碼（空格可留可不留）
  ※ 一般 Gmail 登入密碼無法用於 IMAP/SMTP，一定要用應用程式密碼。

設定方式（二選一）：
  A. 環境變數  GMAIL_USER / GMAIL_APP_PASSWORD
  B. 將 config.example.json 複製成 config.json 後填入

用法：
  python gmail_auto.py read  [--limit 10] [--unseen] [--since 2026-10-01] [--mailbox INBOX]
  python gmail_auto.py send  --to a@b.com --subject "主旨" --body "內文" [--attach 檔案 ...]
  python gmail_auto.py selftest        # 寄一封信給自己，等 5 秒後讀取並確認有收到
"""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import smtplib
import ssl
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime, formataddr
from pathlib import Path
from typing import Optional

# Windows 終端預設不是 UTF-8，強制用 UTF-8 輸出以免中文變亂碼
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

IMAP_HOST, IMAP_PORT = "imap.gmail.com", 993
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
BASE_DIR = Path(__file__).resolve().parent


# ----------------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------------
@dataclass
class Config:
    user: str
    app_password: str
    sender_name: str = ""

    @staticmethod
    def load(path: Path = BASE_DIR / "config.json") -> "Config":
        user = os.environ.get("GMAIL_USER", "").strip()
        pwd = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
        name = os.environ.get("GMAIL_SENDER_NAME", "").strip()
        if not (user and pwd) and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            user = user or data.get("user", "")
            pwd = pwd or data.get("app_password", "")
            name = name or data.get("sender_name", "")
        pwd = pwd.replace(" ", "")  # Google 顯示時每 4 字一組加空白，這裡去掉
        if not user or not pwd:
            raise SystemExit(
                "找不到帳號或應用程式密碼。請設定環境變數 GMAIL_USER / GMAIL_APP_PASSWORD，"
                "或複製 config.example.json 為 config.json 後填入。"
            )
        if len(pwd) != 16:
            raise SystemExit(f"應用程式密碼應為 16 字元，目前為 {len(pwd)} 字元，請重新確認。")
        return Config(user=user, app_password=pwd, sender_name=name)


# ----------------------------------------------------------------------------
# 執行結果紀錄
# ----------------------------------------------------------------------------
@dataclass
class MailSummary:
    uid: str
    date: str
    sender: str
    subject: str
    snippet: str


@dataclass
class RunResult:
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str = ""
    mode: str = ""
    account: str = ""
    steps: list[dict] = field(default_factory=list)   # 每一步驟：name / ok / detail / seconds
    mails: list[MailSummary] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def step(self, name: str, ok: bool, detail: str, seconds: float) -> None:
        self.steps.append({"name": name, "ok": ok, "detail": detail, "seconds": round(seconds, 2)})
        print(f"[{'OK ' if ok else 'ERR'}] {name}: {detail} ({seconds:.2f}s)")
        if not ok:
            self.errors.append(f"{name}: {detail}")


def _decode(s) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return str(s)


def _body_snippet(msg: email.message.Message, limit: int = 120) -> str:
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                break
    else:
        payload = msg.get_payload(decode=True) or b""
        text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


# ----------------------------------------------------------------------------
# Gmail 用戶端
# ----------------------------------------------------------------------------
class GmailClient:
    def __init__(self, cfg: Config, result: RunResult):
        self.cfg = cfg
        self.result = result
        self.imap: Optional[imaplib.IMAP4_SSL] = None

    # ---- IMAP 讀取 ----
    def imap_login(self) -> None:
        t = time.perf_counter()
        try:
            self.imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context())
            self.imap.login(self.cfg.user, self.cfg.app_password)
            self.result.step("IMAP 登入", True,
                             f"{IMAP_HOST}:{IMAP_PORT} 以 {self.cfg.user} 登入成功", time.perf_counter() - t)
        except imaplib.IMAP4.error as e:
            self.result.step("IMAP 登入", False,
                             f"認證失敗：{e}（請確認已開啟兩步驟驗證並使用應用程式密碼）", time.perf_counter() - t)
            raise
        except Exception as e:
            self.result.step("IMAP 登入", False, f"{type(e).__name__}: {e}", time.perf_counter() - t)
            raise

    def read(self, limit: int = 10, unseen: bool = False, since: Optional[date] = None,
             subject: Optional[str] = None, mailbox: str = "INBOX") -> list[MailSummary]:
        assert self.imap is not None
        t = time.perf_counter()
        status, data = self.imap.select(mailbox, readonly=True)
        total = int(data[0]) if status == "OK" else 0
        criteria = ["UNSEEN" if unseen else "ALL"]
        if since:
            criteria += ["SINCE", since.strftime("%d-%b-%Y")]
        if subject:
            criteria += ["SUBJECT", f'"{subject}"']
        status, data = self.imap.uid("search", None, *criteria)
        uids = data[0].split() if status == "OK" else []
        picked = uids[-limit:][::-1]  # 最新的排前面
        mails: list[MailSummary] = []
        for uid in picked:
            status, msgdata = self.imap.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msgdata or msgdata[0] is None:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            try:
                d = parsedate_to_datetime(msg.get("Date")).astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                d = msg.get("Date", "")
            mails.append(MailSummary(
                uid=uid.decode(), date=d, sender=_decode(msg.get("From")),
                subject=_decode(msg.get("Subject")), snippet=_body_snippet(msg)))
        self.result.mails.extend(mails)
        self.result.step(
            "IMAP 讀取", True,
            f"信箱 {mailbox} 共 {total} 封，條件 {' '.join(criteria)} 符合 {len(uids)} 封，取回 {len(mails)} 封",
            time.perf_counter() - t)
        return mails

    def imap_logout(self) -> None:
        if self.imap is not None:
            try:
                self.imap.logout()
            except Exception:
                pass
            self.imap = None

    # ---- SMTP 寄送 ----
    def send(self, to: list[str], subject: str, body: str, attachments: list[Path] | None = None) -> None:
        t = time.perf_counter()
        msg = EmailMessage()
        msg["From"] = formataddr((self.cfg.sender_name, self.cfg.user)) if self.cfg.sender_name else self.cfg.user
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.set_content(body)
        for p in attachments or []:
            p = Path(p)
            msg.add_attachment(p.read_bytes(), maintype="application", subtype="octet-stream", filename=p.name)
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as s:
                s.login(self.cfg.user, self.cfg.app_password)
                s.send_message(msg)
            self.result.sent.append({"to": to, "subject": subject,
                                     "attachments": [Path(p).name for p in attachments or []],
                                     "time": datetime.now().isoformat(timespec="seconds")})
            self.result.step("SMTP 寄送", True, f"寄給 {', '.join(to)}，主旨「{subject}」", time.perf_counter() - t)
        except smtplib.SMTPAuthenticationError as e:
            self.result.step("SMTP 寄送", False,
                             f"認證失敗：{e.smtp_code} {e.smtp_error.decode(errors='replace')}", time.perf_counter() - t)
            raise
        except Exception as e:
            self.result.step("SMTP 寄送", False, f"{type(e).__name__}: {e}", time.perf_counter() - t)
            raise


# ----------------------------------------------------------------------------
# 執行結果輸出（給同學貼進報告用）
# ----------------------------------------------------------------------------
def result_to_markdown(r: RunResult) -> str:
    L: list[str] = []
    L.append(f"## 執行紀錄：{r.mode} 模式，{r.started_at} ~ {r.finished_at}")
    L.append("")
    L.append(f"- 帳號：{r.account}")
    L.append(f"- IMAP：{IMAP_HOST}:{IMAP_PORT} (SSL)　SMTP：{SMTP_HOST}:{SMTP_PORT} (SSL)")
    L.append(f"- Python：{sys.version.split()[0]}")
    L.append("")
    L.append("| # | 步驟 | 結果 | 說明 | 耗時(秒) |")
    L.append("|---|---|---|---|---|")
    for i, s in enumerate(r.steps, 1):
        L.append(f"| {i} | {s['name']} | {'成功' if s['ok'] else '失敗'} | {s['detail']} | {s['seconds']} |")
    L.append("")
    if r.mails:
        L.append("| # | 日期 | 寄件者 | 主旨 | 內文摘要 |")
        L.append("|---|---|---|---|---|")
        esc = lambda x: str(x).replace("|", "\\|")
        for i, m in enumerate(r.mails, 1):
            L.append(f"| {i} | {esc(m.date)} | {esc(m.sender)} | {esc(m.subject)} | {esc(m.snippet)} |")
        L.append("")
    if r.sent:
        L.append("| # | 時間 | 收件者 | 主旨 | 附件 |")
        L.append("|---|---|---|---|---|")
        for i, s in enumerate(r.sent, 1):
            L.append(f"| {i} | {s['time']} | {', '.join(s['to'])} | {s['subject']} | {', '.join(s['attachments']) or '無'} |")
        L.append("")
    if r.errors:
        L.append("錯誤：")
        for e in r.errors:
            L.append(f"- {e}")
        L.append("")
    return "\n".join(L)


def save_result(r: RunResult, out_dir: Path = BASE_DIR / "results") -> Path:
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md = out_dir / f"run_{stamp}.md"
    md.write_text(result_to_markdown(r), encoding="utf-8")
    (out_dir / f"run_{stamp}.json").write_text(
        json.dumps(asdict(r), ensure_ascii=False, indent=2), encoding="utf-8")
    return md


# ----------------------------------------------------------------------------
# 主程式
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gmail 自動讀寫（應用程式密碼）")
    ap.add_argument("mode", choices=["read", "send", "selftest"])
    ap.add_argument("--limit", type=int, default=10, help="讀取最新 N 封")
    ap.add_argument("--unseen", action="store_true", help="只讀未讀信")
    ap.add_argument("--since", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    help="只讀此日期之後 (YYYY-MM-DD)")
    ap.add_argument("--mailbox", default="INBOX")
    ap.add_argument("--to", nargs="*", default=[], help="收件者，可多個")
    ap.add_argument("--subject", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--attach", nargs="*", default=[], help="附件路徑")
    ap.add_argument("--no-save", action="store_true", help="不寫 results/ 紀錄檔")
    a = ap.parse_args(argv)

    result = RunResult(mode=a.mode)
    try:
        cfg = Config.load()
    except SystemExit as e:
        print(e)
        return 2
    result.account = cfg.user
    client = GmailClient(cfg, result)

    try:
        if a.mode == "selftest":
            a.to = [cfg.user]
            a.subject = f"[selftest] gmail_auto {datetime.now():%Y-%m-%d %H:%M:%S}"
            a.body = "這是 gmail_auto.py 的自我測試信。若能在讀取結果中看到此信，表示寄送與讀取皆正常。"

        if a.mode in ("send", "selftest"):
            if not a.to or not a.subject:
                print("send 模式需要 --to 與 --subject")
                return 2
            client.send(a.to, a.subject, a.body, [Path(p) for p in a.attach])

        if a.mode in ("read", "selftest"):
            if a.mode == "selftest":
                time.sleep(5)  # 等 Gmail 把信收進信箱
            client.imap_login()
            mails = client.read(limit=a.limit, unseen=a.unseen, since=a.since, mailbox=a.mailbox)
            if a.mode == "selftest":
                t = time.perf_counter()
                hit = any(a.subject in m.subject for m in mails)
                result.step("寄送後驗證", hit,
                            f"{'已' if hit else '尚未'}在 {a.mailbox} 讀到主旨「{a.subject}」的信",
                            time.perf_counter() - t)
            client.imap_logout()
    except Exception:
        traceback.print_exc(limit=1)
    finally:
        client.imap_logout()
        result.finished_at = datetime.now().isoformat(timespec="seconds")
        if not a.no_save:
            path = save_result(result)
            print(f"\n執行紀錄已存檔：{path}（可直接貼進報告）")
    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(main())
