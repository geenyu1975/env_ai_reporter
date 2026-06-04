# env_ai_reporter

環境部同仁專用的 **AI 科技新知摘要自動化系統**。

每日自動從 13 個 AI 資訊來源抓取文章與影片，透過 Claude AI 進行科普轉譯與實務應用說明，產出 PDF 報告並以電子郵件寄送給指定收件人。

---

## 功能

- **多來源抓取**：13 個 RSS 來源（Hugging Face、Google AI Blog、MIT Tech Review、科技新報、電腦玩物、TechOrange、MR JAMIE、Microsoft AI Blog、VentureBeat、How-To Geek、The Verge、MakeUseOf、WIRED）＋ YouTube 5 頻道
- **持久候選池**：每次抓到的文章加入 `found_articles.json`，跨日累積，不因當日池不足就斷篇
- **Claude Haiku AI 轉譯**：技術論文科普化、環境部工作情境應用指南
- **每日產出** Markdown + PDF 雙格式報告
- **Gmail 自動寄送**，信件內文含 AI 生成的三段式摘要（約 155 字）
- **Windows 工作排程器**每日 10:00 自動執行

---

## 目錄結構

```
env_ai_reporter/
├── src/
│   ├── main.py               # 主流程（抓取→候選池→AI處理→報告→去重）
│   ├── ingestion.py          # 13 來源 RSS 抓取、候選池管理、SSRF 防護
│   ├── ingestion_social.py   # YouTube RSS 抓取、Channel ID 快取
│   ├── processor.py          # Claude Haiku AI 科普轉譯（並行、指數退避）
│   └── reporter.py           # ReportLab PDF + Markdown 報告生成
├── scripts/
│   ├── send_report.py        # 執行主流程並以 Gmail 寄送 PDF
│   ├── run_daily.bat         # Windows 排程批次檔（含 log rotation）
│   └── log_change.py         # 變更記錄工具
├── config/
│   ├── social_targets.json   # YouTube 抓取頻道設定
│   └── youtube_channel_ids.json  # Channel ID 快取（自動維護）
├── data/
│   ├── raw_data.json         # 本次送 Claude 的原始文章（含緩衝篇）
│   ├── processed_data.json   # Claude 處理後資料（截斷至最終篇數）
│   ├── found_articles.json   # 候選池：歷史已找到但尚未刊出的文章
│   └── seen_urls.json        # 已刊出 URL 歷史記錄（30 天自動過期）
├── reports/                  # 輸出報告（YYYY-MM-DD_ai_digest.md / .pdf）
├── .env                      # API 金鑰與 Gmail 設定（勿上傳版本控制）
├── .env.example              # 環境變數範本
└── requirements.txt
```

---

## 資料來源

### AI 技術新知（tech）
| 來源 | 說明 |
|------|------|
| Hugging Face Daily Papers | 每日精選 AI 研究論文 |
| Google AI Blog | Google 官方 AI 技術部落格 |
| MIT Technology Review | 麻省理工學院科技評論 |

### AI 應用技巧（application）
| 來源 | 過濾規則 | 說明 |
|------|---------|------|
| 科技新報 TechNews AI | 正向關鍵字 | 台灣科技媒體 AI 報導 |
| 電腦玩物 Esor | **白名單** | AI 工具實務教學 |
| TechOrange 科技橘報 AI | 負向過濾後直通 | 台灣 AI 應用新聞 |
| MR JAMIE | 正向關鍵字 | 新創 / AI 應用觀點 |
| Microsoft AI Blog | 正向關鍵字 | 微軟 AI 產品應用指南 |
| VentureBeat AI | 正向關鍵字 | 英文 AI 產業應用新聞 |
| How-To Geek | **白名單** | 英文 AI 操作教學 |
| The Verge AI | 正向關鍵字 | 英文 AI 消費應用新聞 |
| MakeUseOf | **白名單** | 英文 AI 操作教學 |
| WIRED AI | 正向關鍵字 | 英文科技雜誌 AI 應用報導 |
| YouTube（5 頻道）| 負向過濾後直通 | Google DeepMind、OpenAI、Google、Microsoft 台灣、NVIDIA 台灣 |

