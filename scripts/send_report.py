"""
send_report.py — 執行 main.py 後將當日 PDF 報告寄送至指定信箱

寄送前會自動從 processed_data.json 擷取各篇標題，
組成 100 字以內的摘要放入電子郵件內文。

用法：
  python scripts/send_report.py          # 完整執行（爬取 + AI 處理 + 寄信）
  python scripts/send_report.py --no-run # 只寄最新 PDF，不重新執行 main.py

環境變數（.env）：
  GMAIL_APP_PASSWORD  Gmail 應用程式密碼（16 碼，不含空格）
  GMAIL_SENDER        寄件帳號（預設 geenyu1975@gmail.com）
  GMAIL_RECIPIENT     收件帳號（預設 geenyu@gmail.com）
"""

import argparse
import json
import re
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).parent.parent
load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)

REPORTS_DIR      = BASE_DIR / "reports"
PROCESSED_PATH   = BASE_DIR / "data" / "processed_data.json"
SMTP_TIMEOUT_SEC = 30          # SMTP 連線與操作逾時（秒）
MAX_PDF_MB       = 20          # 拒絕寄送超過此大小的 PDF（MB），防止意外大檔

TZ_TAIWAN = timezone(timedelta(hours=8), "CST")  # 台灣標準時間 UTC+8

# ── 設定（可在 .env 覆寫） ────────────────────────────────────────────────────
GMAIL_SENDER    = os.getenv("GMAIL_SENDER",    "geenyu1975@gmail.com")
GMAIL_RECIPIENT = os.getenv("GMAIL_RECIPIENT", "geenyu@gmail.com")
GMAIL_APP_PW    = os.getenv("GMAIL_APP_PASSWORD", "")

# 類別標籤
_CATEGORY_LABEL = {
    "tech":        "AI 新知",
    "application": "AI 應用",
}


def run_main() -> bool:
    """執行 main.py，回傳是否成功。"""
    main_py = BASE_DIR / "src" / "main.py"
    print(f"[send_report] 執行 {main_py} ...")
    result = subprocess.run(
        [sys.executable, str(main_py)],
        cwd=str(BASE_DIR),
    )
    if result.returncode != 0:
        print(f"[send_report] main.py 回傳非零碼 {result.returncode}，但仍嘗試寄送最新報告。")
        return False
    return True


def find_latest_pdf() -> Path | None:
    """回傳 reports/ 目錄下最新的 PDF 檔。"""
    pdfs = sorted(REPORTS_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


def _extract_title(claude_output: str) -> str:
    """
    從 claude_output 中擷取 ### 標題 區塊的第一行文字。
    找不到時回傳空字串。
    """
    m = re.search(r"###\s*標題\s*\n+([^\n#─]+)", claude_output)
    if m:
        return m.group(1).strip()
    # 備用：取第一個非空白行
    for line in claude_output.splitlines():
        line = line.strip().lstrip("#").strip()
        if line and not line.startswith("─"):
            return line
    return ""


def build_email_summary(today: str) -> str:
    """
    讀取 processed_data.json，用 Claude Haiku 生成 100 字以內的電子郵件內文摘要。
    API 呼叫失敗時，退回至條列標題的備用格式。
    """
    if not PROCESSED_PATH.exists():
        return ""

    try:
        data  = json.loads(PROCESSED_PATH.read_text(encoding="utf-8"))
        items = [it for it in data.get("items", []) if it.get("processed")]
    except Exception:
        return ""

    if not items:
        return ""

    # ── 區分 tech / application 篇目 ────────────────────────────────────────
    tech_titles = []
    app_titles  = []
    for item in items:
        cat   = item.get("category", "application")
        title = _extract_title(item.get("claude_output", "")) or item.get("title", "")
        if not title:
            continue
        if cat == "tech":
            tech_titles.append(title)
        else:
            app_titles.append(title)

    if not tech_titles and not app_titles:
        return ""

    # ── 呼叫 Claude Haiku 仿照範例風格生成摘要 ──────────────────────────────
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)

            tech_part = "\n".join(f"- {t}" for t in tech_titles) or "（無）"
            app_part  = "\n".join(f"- {t}" for t in app_titles)  or "（無）"

            message = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=250,
                system="""\
你是環境部 AI 科技新知報告的編輯助理，負責為每日報告寫一段電子郵件摘要。

【風格範例】（請嚴格模仿此段落結構與語氣）
「這份 2026 年 5 月 3 日的 AI 科技摘要，為同仁解析具備語音、文字與影像處理能力的 Nemotron 3 Nano Omni 新技術。此外，報告分享了三大實務技巧：透過 Gemini 協助分類陳情案卷與報告、利用 NotebookLM 記錄工作 SOP 提升 AI 助理效能，以及將散亂監測數據快速轉為專業 Excel 或簡報。目的幫助同仁減輕重複性文書工作，大幅提升環境行政效能！」

【結構說明】
1. 開頭：「這份 [日期] 的 AI 科技摘要，為同仁解析…（AI 新知主題）。」
2. 中段：「此外，報告分享了三大實務技巧：…（條列 AI 應用，以頓號「、」分隔）。」
3. 結語：「目的幫助同仁減輕重複性文書工作，大幅提升環境行政效能！」（固定句式）

【限制】
- 全文 100 字以內（含標點）
- 台灣繁體中文，禁用中國大陸用語（實時→即時、在線→線上、數據→資料、算法→演算法等）
- 直接輸出段落，不加任何標題、序號或前言""",
                messages=[{
                    "role": "user",
                    "content": (
                        f"今日日期：{today}\n\n"
                        f"AI 新知文章：\n{tech_part}\n\n"
                        f"AI 應用文章：\n{app_part}\n\n"
                        "請依照範例風格生成 100 字以內的摘要。"
                    ),
                }],
            )
            summary = message.content[0].text.strip()
            # 若明顯過長（> 160 字）才截斷，保留完整結語句式
            if len(summary) > 160:
                summary = summary[:158].rstrip("，。、；：！？")
                if summary and summary[-1] not in "。！？":
                    summary += "⋯⋯"
            return summary
        except Exception as e:
            print(f"[send_report] 摘要生成失敗，改用備用格式：{e}")

    # ── 備用：條列標題（截斷至 100 字） ─────────────────────────────────────
    all_titles = tech_titles + app_titles
    summary = "今日重點：\n" + "\n".join(f"・{t}" for t in all_titles)
    if len(summary) > 100:
        summary = summary[:100].rstrip("，。、；：！？・") + "⋯⋯"
    print(f"[send_report] 使用備用摘要格式（{len(summary)} 字）")
    return summary


