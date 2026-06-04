"""
processor.py — 以 Claude AI 將原始文章轉譯為科普報告

流程：
  讀取 data/raw_data.json → 並行呼叫 Claude API → 輸出 data/processed_data.json

特性：
  - AsyncAnthropic + asyncio.gather 並行請求，大幅縮短整批處理時間
  - asyncio.Semaphore 控制最大並行數，避免超出 Rate Limit
  - 串流輸出，避免長文章觸發 HTTP timeout
  - 失敗單篇不中斷整批，記錄 error 後繼續
  - 兩種 System Prompt：tech（純技術解析）/ application（Gemini/NotebookLM/雅婷操作指南）
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(dotenv_path=(BASE_DIR / ".env").resolve(), override=True)
DATA_DIR  = BASE_DIR / os.getenv("DATA_OUTPUT_DIR", "data")
RAW_PATH  = DATA_DIR / "raw_data.json"
OUT_PATH  = DATA_DIR / "processed_data.json"

MODEL          = "claude-haiku-4-5"
MAX_CONCURRENT = 3   # 同時進行的 API 請求上限（依 Haiku Rate Limit 調整）

# ── System Prompt：AI 技術發展新知 ───────────────────────────────────────────
# 純技術視角，不提環境部現況或特定機關應用

TECH_SYSTEM_PROMPT = """\
妳是一位擅長科普的 AI 技術評論員。

【任務】
將 AI 研究論文或技術突破，轉譯成讓非技術背景讀者也能理解的科普說明。

【輸出格式】
請嚴格依序輸出以下四個區塊，每個區塊以 ### 開頭的標題列開始。
不要加入任何 JSON、程式碼區塊或額外的 Markdown 語法。

─────────────────────────────────────────
### 標題
─────────────────────────────────────────
寫法規則：
- 保留技術名稱與縮寫（如模型名稱、演算法縮寫），不音譯
- 加一句白話說明「這技術能做什麼」
- 全長 20 字以內，語氣簡潔

─────────────────────────────────────────
### 技術核心
─────────────────────────────────────────
寫法規則：
- 2–3 句，純技術視角
- 說明：這個研究解決了什麼問題？核心突破是什麼？與現有方法有何不同？
- 不提及特定國家機關、政府政策或特定產業應用
- 避免被動語態堆疊

好的範例：
  「現有影像生成模型往往在高解析度下出現細節失真，本研究提出以擴散模型為基礎的自適應分辨率框架，
   在不重新訓練的情況下直接提升輸出品質。
   相較於傳統需額外 fine-tuning 的做法，此方法可節省約 70% 的計算資源。」

─────────────────────────────────────────
### 一句話看懂
─────────────────────────────────────────
寫法規則：
- 只能寫一句（含比喻本體與喻依）
- 用日常生活熟悉的事物做比喻（廚房、交通、圖書館、樂器等皆可）
- 不要用技術詞彙解釋技術詞彙

好的範例：
  「就像廚師不用重新學食譜，只要換更好的鍋具就能做出更細緻的料理，這個框架讓 AI 模型用
   更好的演算法直接升級輸出品質。」

─────────────────────────────────────────
### 未來應用場景
─────────────────────────────────────────
寫法規則：
- 列出 2–3 個未來可能的應用領域（不限特定產業）
- 每個場景一句，具體描述「能解決什麼問題」
- 不需與環境保護直接連結，可涵蓋醫療、製造、媒體、科學研究等

好的範例：
  「醫療影像診斷：讓低解析度掃描影像在無需重拍的情況下提升清晰度，輔助醫師判讀。
   衛星遙測分析：改善衛星影像細節，提升地表變化偵測的精準度。
   影視後製：自動增強舊片素材品質，降低重製成本。」