> **白名單**來源（電腦玩物、How-To Geek、MakeUseOf）完全跳過關鍵字過濾，直接通過。
> YouTube 影片描述不足 30 字者自動略過。

---

## 候選池運作邏輯

```
每次執行流程：

  1. 從 13 個來源抓取新文章（AI 關鍵字過濾）
         ↓
  2. 加入 found_articles.json 候選池
     （跳過已在池中或已刊出的文章）
         ↓
  3. 候選池 − seen_urls（已刊出）= 未刊出候選清單
     （按 found_date 升序，較早發現的優先）
         ↓
  4. 選出 1 tech + 3 app（另加 2 篇緩衝）→ 送 Claude
         ↓
  5. Claude 處理後：
     - 成功刊出  → 加入 seen_urls，從候選池移除
     - Claude 拒絕 → fail_count +1；達 3 次自動移出候選池
     - 超過 14 天未刊出 → 候選池自動清除
```

---

## 每日報告規格

- **AI 新知**：1 篇（技術論文科普說明）
- **AI 應用**：3 篇（環境部工作情境操作指南，優先使用 Gemini / NotebookLM / 雅婷逐字稿）
- **語言**：台灣繁體中文，禁用中國大陸慣用語
- **篇數不足時**：僅顯示警告，候選池於後續抓取自動補充（不強制跨類別替補）

---

## 快速開始

```bash
# 1. 安裝相依套件
pip install -r requirements.txt

# 2. 設定環境變數
cp .env.example .env
# 編輯 .env，填入以下項目：
#   ANTHROPIC_API_KEY=...
#   GMAIL_SENDER=your@gmail.com
#   GMAIL_APP_PASSWORD=...       # Gmail 應用程式密碼（16 碼）
#   GMAIL_RECIPIENT=recipient@example.com

# 3. 執行（抓取 + AI 處理 + 產出報告 + 寄信）
python scripts/send_report.py

# 或僅產出報告不寄信
python src/main.py

# 寄送最新報告（不重新執行主流程）
python scripts/send_report.py --no-run

# 進階選項
python src/main.py --limit 5        # 指定 app 篇數（預設 3）
python src/main.py --no-pdf         # 只產 Markdown，不產 PDF
python src/main.py --no-dedup       # 忽略候選池與歷史記錄，直接從本次抓取選篇
python src/main.py --skip-fetch     # 跳過抓取，使用現有 raw_data.json
python src/main.py --skip-process   # 跳過 AI 處理，使用現有 processed_data.json
```

---

## Windows 排程設定

```bat
schtasks /create /tn "env_ai_reporter_daily" /tr "D:\path\to\scripts\run_daily.bat" /sc daily /st 10:00
```

---

## 需求

- Python 3.11+
- Anthropic API Key（必要）
- Gmail 帳號 + 應用程式密碼（必要，用於寄送報告）

---

## 技術備註

### Claude 拒絕偵測（`processor.py`）

`_REFUSAL_MARKERS` 清單涵蓋下列句式，命中任一即視為拒絕（`processed=False`），觸發 `fail_count` 計數：

```
不適合轉譯、超出機關應用範疇、不適合列入、無法轉譯、
沒有直接關聯、不屬於「生成式、建議做法、選項 A / B、
不適合列入「AI 應用、敬請提供適合的文章、
請重新提供、並非生成式 AI
```

> 同時檢查輸出是否包含 `### 標題`/`### 技術核心`/`### 同仁可以怎麼用` 結構區塊；缺少結構也視為失敗。

### Windows 主控台編碼（`ingestion_social.py` + `run_daily.bat`）

YouTube 頻道描述常含 emoji（🧬🤖📊），Windows 預設 cp950 console 無法編碼會拋 `UnicodeEncodeError`，導致整個 `fetch_social()` 提早結束、YouTube 0 篇。

雙重防護：
1. `ingestion_social.py` 模組頂層：`sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
2. `run_daily.bat` 排程執行時：`set PYTHONIOENCODING=utf-8`