def send_email(pdf_path: Path) -> None:
    """用 Gmail SMTP（TLS）寄送 PDF 附件，內文含當日報告摘要。"""
    if not GMAIL_APP_PW:
        print("[send_report] 錯誤：.env 中尚未設定 GMAIL_APP_PASSWORD。")
        print("  請參考說明文件取得 Gmail 應用程式密碼後，填入 .env。")
        sys.exit(1)

    # 檔案大小防護：拒絕意外過大的 PDF
    pdf_mb = pdf_path.stat().st_size / (1024 * 1024)
    if pdf_mb > MAX_PDF_MB:
        print(f"[send_report] 錯誤：PDF 大小 {pdf_mb:.1f} MB 超過上限 {MAX_PDF_MB} MB，已取消寄送。")
        sys.exit(1)

    today   = datetime.now(TZ_TAIWAN).strftime("%Y-%m-%d")
    subject = f"【AI 科技新知】{today} 每日摘要報告"

    # 生成當日摘要（100 字以內）
    summary = build_email_summary(today)
    print(f"[send_report] 內文摘要（{len(summary)} 字）：{summary[:30]}…")

    body = f"""\
您好，

附件為 {today} 環境部 AI 科技新知摘要報告（PDF）。

{summary}

詳細內容請見附件 PDF 報告。

本信件由自動化系統寄送，請勿直接回覆。
"""

    msg = MIMEMultipart()
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = GMAIL_RECIPIENT
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with open(pdf_path, "rb") as f:
        attachment = MIMEApplication(f.read(), _subtype="pdf")
        attachment.add_header(
            "Content-Disposition", "attachment",
            filename=pdf_path.name,
        )
        msg.attach(attachment)

    print(f"[send_report] 連線 Gmail SMTP（timeout={SMTP_TIMEOUT_SEC}s）...")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=SMTP_TIMEOUT_SEC) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_SENDER, GMAIL_APP_PW)
        server.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_bytes())

    print(f"[send_report] 已寄送至 {GMAIL_RECIPIENT}（附件：{pdf_path.name}，{pdf_mb:.1f} MB）")


def main():
    parser = argparse.ArgumentParser(description="執行報告並寄送 PDF")
    parser.add_argument("--no-run", action="store_true",
                        help="跳過執行 main.py，直接寄送最新 PDF")
    args = parser.parse_args()

    if not args.no_run:
        run_main()

    pdf_path = find_latest_pdf()
    if not pdf_path:
        print("[send_report] 錯誤：reports/ 目錄中找不到任何 PDF 檔。")
        sys.exit(1)

    print(f"[send_report] 準備寄送：{pdf_path.name}")
    send_email(pdf_path)


if __name__ == "__main__":
    main()
