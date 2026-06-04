# 系統架構調整歷史記錄

本文件記錄 `env_ai_reporter` 專案的歷次架構調整與重要程式邏輯變更。
每次對模組介面、資料流、外部服務整合或核心演算法做出重大調整，均需在此追加記錄。

---

## v0.1 — 2026-04-24　專案初始化

**異動模組**：全部（新建）

### 建立基礎目錄結構
- `src/`、`data/`、`reports/`、`config/` 目錄
- `.env.example`：API Key 範本
- `requirements.txt`：requests, feedparser, python-dotenv, anthropic, lxml

### 建立 src/ingestion.py
**資料來源（3 個）**：
| 來源 | 類型 | URL |
|------|------|-----|
| Hugging Face Daily Papers | JSON API | `huggingface.co/api/daily_papers` |
| 科技新報 TechNews AI | RSS | `technews.tw/category/ai/feed/` |
| 電腦玩物 Esor | RSS | `playpcesor.com/feeds/posts/default?alt=rss` |

**關鍵字過濾**：環境、永續、氣候、效率、辦公室、生成式AI、LLM、大型語言模型、人工智慧、AI

**函式介面**：
- `fetch_all(filter_keywords=True)` → `list[dict]`
- `save_raw(items, path)` → 輸出 `data/raw_data.json`

### 建立 src/processor.py
- 模型：`claude-opus-4-7`
- System Prompt：四區塊格式（標題／為什麼重要／一句話看懂／實務技巧）
- **Prompt caching**：`cache_control: {"type": "ephemeral"}` 套用於 system prompt
- **串流輸出**：`client.messages.stream()` + `get_final_message()`，避免 HTTP timeout
- 速率限制自動重試：捕捉 `RateLimitError`，等待 60 秒後重試
- 單篇失敗不中斷整批
- 輸出：`data/processed_data.json`（含 token 統計）

### 建立 CLAUDE.md
- 記錄專案目的、執行方式、架構說明

---

## v0.2 — 2026-04-24　報告輸出模組

**異動模組**：新增 `src/reporter.py`、新增 `src/main.py`

### 新增 src/reporter.py
- `generate_report()` → Markdown 報告（`reports/YYYY-MM-DD_ai_digest.md`）
  - 封面統計表（日期、來源、token 用量）
  - 每篇文章四區塊 claude_output 展開
  - 附錄：失敗清單（若有）
- `generate_pdf()` → PDF 報告（`reports/YYYY-MM-DD_ai_digest.pdf`）
  - 使用 ReportLab 產生，支援繁體中文
  - 字型偵測：Windows（微軟正黑體）、Linux（Noto CJK）、macOS（PingFang）
  - 每頁頂端綠色色條（環境部品牌色 #2e7d32）+ 頁碼
  - 封面統計表、文章色帶標頭、四區塊分段排版

### 新增 src/main.py
- 串接完整流程：ingestion → processor → reporter
- CLI 參數：`--limit`、`--skip-fetch`、`--skip-process`、`--no-pdf`、`--delay`
- 預設執行全部流程

**相依套件新增**：`reportlab>=4.0.0`

---

## v0.3 — 2026-04-25　GitHub Actions 自動化 + 去重機制 + System Prompt 改版

**異動模組**：`src/ingestion.py`、`src/processor.py`、`src/main.py`、`src/reporter.py`、新增 `.github/workflows/daily_report.yml`

### 1. 去重機制（防止每日重複刊出）

**新增 `data/seen_urls.json`**：儲存歷次刊出的文章 URL，格式為 JSON 陣列，排序後存放方便 git diff。

**新增 `src/ingestion.py` 函式**：
- `load_seen_urls(path)` → `set[str]`：載入歷史記錄
- `save_seen_urls(urls, path)`：將 URL 集合寫回 JSON
- `filter_new_items(items, seen_urls)` → `list[dict]`：排除已刊出文章（無 URL 時以標題為鍵值）

**`src/main.py` 流程調整**：
1. 抓取後呼叫 `filter_new_items()` 排除歷史文章
2. 限制篇數（`--limit`，預設改為 **4**）
3. AI 處理完成後，將成功刊出的 URL 追加寫入 `seen_urls.json`
4. 新增 `--no-dedup` 旗標可略過去重（測試用）

### 2. System Prompt 改版

| 項目 | 舊版 | 新版 |
|------|------|------|
| 讀者年齡 | 50 歲以上 | 30–50 歲 |
| 標題規則 | 去掉專有名詞縮寫 | **保留**專有名詞，加中文補充詮釋 |
| 語氣描述 | 正式公文，不拗口 | 同，措辭微調更貼近公務員用語 |

### 3. reporter.py 字型偵測強化

舊版：僅寫死 Windows / 單一 Linux 路徑。
新版：
- `_find_noto_cjk_on_linux()`：glob 搜尋 `/usr/share/fonts/*/noto/` 下所有 CJK ttc/otf
- 分平台（Windows / Darwin / Linux）各自有優先字型清單
- 找不到 CJK 字型時列印警告，退路為 Helvetica，不崩潰

### 4. 新增 GitHub Actions 工作流程

**檔案**：`.github/workflows/daily_report.yml`

| 項目 | 設定 |
|------|------|
| 排程 | `cron: '0 0 * * *'`（UTC 00:00 = 台灣 08:00）|
| 手動觸發 | `workflow_dispatch`（可指定 limit / no_pdf）|
| Runner | `ubuntu-latest` |
| 字型安裝 | `sudo apt-get install -y fonts-noto-cjk` |
| 必要 Secret | `ANTHROPIC_API_KEY` |
| 產出物 | PDF 上傳為 Artifact（保留 30 天）|
| git commit | 自動 commit `data/` + `reports/` + `data/seen_urls.json` 回倉庫 |

### 5. CLAUDE.md 全面更新

重寫為反映現行架構：資料流圖、模組職責表、去重機制說明、PDF 字型偵測順序、GitHub Actions 設定指引。

---

## 記錄規範

新增版本記錄請使用以下格式：

```
## vX.Y — YYYY-MM-DD　<簡短標題>

**異動模組**：列出受影響的檔案

### 1. <變更項目>
- 具體說明：做了什麼、為什麼這樣做
- 介面變更（函式簽名、資料結構、CLI 參數）
- 相容性注意事項

### 2. <變更項目>
...
```

---

## [待補充] 2026-04-25 — 異動：processor.py

> **自動偵測到 `processor.py` 於 2026-04-25 01:21 UTC 被修改。**
> 請在本次 session 結束前補充以下欄位，或執行 `python scripts/log_change.py --fill` 整理。

**異動模組**：`src/processor.py`

### 變更摘要
（請補充：做了什麼、為什麼這樣改）

### 介面變更
（若有函式簽名、CLI 參數、資料結構變動，請列出）

### 注意事項
（相容性、相依套件、部署注意事項）

