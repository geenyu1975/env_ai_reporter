"""
main.py — 一鍵執行完整流程

流程：
  1. ingestion  ─ 抓取 RSS / API → data/raw_data.json
                  比對 data/seen_urls.json，排除已刊出文章
  2. processor  ─ 呼叫 Claude API → data/processed_data.json
  3. reporter   ─ 輸出 Markdown + PDF → reports/YYYY-MM-DD_ai_digest.*
  4. 更新        ─ 將本次刊出 URL 寫入 data/seen_urls.json

用法：
  python src/main.py                     # 全部執行（預設抓 4 篇）
  python src/main.py --limit 10          # 抓取指定篇數
  python src/main.py --skip-fetch        # 跳過抓取（直接用現有 raw_data.json）
  python src/main.py --skip-process      # 跳過 AI 處理（直接用現有 processed_data.json）
  python src/main.py --no-pdf            # 不產生 PDF
  python src/main.py --no-dedup          # 不做去重（忽略歷史記錄）
"""

import argparse
import json
import os
import platform
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

TZ_TAIWAN = timezone(timedelta(hours=8), "CST")  # 台灣標準時間 UTC+8
from pathlib import Path

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))


# ── 安全輔助：.env 檔案權限收緊 ───────────────────────────────────────────────

def _enforce_env_permissions(env_path: Path) -> None:
    """
    確保 .env 不被同主機其他一般使用者讀取。

    Windows 策略：
      - 僅稽核目前 ACL 並印出警告，不主動修改 ACL。
      - 在 Windows 上以 icacls /deny Everyone 封鎖的方式過於激進，
        可能連自身程序都一併鎖死（PermissionError），因此改為只警告。
      - 若需要進一步收緊，請手動執行：
          icacls .env /inheritance:r /grant:r "%USERNAME%":(R)
        （保留 Administrators/SYSTEM 繼承後另行移除 Users/Everyone）

    Unix/macOS 策略：
      - chmod 600，移除 group / other 的任何權限

    失敗時僅警告，不中斷主流程。
    """
    if not env_path.exists():
        return

    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["icacls", str(env_path)],
                capture_output=True, text=True,
            )
            acl_output = result.stdout
            # 若仍有 Everyone DENY 殘留（前次意外設定），自動移除以恢復可用性
            if "Everyone:(DENY)" in acl_output:
                subprocess.run(
                    ["icacls", str(env_path), "/remove:d", "Everyone"],
                    capture_output=True,
                )
                print("[main] 已移除 .env 的 Everyone:(DENY)，恢復正常存取。", file=sys.stderr)
            # 僅警告（不修改），讓使用者自行決定是否收緊
            if "BUILTIN\\Users:(I)(RX)" in acl_output or "Everyone" in acl_output:
                print(
                    "[main] 安全提示：.env 目前對本機所有使用者可讀，"
                    "如需收緊請手動調整 ACL。",
                    file=sys.stderr,
                )
        else:
            current_mode = stat.S_IMODE(env_path.stat().st_mode)
            if current_mode & 0o077:   # group 或 other 有任何權限
                env_path.chmod(0o600)
    except Exception as exc:
        print(f"[main] 警告：無法檢查 .env 權限，請手動確認：{exc}", file=sys.stderr)


# ── 安全輔助：路徑遍歷防護 ────────────────────────────────────────────────────

def _safe_subdir(base: Path, rel: str, var_name: str) -> Path:
    """
    解析相對路徑後確認仍在 base 目錄內，防止環境變數路徑遍歷。
    例如 DATA_OUTPUT_DIR=../../etc 會被擋下。
    """
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        raise SystemExit(
            f"[main] 安全錯誤：{var_name}={rel!r} 解析後超出專案目錄，已中止。"
        )
    return candidate

from ingestion import (
    fetch_all, save_raw, RAW_DATA_PATH,
    load_seen_urls, save_seen_urls, SEEN_PATH,
    load_found_articles, save_found_articles,
    add_to_found_pool, get_candidates,
    FOUND_PATH, MAX_FAIL_COUNT,
    _normalize_url, _is_application_usable,
)
from processor import process_all, RAW_PATH, OUT_PATH
from reporter  import generate_report, generate_pdf, PROCESSED_PATH, REPORTS_DIR