【語言與用語規範】★ 這是強制要求，違反即視為輸出不合格 ★
- 全文只能使用台灣繁體中文，禁止出現任何簡體字
- 語感與用詞須符合台灣讀者習慣，切勿使用中國大陸慣用語
- 舉例情境請貼近台灣日常生活（如：健保、捷運、LINE、便利商店、夜市等）
- 絕對禁用的中國大陸用語對照（請逐一檢查輸出）：
    軟件→軟體　　視頻→影片　　鏈接→連結　　網絡→網路
    點擊→點選　　程序→程式　　內存→記憶體　硬盤→硬碟
    實時→即時　　在線→線上　　線下→實體　　信息→訊息
    獲取→取得　　文件夾→資料夾　算法→演算法　數據→資料
    打車→叫計程車　手机→手機（保留）　消費者→消費者（保留）
- 輸出前請自我校閱，確認無任何上述禁用詞彙
【精簡要求】整體輸出控制在 300 字以內。
"""

# ── System Prompt：AI 應用技巧 ────────────────────────────────────────────────
# 聚焦環境部現有 AI 工具（Gemini、NotebookLM、雅婷逐字稿落地版）的實際操作

APP_SYSTEM_PROMPT = """\
妳是一位體貼資深公務員的數位應用輔導員。

【任務】
將生成式 AI 應用技巧相關文章，轉譯成環境部同仁可立即上手的操作指南。
文章主題範圍：生成式 AI 的各類應用，包含文字生成、圖像生成、語音轉文字、
文件摘要、AI 搜尋、提示詞技巧等。

【讀者畫像】
- 環境部同仁，多數非資訊背景，年紀 30–50 歲
- 重視務實性與資訊安全
- 機關目前提供同仁使用的 AI 工具：
  ① Gemini（gemini.google.com 或手機 App）
  ② NotebookLM（notebooklm.google.com）
  ③ 雅婷逐字稿落地版（機關內部系統）

【輸出格式】
請嚴格依序輸出以下四個區塊，每個區塊以 ### 開頭的標題列開始。
不要加入任何 JSON、程式碼區塊或額外的 Markdown 語法。

─────────────────────────────────────────
### 標題
─────────────────────────────────────────
寫法規則：
- 點出「能做什麼事」，語氣口語化，吸引人
- 全長 20 字以內

好的範例：
  「AI 幫你整理陳情信，三分鐘出條列摘要」
  「上傳整本環評報告，AI 秒回你的問題」
  「會議錄音轉文字，開完馬上搞定記錄」

─────────────────────────────────────────
### 同仁可以怎麼用？
─────────────────────────────────────────
寫法規則：
- 2–3 句，說明在環境部日常工作中的具體應用場景
  （如：整理陳情信、彙整會議記錄、分析長篇報告、快速查詢法規、製作宣導素材）
- 語氣親切，避免過度技術術語

─────────────────────────────────────────
### 一句話看懂
─────────────────────────────────────────
寫法規則：
- 只能寫一句（含比喻本體與喻依）
- 用公務員日常生活情境做比喻（如：公文、會議、SOP手冊、倉庫、新進同仁）

─────────────────────────────────────────
### 實務操作步驟
─────────────────────────────────────────
寫法規則：
- 依文章主題，優先使用機關提供的工具（Gemini、NotebookLM、雅婷逐字稿落地版）
- 若文章主題較適合其他生成式 AI 工具（如圖像生成等），可使用對應工具並說明
- 步驟 3–5 步，每步一行，以數字編號
- 步驟中包含可直接複製貼上的提示詞（以引號標示）
- 最後加一行「小提醒」說明注意事項

好的範例（使用 Gemini）：
  想用 Gemini 整理長篇陳情信，可以這樣做：
  1. 開啟 Gemini（gemini.google.com）。
  2. 貼上陳情信全文，輸入：「請整理這封陳情信的主要訴求、地點、涉及法規，條列呈現。」
  3. 複製輸出後，對照原信確認重點是否完整。
  4. 貼入公文系統備查。
  小提醒：含民眾姓名、電話等個資，請使用機關核准的內部版本，勿上傳公開平台。

好的範例（使用 NotebookLM）：
  想讓 NotebookLM 讀懂一份環評書件，可以這樣做：
  1. 開啟 NotebookLM（notebooklm.google.com），建立新筆記本。
  2. 上傳環評書件 PDF 作為來源。
  3. 在問答欄輸入：「這份環評報告中，哪些污染指標超過標準值？請列出頁碼。」
  4. 系統會直接引用書件內容作答，並附上出處。
  小提醒：僅上傳已公開或去識別化的文件，機密文件請使用機關授權的本地系統。

