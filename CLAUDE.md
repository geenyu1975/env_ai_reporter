# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案目的

環境部同仁專用的 AI 科普新知自動化系統。每日自動爬取 AI 相關新聞，透過 Claude AI 轉譯為科普報告，輸出 Markdown 與 PDF 格式，並由 GitHub Actions 定時執行、commit 回倉庫。

---

## 快速執行

```bash
# 安裝相依套件
pip install -r requirements.txt

# 設定 API Key（首次使用）
cp .env.example .env
# 填入 ANTHROPIC_API_KEY

# 一鍵執行完整流程（預設抓 4 篇、去重、產生 MD + PDF）
python src/main.py

# 常用選項
python src/main.py --limit 10       # 抓取指定篇數
python src/main.py --no-pdf         # 只產 Markdown
python src/main.py --skip-fetch     # 跳過抓取（用現有 raw_data.json）
python src/main.py --skip-process   # 跳過 AI 處理（用現有 processed_data.json）
python src/main.py --no-dedup       # 不做歷史去重

# 單獨執行各模組
python src/ingestion.py   # 只抓取 → raw_data.json
python src/processor.py   # 只做 AI 處理 → processed_data.json
python src/reporter.py    # 只產報告（MD + PDF）
```

---

## 環境變數（.env）

| 變數名稱 | 必要 | 說明 |
|---------|------|------|
| `ANTHROPIC_API_KEY` | **是** | Claude AI 科普轉譯用 |
| `REPORT_OUTPUT_DIR` | 否 | 報告輸出目錄，預設 `reports/` |
| `DATA_OUTPUT_DIR` | 否 | 原始資料目錄，預設 `data/` |

> `.env` 使用 `load_dotenv(dotenv_path=(BASE_DIR / ".env").resolve(), override=True)` 載入，確保在任何工作目錄下均可正確讀取。

---

## 系統架構

### 資料流

```
RSS / HF API
    │
    ▼
ingestion.py ──── 關鍵字過濾 ──── 歷史去重（seen_urls.json）
    │                                      │
    ▼                                      ▼
raw_data.json                    限制篇數（預設 4）
    │
    ▼
processor.py ──── Claude API（claude-opus-4-7）──── prompt caching
    │
    ▼
processed_data.json
    │
    ▼
reporter.py ──┬── Markdown（reports/YYYY-MM-DD_ai_digest.md）
              └── PDF      （reports/YYYY-MM-DD_ai_digest.pdf）
    │
    ▼
seen_urls.json ← 更新歷史記錄（防止隔日重複）
```

### 模組職責

| 模組 | 職責 |
|------|------|
| `src/ingestion.py` | 抓取 HF Daily Papers / TechNews / 電腦玩物；關鍵字過濾；歷史去重（`seen_urls.json`）|
| `src/processor.py` | 呼叫 Claude API，將文章轉譯為四區塊科普報告；prompt caching；串流輸出 |
| `src/reporter.py` | 輸出 Markdown 報告；用 ReportLab 產生 PDF（自動偵測系統字型）|
| `src/main.py` | 串接完整流程；CLI 參數控制；更新 `seen_urls.json` |
| `.github/workflows/daily_report.yml` | 每天 UTC 00:00（台灣 08:00）自動執行、commit 報告 |

### 去重機制（seen_urls.json）

- 每次執行後，成功刊出的文章 URL 會追加寫入 `data/seen_urls.json`
- 下次抓取時自動排除已刊出的 URL（無 URL 時以標題為鍵值）
- 此檔案隨報告一起 commit 回倉庫，跨次執行持久保存

### Claude API 設計

- 模型：`claude-opus-4-7`
- System Prompt 加上 `cache_control: {"type": "ephemeral"}` 做 prompt caching，批次處理時節省 token
- 使用 `client.messages.stream()` + `get_final_message()` 串流避免 HTTP timeout
- `RateLimitError` 自動等待 60 秒後重試
- 單篇失敗不中斷整批，錯誤記錄在 `processed_data.json` 的 `error` 欄位

### PDF 字型偵測順序

| 環境 | 字型 |
|------|------|
| Windows | 微軟正黑體（msjh.ttc）→ 新細明體 → 微軟雅黑 |
| Linux / GitHub Actions | Noto Sans CJK（apt: fonts-noto-cjk）|
| macOS | PingFang.ttc |
| 無 CJK 字型 | 退路：Helvetica（中文顯示異常但不崩潰）|

---

## 重要檔案位置

```
env_ai_reporter/
├── src/
│   ├── main.py           # 主入口（CLI）
│   ├── ingestion.py      # 抓取 + 去重
│   ├── processor.py      # Claude API 轉譯
│   └── reporter.py       # MD + PDF 輸出
├── data/
│   ├── raw_data.json         # 抓取結果（每次覆寫）
│   ├── processed_data.json   # AI 處理結果（每次覆寫）
│   └── seen_urls.json        # 歷史刊出記錄（累積）
├── reports/
│   └── YYYY-MM-DD_ai_digest.{md,pdf}
├── docs/
│   └── architecture_history.md   # 系統架構調整歷史記錄
├── .github/workflows/
│   └── daily_report.yml      # GitHub Actions 排程
├── .env                      # API Key（不 commit）
└── requirements.txt
```

---

## GitHub Actions 設定

1. 在 GitHub 倉庫 `Settings → Secrets → Actions` 新增 Secret：
   - `ANTHROPIC_API_KEY`：Anthropic API 金鑰
2. 工作流程會自動：安裝 Noto CJK 字型 → 執行 main.py → commit 報告 → 上傳 PDF artifact
3. 手動觸發：Actions 頁面 → `Daily AI Digest` → `Run workflow`

---

## 架構調整規範

每次對系統架構或程式邏輯做重大調整，**必須同步更新** `docs/architecture_history.md`，記錄：
- 調整日期
- 異動模組
- 變更摘要（做了什麼、為什麼）