# ── CLI 參數 ──────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="環境部 AI 科技新知摘要自動化流程",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--limit",        type=int, default=4,
                   help="每次最多刊出幾篇文章（預設 4；固定比例：1 技術新知 + (limit-1) 應用技巧）")
    p.add_argument("--skip-fetch",   action="store_true",
                   help="跳過抓取，直接使用現有 raw_data.json")
    p.add_argument("--skip-process", action="store_true",
                   help="跳過 AI 處理，直接使用現有 processed_data.json")
    p.add_argument("--no-pdf",       action="store_true",
                   help="不產生 PDF，只輸出 Markdown")
    p.add_argument("--no-dedup",     action="store_true",
                   help="不做去重，忽略歷史刊出記錄")
    p.add_argument("--delay",        type=float, default=0.5,
                   help="每篇 API 呼叫間隔秒數（預設 0.5）")
    return p.parse_args()


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    args  = _parse_args()
    start = time.time()
    today = datetime.now(TZ_TAIWAN).strftime("%Y-%m-%d")  # 以台灣時間（UTC+8）為基準

    # 啟動安全檢查
    _enforce_env_permissions(BASE_DIR / ".env")
    _safe_subdir(BASE_DIR, os.getenv("DATA_OUTPUT_DIR",   "data"),    "DATA_OUTPUT_DIR")
    _safe_subdir(BASE_DIR, os.getenv("REPORT_OUTPUT_DIR", "reports"), "REPORT_OUTPUT_DIR")

    print("=" * 60)
    print(f"  env_ai_reporter  --  {today}")
    print("=" * 60)

    # 選篇參數（在 fetch 區塊外定義，供 Step 2.5 使用）
    BUFFER     = 2   # 多選緩衝篇數，防止 Claude 拒絕回應後篇數不足
    tech_count = 1
    app_count  = max(1, args.limit - tech_count) if args.limit > 0 else 99

    # ── Step 1：資料抓取 → 加入候選池 → 從候選池選篇 ────────────────────────
    if args.skip_fetch:
        print("\n[Step 1/3] 抓取（略過，使用現有 raw_data.json）")
        if not RAW_DATA_PATH.exists():
            print(f"  [ERROR] 找不到 {RAW_DATA_PATH}，請先執行一次完整流程。")
            sys.exit(1)
    else:
        print("\n[Step 1/3] 抓取資料來源...")
        new_items = fetch_all(filter_keywords=True)

        if args.no_dedup:
            # --no-dedup：直接從本次抓取選篇，完全跳過候選池與歷史去重
            print("  [dedup] 已略過去重（--no-dedup），直接從本次抓取選篇")
            candidates = new_items
        else:
            # 正常模式：
            #   1. 將新文章加入「歷史已找到的新知候選池」
            #   2. 從候選池排除「已刊出」文章，得到未刊出候選清單
            #   3. 從候選清單選篇（tech + app）
            seen_urls = load_seen_urls(SEEN_PATH)
            found     = load_found_articles(FOUND_PATH)

            found, added = add_to_found_pool(new_items, found, seen_urls, today)
            save_found_articles(found, FOUND_PATH)
            print(f"  [found-pool] 本次新增 {added} 篇，候選池共 {len(found)} 篇")

            candidates = get_candidates(found, seen_urls)
            print(f"  [candidates] 尚未刊出候選：{len(candidates)} 篇")

        # 依類別分池，並對 application 篇目套用可操作性過濾
        tech_pool = [it for it in candidates if it.get("category") == "tech"]
        app_pool  = [it for it in candidates
                     if it.get("category") == "application"
                     and _is_application_usable(it)]

        app_all_cnt  = sum(1 for it in candidates if it.get("category") == "application")
        app_excluded = app_all_cnt - len(app_pool)
        if app_excluded:
            print(f"  [app-filter] 排除 {app_excluded} 篇不適用應用技巧的文章（投資/硬體/法規等）")

        selected_tech = tech_pool[:tech_count]
        selected_app  = app_pool[:app_count + BUFFER]

        # 篇數不足時警告（不跨類別強制補充，避免 Claude 拒絕錯誤配對的文章）
        if len(selected_tech) < tech_count:
            print(f"  [WARN] AI新知不足：需要 {tech_count} 篇，候選池僅 {len(tech_pool)} 篇")
        if len(selected_app) < app_count:
            print(f"  [WARN] AI應用不足：需要 {app_count}+{BUFFER} 篇，候選池僅 {len(app_pool)} 篇"
                  f"（候選池將於後續抓取自動累積補充）")

        items = selected_tech + selected_app
        print(f"  [select] AI新知 {len(selected_tech)} 篇 + AI應用 {len(selected_app)} 篇（含 {BUFFER} 篇緩衝）")

        if not items:
            print("  [WARN] 候選池為空，無文章可刊出，流程結束。")
            return

        save_raw(items, path=RAW_DATA_PATH)
        print(f"  本次準備處理：{len(items)} 篇")

    # 用於候選池 fail_count 更新：在 Step 2.5 截斷前保存完整結果（含失敗篇）
    _all_processed_items: list[dict] = []

    # ── Step 2：AI 處理 ───────────────────────────────────────────────────────
    if args.skip_process:
        print("\n[Step 2/3] AI 處理（略過，使用現有 processed_data.json）")
        if not PROCESSED_PATH.exists():
            print(f"  [ERROR] 找不到 {PROCESSED_PATH}，請先執行一次完整流程。")
            sys.exit(1)
    else:
        print("\n[Step 2/3] 呼叫 Claude API 進行科普轉譯...")
        process_all(raw_path=RAW_PATH, out_path=OUT_PATH, delay=args.delay)
        print("  AI 處理完成")

    # ── Step 2.5：過濾失敗/緩衝篇目，修剪至目標篇數（tech_count + app_count）──
    if not args.skip_process and not args.skip_fetch:
        _data    = json.loads(PROCESSED_PATH.read_text(encoding="utf-8"))
        _all     = _data.get("items", [])

        # 截斷前先保存完整清單（含失敗篇），供後續候選池 fail_count 更新使用
        _all_processed_items = _all

        _tech_ok = [it for it in _all if it.get("processed") and it.get("category") == "tech"]
        _app_ok  = [it for it in _all if it.get("processed") and it.get("category") == "application"]
        _trimmed = _tech_ok[:tech_count] + _app_ok[:app_count]

        dropped = len(_all) - len(_trimmed)
        if dropped:
            print(f"  [trim] 過濾失敗/緩衝篇目 {dropped} 篇，最終保留 {len(_trimmed)} 篇")

        # 無條件寫回：確保 processed_data.json 與報告內容一致
        _data["items"] = _trimmed
        _data["total"] = len(_trimmed)
        PROCESSED_PATH.write_text(
            json.dumps(_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 篇數不足警告
        if len(_tech_ok) < tech_count:
            print(f"  [WARN] AI新知不足：需要 {tech_count} 篇，實際 {len(_tech_ok)} 篇")
        if len(_app_ok) < app_count:
            print(f"  [WARN] AI應用不足：需要 {app_count} 篇，實際 {len(_app_ok)} 篇"
                  f"（候選池將於後續抓取自動累積補充）")

    # ── Step 3：產生報告 ──────────────────────────────────────────────────────
    print("\n[Step 3/3] 產生報告...")

    md_path = generate_report(
        processed_path=PROCESSED_PATH,
        reports_dir=REPORTS_DIR,
        report_date=today,
    )

    if args.no_pdf:
        pdf_path = None
        print("  PDF 略過（--no-pdf）")
    else:
        pdf_path = generate_pdf(
            processed_path=PROCESSED_PATH,
            reports_dir=REPORTS_DIR,
            report_date=today,
        )

    # ── 更新 seen_urls（已刊出）+ 候選池（found_articles）────────────────────
    if not args.skip_fetch and not args.no_dedup:
        new_entries: dict[str, str] = {}

        # 使用截斷前的完整清單（含失敗篇），才能正確累計 fail_count
        # 若 skip_process（略過 AI），則讀取現有 processed_data.json 作為備用
        items_for_update = _all_processed_items or json.loads(
            PROCESSED_PATH.read_text(encoding="utf-8")
        ).get("items", [])

        # 重新載入最新的候選池（確保取到先前已儲存的最新狀態）
        found = load_found_articles(FOUND_PATH)

        for it in items_for_update:
            raw_url = it.get("url", "").strip()
            key = _normalize_url(raw_url) if raw_url else it.get("title", "").strip()
            if not key:
                continue

            if it.get("processed"):
                # 成功刊出 → 記入 seen_urls，從候選池移除
                new_entries[key] = today
                found.pop(key, None)
            else:
                # 處理失敗（Claude 拒絕等）→ 累計失敗次數
                if key in found:
                    found[key]["fail_count"] = found[key].get("fail_count", 0) + 1
                    if found[key]["fail_count"] >= MAX_FAIL_COUNT:
                        title_short = it.get("title", "")[:40]
                        print(f"  [found-pool] 移除（連續失敗 {MAX_FAIL_COUNT} 次）：{title_short}")
                        found.pop(key, None)

        updated_seen = {**load_seen_urls(SEEN_PATH), **new_entries}
        save_seen_urls(updated_seen, SEEN_PATH)
        save_found_articles(found, FOUND_PATH)
        print(f"  [dedup] 本次新增 {len(new_entries)} 筆，歷史總計 {len(updated_seen)} 筆")
        print(f"  [found-pool] 候選池剩餘 {len(found)} 篇")

    # ── 完成摘要 ──────────────────────────────────────────────────────────────
    elapsed = time.time() - start
    print()
    print("=" * 60)
    print(f"  [Done]  elapsed {elapsed:.1f}s")
    print(f"  Markdown : {md_path}")
    if pdf_path:
        print(f"  PDF      : {pdf_path}")
    print(f"  seen_urls: {SEEN_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()