【語言與用語規範】★ 這是強制要求，違反即視為輸出不合格 ★
- 全文只能使用台灣繁體中文，禁止出現任何簡體字
- 語感與用詞須符合台灣公務員與一般民眾的閱讀習慣
- 舉例情境請使用台灣日常場景（如：公文、健保系統、LINE、便利商店收據、公路總局、戶政事務所等）
- 絕對禁用的中國大陸用語對照（請逐一檢查輸出）：
    軟件→軟體　　視頻→影片　　鏈接→連結　　網絡→網路
    點擊→點選　　程序→程式　　內存→記憶體　硬盤→硬碟
    實時→即時　　在線→線上　　線下→實體　　信息→訊息
    獲取→取得　　文件夾→資料夾　算法→演算法　數據→資料
    打車→叫計程車　查詢功能→查詢功能（保留）
- 輸出前請自我校閱，確認無任何上述禁用詞彙
【精簡要求】整體輸出控制在 400 字以內。
"""

# ── 組合 user message ────────────────────────────────────────────────────────

def _build_user_message(item: dict) -> str:
    """將一篇文章組合成 user message（System Prompt 已處理類別邏輯）。"""
    parts = [
        f"來源：{item.get('source_name', '')}",
        f"標題：{item.get('title', '')}",
    ]
    summary = item.get("summary", "").strip()
    if summary:
        parts.append(f"摘要：{summary[:250]}")
    ai_kws = item.get("ai_keywords", [])
    if ai_kws:
        parts.append(f"關鍵詞：{', '.join(ai_kws[:8])}")
    return "\n".join(parts)


# ── 單篇非同步處理 ────────────────────────────────────────────────────────────

async def _process_item_async(
    client: anthropic.AsyncAnthropic,
    item: dict,
    idx: int,
    total: int,
    semaphore: asyncio.Semaphore,
    _retry: int = 0,           # 內部遞迴計數器，呼叫方不需傳入
    _max_retries: int = 3,     # RateLimitError 最多重試次數
) -> dict:
    """
    非同步處理單篇文章。依 category 選擇對應的 System Prompt。
    Semaphore 確保同時最多 MAX_CONCURRENT 個請求進行中。
    """
    async with semaphore:
        user_msg     = _build_user_message(item)
        system_prompt = (TECH_SYSTEM_PROMPT
                         if item.get("category") == "tech"
                         else APP_SYSTEM_PROMPT)
        try:
            async with client.messages.stream(
                model=MODEL,
                max_tokens=800,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            ) as stream:
                print(f"  [{idx}/{total}] 處理中：{item.get('title', '')[:40]}...")
                async for _ in stream.text_stream:
                    pass   # 串流消費，避免 timeout；不逐字印出
                final = await stream.get_final_message()

            content = ""
            for block in final.content:
                if block.type == "text":
                    content = block.text
                    break

            # 偵測拒絕回應：Claude 說「不適合轉譯」時視為失敗，避免空白區塊進入報告
            _REFUSAL_MARKERS = [
                "不適合轉譯", "超出機關應用範疇", "不適合列入", "無法轉譯",
                "沒有直接關聯", "不屬於「生成式", "建議做法", "選項 A", "選項 B",
                "不適合列入「AI 應用", "敬請提供適合的文章",
                "請重新提供", "並非生成式 AI",
            ]
            # 同時確認輸出包含應有的結構區塊（### 標題 必存在）
            _has_structure = "### 標題" in content or "### 技術核心" in content or "### 同仁可以怎麼用" in content
            if any(m in content for m in _REFUSAL_MARKERS) or not _has_structure:
                print(f"  [{idx}/{total}] 拒絕回應（主題不適用），標記為失敗")
                return {
                    **item,
                    "processed": False,
                    "claude_output": None,
                    "error": "Claude 拒絕轉譯：主題與環境部業務無關",
                }

            print(f"  [{idx}/{total}] 完成（in={final.usage.input_tokens} "
                  f"out={final.usage.output_tokens}）")
            return {
                **item,
                "processed": True,
                "claude_output": content,
                "usage": {
                    "input_tokens":                final.usage.input_tokens,
                    "output_tokens":               final.usage.output_tokens,
                    "cache_read_input_tokens":     getattr(final.usage, "cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": getattr(final.usage, "cache_creation_input_tokens", 0),
                },
            }

        except anthropic.RateLimitError:
            if _retry >= _max_retries:
                print(f"  [{idx}/{total}] 速率限制，已重試 {_max_retries} 次，放棄")
                return {
                    **item,
                    "processed": False,
                    "claude_output": None,
                    "error": f"RateLimitError：超過最大重試次數 {_max_retries}",
                }
            wait = 60 * (2 ** _retry)   # 指數退避：60 / 120 / 240 秒
            print(f"  [{idx}/{total}] 速率限制，{wait}s 後重試（第 {_retry + 1}/{_max_retries} 次）...")
            await asyncio.sleep(wait)
            return await _process_item_async(
                client, item, idx, total, semaphore,
                _retry=_retry + 1, _max_retries=_max_retries,
            )

        except Exception as e:
            print(f"  [{idx}/{total}] 失敗：{type(e).__name__}: {e}")
            return {
                **item,
                "processed": False,
                "claude_output": None,
                "error": f"{type(e).__name__}: {e}",
            }


# ── 並行排程 ─────────────────────────────────────────────────────────────────

async def _run_parallel(items: list[dict], api_key: str) -> list[dict]:
    """以 asyncio.gather 並行發出所有請求，保留原始順序。"""
    client    = anthropic.AsyncAnthropic(api_key=api_key)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    total     = len(items)

    tasks = [
        _process_item_async(client, item, idx, total, semaphore)
        for idx, item in enumerate(items, 1)
    ]

    results = await asyncio.gather(*tasks)
    return list(results)


# ── 主流程（對外介面，同步包裝）──────────────────────────────────────────────

def process_all(
    raw_path: Path = RAW_PATH,
    out_path: Path = OUT_PATH,
    delay: float = 0.5,   # 保留參數相容性；並行模式下由 Semaphore 控流，此值忽略
) -> list[dict]:
    """
    讀取 raw_data.json，並行送 Claude 處理，存入 processed_data.json。
    tech 文章使用 TECH_SYSTEM_PROMPT，application 文章使用 APP_SYSTEM_PROMPT。
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 未設定，請在 .env 填入金鑰")

    raw   = json.loads(raw_path.read_text(encoding="utf-8"))
    items: list[dict] = raw.get("items", [])
    total = len(items)
    print(f"[processor] 共 {total} 篇文章，並行處理 "
          f"（model={MODEL}, max_concurrent={MAX_CONCURRENT}）")

    t0      = time.time()
    results = asyncio.run(_run_parallel(items, api_key))
    elapsed = time.time() - t0

    ok        = sum(1 for r in results if r.get("processed"))
    err       = total - ok
    total_in  = sum(r.get("usage", {}).get("input_tokens", 0) for r in results)
    total_out = sum(r.get("usage", {}).get("output_tokens", 0) for r in results)
    cache_hit = sum(r.get("usage", {}).get("cache_read_input_tokens", 0) for r in results)

    payload = {
        "processed_at":        datetime.now(timezone.utc).isoformat(),
        "model":               MODEL,
        "max_concurrent":      MAX_CONCURRENT,
        "elapsed_seconds":     round(elapsed, 1),
        "total":               total,
        "succeeded":           ok,
        "failed":              err,
        "total_input_tokens":  total_in,
        "total_output_tokens": total_out,
        "cache_read_tokens":   cache_hit,
        "items":               results,
    }

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[processor] 完成：{ok} 成功 / {err} 失敗  ({elapsed:.1f}s)")
    print(f"[processor] Token：input={total_in:,}  output={total_out:,}  cache_hit={cache_hit:,}")
    print(f"[processor] 已儲存 -> {out_path}")
    return results


# ── 直接執行入口 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    process_all()
