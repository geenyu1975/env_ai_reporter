"""
ingestion.py — 新聞與論文抓取模組

來源：
  1.  Hugging Face Daily Papers (JSON API)          — tech
  2.  科技新報 TechNews AI 專欄 (RSS)                — application
  3.  電腦玩物 Esor (RSS)                            — application
  4.  TechOrange 科技橘報 AI (RSS)                   — application
  5.  MR JAMIE (RSS)                                 — application
  6.  Google AI Blog (RSS)                           — tech
  7.  Microsoft AI Blog (RSS)                        — application
  8.  VentureBeat AI (RSS)                           — application
  9.  How-To Geek (RSS)                              — application
  10. The Verge AI (RSS)                             — application
  11. MakeUseOf (RSS)                                — application
  12. WIRED AI (RSS)                                 — application
  13. MIT Technology Review (RSS)                    — tech

流程：fetch → 關鍵字過濾 → 儲存 data/raw_data.json
"""

import ipaddress
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / os.getenv("DATA_OUTPUT_DIR", "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

RAW_DATA_PATH = DATA_DIR / "raw_data.json"

# ── 來源設定 ──────────────────────────────────────────────────────────────────

SOURCES = [
    # ── AI 技術新知（category: tech）────────────────────────────────────────
    {
        "id": "huggingface_daily",
        "name": "Hugging Face Daily Papers",
        "type": "hf_api",
        "url": "https://huggingface.co/api/daily_papers",
        "category": "tech",
    },
    {
        "id": "google_ai_blog",
        "name": "Google AI Blog",
        "type": "rss",
        "url": "https://blog.google/technology/ai/rss/",
        "category": "tech",
    },
    {
        "id": "mit_techreview",
        "name": "MIT Technology Review",
        "type": "rss",
        "url": "https://www.technologyreview.com/feed/",
        "category": "tech",
    },
    # ── AI 應用技巧（category: application）─────────────────────────────────
    {
        "id": "technews_ai",
        "name": "科技新報 TechNews AI",
        "type": "rss",
        "url": "https://technews.tw/category/ai/feed/",
        "category": "application",
    },
    {
        "id": "playpcesor",
        "name": "電腦玩物 Esor",
        "type": "rss",
        "url": "https://www.playpcesor.com/feeds/posts/default?alt=rss",
        "category": "application",
    },
    {
        "id": "buzzorange_ai",
        "name": "TechOrange 科技橘報 AI",
        "type": "rss",
        "url": "https://buzzorange.com/techorange/category/ai/feed/",
        "category": "application",
    },
    {
        "id": "mrjamie",
        "name": "MR JAMIE",
        "type": "rss",
        "url": "https://mrjamie.cc/feed/",
        "category": "application",
    },
    {
        "id": "microsoft_ai_blog",
        "name": "Microsoft AI Blog",
        "type": "rss",
        "url": "https://blogs.microsoft.com/ai/feed/",
        "category": "application",
    },
    {
        "id": "venturebeat_ai",
        "name": "VentureBeat AI",
        "type": "rss",
        "url": "https://venturebeat.com/category/ai/feed/",
        "category": "application",
    },
    {
        "id": "howtogeek",
        "name": "How-To Geek",
        "type": "rss",
        "url": "https://www.howtogeek.com/feed/",
        "category": "application",
    },
    {
        "id": "theverge_ai",
        "name": "The Verge AI",
        "type": "rss",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "category": "application",
    },
    {
        "id": "makeuseof",
        "name": "MakeUseOf",
        "type": "rss",
        "url": "https://www.makeuseof.com/feed/",
        "category": "application",
    },
    {
        "id": "wired_ai",
        "name": "WIRED AI",
        "type": "rss",
        "url": "https://www.wired.com/feed/tag/ai/latest/rss",
        "category": "application",
    },
]

# ── SSRF 防護 ─────────────────────────────────────────────────────────────────

MAX_REDIRECTS = 3   # 重新導向上限，超過視為可疑


def _is_private_host(hostname: str) -> bool:
    """
    判斷 hostname 是否指向私有/保留位址，回傳 True 表示拒絕。
    涵蓋：loopback、私有網段、link-local、APIPA、metadata API 等。
    """
    # 拒絕明確的私有 hostname
    blocked_hosts = {"localhost", "metadata.google.internal"}
    if hostname.lower() in blocked_hosts:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    except ValueError:
        # hostname 為域名（非 IP），讓 DNS 解析後由 OS 處理；
        # 此處無法預先驗證，依賴後續 redirect hook 攔截
        return False


def _assert_safe_url(url: str) -> None:
    """
    驗證 URL 符合安全要求，不安全時拋出 ValueError。
    規則：
      1. scheme 必須為 https 或 http
      2. 不得指向私有 / 保留 IP
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"不允許的 URL scheme：{parsed.scheme!r} ({url})")
    host = parsed.hostname or ""
    if _is_private_host(host):
        raise ValueError(f"拒絕存取私有/保留位址：{host!r} ({url})")


def _redirect_hook(response: requests.Response, *args, **kwargs) -> None:
    """
    requests event hook：每次重新導向前驗證目標 URL。
    阻止重導向至私有網段（防止 SSRF via open redirect）。
    """
    if response.is_redirect:
        location = response.headers.get("Location", "")
        try:
            _assert_safe_url(location)
        except ValueError as exc:
            raise ValueError(f"[SSRF 防護] 重新導向被阻擋：{exc}") from exc


def _safe_get(url: str, **kwargs) -> requests.Response:
    """
    替代 requests.get 的安全版本：
    - 請求前驗證來源 URL
    - 限制重新導向次數（MAX_REDIRECTS），使用 Session 實作
    - 每次重新導向觸發 _redirect_hook 驗證目標
    """
    _assert_safe_url(url)
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    session.hooks["response"].append(_redirect_hook)
    return session.get(url, allow_redirects=True, **kwargs)


# ── HTML 清理 ─────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """
    以 BeautifulSoup 解析 HTML，僅提取純文字。

    使用 html.parser（Python 內建，無額外相依），可正確處理：
    - 嵌套或損壞的標籤結構
    - 屬性中含 > 符號的惡意構造
    - HTML entity（&amp; / &lt; / &nbsp; 等）自動還原
    """
    if not text:
        return text
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(separator=" ", strip=True)


# ── URL 正規化 ────────────────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    """移除 URL 的 Query String 與 Fragment，回傳正規化後的 URL。空字串原樣回傳。"""
    if not url:
        return url
    parsed = urlparse(url.strip())
    return urlunparse(parsed._replace(query="", fragment=""))


# ── 關鍵字過濾 ────────────────────────────────────────────────────────────────

FILTER_KEYWORDS: list[str] = [
    "環境", "永續", "氣候", "效率",
    "辦公室", "生成式 AI", "生成式AI",
    "LLM", "大型語言模型", "人工智慧", "AI",
    # 英文關鍵字（涵蓋 Google Blog / VentureBeat / WIRED 等英文來源）
    "artificial intelligence", "machine learning", "deep learning",
    "chatgpt", "gemini", "copilot", "claude", "openai", "anthropic",
    "large language model", "generative ai",
]

# ── 應用技巧適用性過濾 ────────────────────────────────────────────────────────
# RSS 文章需符合「可用 Gemini / NotebookLM / 雅婷操作」的正向關鍵字，
# 並排除投資、晶片、法規等無法直接操作的新聞類文章。

_APP_POSITIVE: list[str] = [
    # 工具名稱（中文）
    "gemini", "notebooklm", "notebook lm", "雅婷", "逐字稿",
    # 實際操作動詞 / 功能（中文）
    "摘要", "整理", "彙整", "會議記錄", "會議紀錄", "會議摘要",
    "筆記", "筆記整理", "重點整理", "文件分析",
    "撰寫", "改寫", "翻譯", "生成文字", "生成內容",
    "提示詞", "prompt",
    # 教學類（中文）
    "教學", "技巧", "怎麼用", "如何使用", "操作教學", "使用技巧",
    "上手", "入門", "實作", "步驟",
    # 生產力 / 工作流（中文）
    "工作效率", "節省時間", "自動化", "簡化流程",
    # AI 工具名稱（英文）
    "chatgpt", "copilot", "claude", "gpt-4", "gpt-4o", "gpt-",
    "perplexity", "midjourney", "dall-e", "stable diffusion",
    # 教學 / 使用指南（英文）
    "tutorial", "how to", "how-to", "guide", "tips", "tricks",
    "step by step", "step-by-step", "hands-on", "getting started",
    "beginner", "use cases", "practical",
    # 生產力 / 工作流（英文）
    "workflow", "productivity", "automate", "automation",
    "save time", "efficiency",
]

_APP_NEGATIVE: list[str] = [
    # 投資 / 商業新聞
    "億美元", "億元投資", "募資", "估值", "ipo", "上市",
    "收購", "併購", "裁員", "layoff",
    # 硬體 / 基礎設施
    "晶片", "gpu", "處理器", "伺服器", "資料中心", "電費",
    # 法規 / 政策
    "立法", "法案", "監管", "訴訟", "罰款", "違規調查",
    # 純新聞（不含操作內容）
    "財報", "營收", "市占率",
    # 加密貨幣 / 區塊鏈（與環境部業務無關，Claude 拒絕轉譯）
    "比特幣", "加密貨幣", "以太坊", "虛擬貨幣", "幣圈", "nft",
    "錢包解密", "錢包破解", "blockchain", "tokenomics",
    # 純業界動態（無可操作內容）
    "ceo", "執行長", "創辦人", "對決", "pk", "大戰",
]


def _is_application_usable(item: dict) -> bool:
    """
    判斷文章是否適合列入「AI 應用技巧」區塊。

    規則：
    - 電腦玩物（playpcesor）以實用工具教學為主，直接通過
    - 社群媒體來源（YouTube / Threads / Facebook）已鎖定 AI 粉專，直接通過
    - 其他 RSS 來源：需命中至少一個正向關鍵字，且不命中任何負向關鍵字
    """
    sid  = item.get("source_id", "")
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()

    # 專屬教學站台：內容以 AI 工具實務操作為主，無需關鍵字過濾，直接通過
    # （例如：電腦玩物的「上市」指產品發布，而非股票上市，不應被負向關鍵字誤擋）
    _ALWAYS_PASS_SOURCES = {
        "playpcesor",   # 電腦玩物 Esor
        "howtogeek",    # How-To Geek
        "makeuseof",    # MakeUseOf
    }
    if sid in _ALWAYS_PASS_SOURCES:
        return True

    # 其他來源：先過負向關鍵字（加密貨幣、純業界動態等）
    if any(neg in text for neg in _APP_NEGATIVE):
        return False

    # 通過負向過濾後：白名單新聞來源與 YouTube 直接通過
    _TRUSTED_NEWS_SOURCES = {
        "buzzorange_ai",    # TechOrange AI（台灣 AI 應用新聞）
    }
    if sid in _TRUSTED_NEWS_SOURCES:
        return True
    if sid.startswith("youtube_"):
        return True

    # 其他 RSS 來源：需含有可操作的正向關鍵字
    return any(pos in text for pos in _APP_POSITIVE)


def _matches_keywords(title: str, summary: str) -> bool:
    """標題或摘要含任一關鍵字即命中。"""
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in FILTER_KEYWORDS)


# ── 各來源抓取 ────────────────────────────────────────────────────────────────

def _fetch_hf_daily(source: dict) -> list[dict]:
    """抓取 Hugging Face Daily Papers（JSON API）。"""
    try:
        resp = _safe_get(source["url"], timeout=15)
        resp.raise_for_status()
        items = resp.json()
    except Exception as e:
        print(f"[ingestion] {source['name']} 抓取失敗：{e}")
        return []

    results = []
    for item in items:
        paper = item.get("paper", {})
        title = item.get("title") or paper.get("title", "")
        summary = item.get("summary") or paper.get("summary", "")
        paper_id = paper.get("id", "")
        url = f"https://huggingface.co/papers/{paper_id}" if paper_id else ""
        published = item.get("publishedAt") or paper.get("publishedAt", "")

        results.append({
            "source_id": source["id"],
            "source_name": source["name"],
            "category": "tech",          # AI 技術發展新知
            "title": title,
            "summary": summary,
            "url": url,
            "published": published,
            "ai_keywords": paper.get("ai_keywords", []),
        })

    return results


def _fetch_rss(source: dict) -> list[dict]:
    """抓取 RSS Feed。"""
    try:
        _assert_safe_url(source["url"])   # feedparser 使用 urllib，先做 URL 預檢
        feed = feedparser.parse(source["url"])
        if feed.bozo and not feed.entries:
            raise ValueError(f"RSS 解析失敗：{feed.bozo_exception}")
    except Exception as e:
        print(f"[ingestion] {source['name']} 抓取失敗：{e}")
        return []

    results = []
    for entry in feed.entries:
        title   = _strip_html(entry.get("title", ""))
        summary = _strip_html(entry.get("summary", "") or entry.get("description", ""))
        url     = entry.get("link", "")
        published = entry.get("published", "") or entry.get("updated", "")

        results.append({
            "source_id": source["id"],
            "source_name": source["name"],
            "category": source.get("category", "application"),  # 依來源設定決定分類
            "title": title,
            "summary": summary,
            "url": url,
            "published": published,
            "ai_keywords": [],
        })

    return results


# ── 去重（歷史 URL 記錄，自動過期）─────────────────────────────────────────

SEEN_PATH        = DATA_DIR / "seen_urls.json"
SEEN_EXPIRY_DAYS = 30          # 超過此天數的已刊出記錄自動清除

FOUND_PATH             = DATA_DIR / "found_articles.json"
FOUND_ARTICLE_EXPIRY_DAYS = 14  # 候選池文章保留天數（超過視為過期自動清除）
MAX_FAIL_COUNT         = 3      # Claude 拒絕幾次後自動移出候選池


def load_seen_urls(path: Path = SEEN_PATH) -> dict[str, str]:
    """
    載入已刊出的文章記錄，格式：{url: "YYYY-MM-DD"}。
    自動清除超過 SEEN_EXPIRY_DAYS 天的舊記錄。
    檔案不存在或格式不符（舊版陣列）時回傳空字典。
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # 相容舊版陣列格式（升級一次性轉換）
        if isinstance(raw, list):
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            raw = {url: today for url in raw}
        # 清除過期記錄
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_EXPIRY_DAYS)).strftime("%Y-%m-%d")
        active = {url: date for url, date in raw.items() if date >= cutoff}
        expired = len(raw) - len(active)
        if expired:
            print(f"[ingestion] 自動清除 {expired} 筆過期記錄（>{SEEN_EXPIRY_DAYS} 天）")
        return active
    except Exception:
        return {}


