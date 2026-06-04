"""
log_change.py — 自動將 src/*.py 變更記錄追加至 docs/architecture_history.md

用法（由 Claude Code hook 呼叫）：
  echo '{"tool_input": {"file_path": "...\\src\\processor.py"}}' | python scripts/log_change.py

邏輯：
  1. 從 stdin 讀取 JSON（hook 傳入）
  2. 取出 file_path，確認屬於 src/*.py
  3. 在 docs/architecture_history.md 末尾追加一筆「待補充」記錄
  4. 同一天同一檔案不重複追加（防止每次小編輯都寫入）
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent.parent
HISTORY_PATH = BASE_DIR / "docs" / "architecture_history.md"


def _safe_resolve(file_path: str) -> Path | None:
    """
    解析輸入路徑並確認它是存在的真實檔案。
    回傳 None 表示路徑無效或不可信（不存在、指向目錄）。

    注意：log_change.py 只使用 p.name，不讀寫 file_path 本身；
    此函式的目的是攔截格式異常或刻意構造的路徑輸入。
    """
    try:
        resolved = Path(file_path).resolve()
        if not resolved.is_file():
            return None
        return resolved
    except (OSError, ValueError):
        return None


def is_src_py(file_path: str) -> bool:
    """判斷路徑是否為 src/ 下的 .py 檔（相容 Windows / Unix 路徑）。"""
    p = Path(file_path)
    parts = [part.lower() for part in p.parts]
    return p.suffix == ".py" and "src" in parts


def already_logged_today(content: str, today: str, file_name: str) -> bool:
    """同一天同一檔案已有未補充的 pending 記錄，不重複追加。"""
    pattern = rf"## \[待補充\].*{today}.*{re.escape(file_name)}"
    return bool(re.search(pattern, content))


def append_pending_entry(file_path: str) -> None:
    p        = Path(file_path)
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rel_path = p.name   # 只取檔名，避免洩漏完整本機路徑

    # 讀取現有內容
    if HISTORY_PATH.exists():
        content = HISTORY_PATH.read_text(encoding="utf-8")
    else:
        content = ""

    # 同天同檔案已記錄則跳過
    if already_logged_today(content, today, rel_path):
        print(f"[log_change] {rel_path} 今日已有記錄，略過", file=sys.stderr)
        return

    entry = f"""
---

## [待補充] {today} — 異動：{rel_path}

> **自動偵測到 `{rel_path}` 於 {now_str} 被修改。**
> 請在本次 session 結束前補充以下欄位，或執行 `python scripts/log_change.py --fill` 整理。

**異動模組**：`src/{rel_path}`

### 變更摘要
（請補充：做了什麼、為什麼這樣改）

### 介面變更
（若有函式簽名、CLI 參數、資料結構變動，請列出）

### 注意事項
（相容性、相依套件、部署注意事項）

"""

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(entry)

    print(f"[log_change] 已追加待填記錄：{rel_path} @ {today}", file=sys.stderr)


def main() -> None:
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return
        data      = json.loads(raw)
        file_path = data.get("tool_input", {}).get("file_path", "")
        if not file_path:
            return
        if _safe_resolve(file_path) is None:
            print(f"[log_change] 路徑無效或不存在，略過：{file_path!r}", file=sys.stderr)
            return
        if not is_src_py(file_path):
            return
        append_pending_entry(file_path)
    except Exception as e:
        # Hook 不能讓主流程崩潰
        print(f"[log_change] 錯誤（略過）：{e}", file=sys.stderr)


if __name__ == "__main__":
    main()