def save_seen_urls(seen: dict[str, str], path: Path = SEEN_PATH) -> None:
    """
    將已刊出記錄 {url: date} 寫回 JSON。
    依日期降序、url 升序排序，方便 git diff 閱讀。
    """
    sorted_seen = dict(sorted(seen.items(), key=lambda x: (x[1], x[0]), reverse=True))
    path.write_text(
        json.dumps(sorted_seen, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[ingestion] 已記錄 {len(seen)} 筆歷史 URL → {path}")


# ── 候選池（found_articles.json）────────────────────────────────────────────
# 「歷史已找到的新知資料」：每次抓取的文章加入此池，刊出後移除。
# 候選選篇來源 = 候選池 − 已刊出（seen_urls），確保跨日累積，不因當日池不足就斷篇。


def load_found_articles(path: Path = FOUND_PATH) -> dict[str, dict]:
    """
    載入候選文章池，格式：{normalized_url: article_dict}。
    自動清除超過 FOUND_ARTICLE_EXPIRY_DAYS 天的記錄。
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=FOUND_ARTICLE_EXPIRY_DAYS)
        ).strftime("%Y-%m-%d")
        active  = {url: art for url, art in raw.items()
                   if art.get("found_date", "9999") >= cutoff}
        expired = len(raw) - len(active)
        if expired:
            print(f"[ingestion] 自動清除 {expired} 筆過期候選記錄（>{FOUND_ARTICLE_EXPIRY_DAYS} 天）")
        return active
    except Exception:
        return {}


def save_found_articles(found: dict[str, dict], path: Path = FOUND_PATH) -> None:
    """將候選文章池寫回 JSON，依 found_date 降序排列。"""
    sorted_found = dict(
        sorted(found.items(), key=lambda x: x[1].get("found_date", ""), reverse=True)
    )
    path.write_text(
        json.dumps(sorted_found, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[ingestion] 候選池已儲存 {len(found)} 篇 → {path}")


def add_to_found_pool(
    new_items: list[dict],
    found: dict[str, dict],
    seen: dict[str, str],
    today: str,
) -> tuple[dict[str, dict], int]:
    """
    將新抓到的文章加入候選池。

    規則：
    - 已在候選池中（URL 已存在）→ 略過，保留原有 found_date / fail_count
    - 已刊出（在 seen_urls 中）→ 略過
    - 否則 → 加入，初始化 found_date = today, fail_count = 0

    回傳 (updated_found, added_count)
    """
    seen_keys = set(seen.keys())
    added = 0
    for item in new_items:
        raw_url = item.get("url", "").strip()
        key = _normalize_url(raw_url) if raw_url else item.get("title", "").strip()
        if not key:
            continue
        if key in found or key in seen_keys:
            continue
        found[key] = {**item, "found_date": today, "fail_count": 0}
        added += 1
    return found, added


def get_candidates(
    found: dict[str, dict],
    seen: dict[str, str],
) -> list[dict]:
    """
    從候選池取出尚未刊出的文章列表。
    依 found_date 升序排列（較早發現的優先選用）。
    """
    seen_keys  = set(seen.keys())
    candidates = [art for url, art in found.items() if url not in seen_keys]
    candidates.sort(key=lambda x: x.get("found_date", "9999-99-99"))
    return candidates


def filter_new_items(items: list[dict], seen: dict[str, str]) -> list[dict]:
    """
    過濾掉已出現在歷史記錄中的文章。
    主鍵為正規化後的 URL（移除 query string），無 URL 時以 title 代替。
    """
    before    = len(items)
    seen_keys = set(seen.keys())
    new_items = [
        it for it in items
        if (_normalize_url(it.get("url", "").strip()) or it.get("title", "").strip()) not in seen_keys
    ]
    excluded = before - len(new_items)
    print(f"[ingestion] 去重過濾：{before} → {len(new_items)} 筆（排除 {excluded} 篇已刊出）")
    return new_items


# ── 主流程 ────────────────────────────────────────────────────────────────────

def fetch_all(filter_keywords: bool = True) -> list[dict]:
    """
    從所有來源抓取資料，可選擇是否套用關鍵字過濾。
    來源包含：HuggingFace API、RSS Feed、社群媒體（Playwright）。

    Returns
    -------
    list[dict]：每筆含 source_id, source_name, title, summary, url, published
    """
    all_items: list[dict] = []

    # ── RSS / API 來源 ────────────────────────────────────────────────────────
    for source in SOURCES:
        print(f"[ingestion] 抓取 {source['name']}...")
        if source["type"] == "hf_api":
            items = _fetch_hf_daily(source)
        elif source["type"] == "rss":
            items = _fetch_rss(source)
        else:
            print(f"[ingestion] 未知來源類型：{source['type']}")
            continue
        print(f"[ingestion]   取得 {len(items)} 筆")
        all_items.extend(items)

    # ── 社群媒體來源（Playwright 瀏覽器自動化） ───────────────────────────────
    print("[ingestion] 抓取社群媒體（Playwright）...")
    try:
        from ingestion_social import fetch_social
        social_items = fetch_social()
        # 社群貼文已鎖定 AI 粉專，summary 同樣做 HTML 清理以防萬一
        for it in social_items:
            it["summary"] = _strip_html(it.get("summary", ""))
            it["title"]   = _strip_html(it.get("title",   ""))
        all_items.extend(social_items)
    except Exception as e:
        print(f"[ingestion] 社群媒體抓取略過：{e}")

    # ── 關鍵字過濾（社群來源已針對 AI 粉專，亦套用，過濾非 AI 內容） ──────────
    if filter_keywords:
        before = len(all_items)
        all_items = [
            it for it in all_items
            if _matches_keywords(it["title"], it["summary"])
        ]
        print(f"[ingestion] 關鍵字過濾：{before} → {len(all_items)} 筆")

    return all_items


def save_raw(items: list[dict], path: Path = RAW_DATA_PATH) -> Path:
    """將抓取結果儲存為 JSON。"""
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total": len(items),
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ingestion] 已儲存 {len(items)} 筆 → {path}")
    return path


# ── 直接執行入口 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    items = fetch_all(filter_keywords=True)
    save_raw(items)
