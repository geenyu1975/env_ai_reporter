"""
ingestion_social.py — YouTube 頻道 RSS 抓取

使用 YouTube 官方 Atom RSS Feed（無需瀏覽器，穩定可靠）：
  https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID

頻道 ID 快取於 config/youtube_channel_ids.json。
首次執行時自動由 @handle 解析 channel_id 並存入快取，後續直接使用。

目標設定：config/social_targets.json
"""

import json
import re
import sys
from pathlib import Path

import feedparser
import requests

# Windows 預設 console 編碼（cp950/gbk）無法輸出 emoji；統一改為 UTF-8 並以 ? 取代無法編碼的字元
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR       = Path(__file__).parent.parent
CONFIG_PATH    = BASE_DIR / "config" / "social_targets.json"
ID_CACHE_PATH  = BASE_DIR / "config" / "youtube_channel_ids.json"

MAX_VIDEOS      = 5     # 每個頻道最多取幾則
FETCH_TIMEOUT   = 15    # HTTP 逾時（秒）
MIN_SUMMARY_LEN = 30    # 影片描述低於此字數視為無實質內容，略過不納入

# YouTube channel ID 格式：UC 開頭，共 24 字元
_CHANNEL_ID_RE = re.compile(r"UC[a-zA-Z0-9_-]{22}")
# 帳號 handle 合法字元（防止 config 值被注入 URL）
_SAFE_HANDLE_RE = re.compile(r"^[\w\-.]{1,64}$")


# ── 設定與快取 ────────────────────────────────────────────────────────────────

def _load_targets() -> dict:
    if not CONFIG_PATH.exists():
        return {"youtube": []}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[social] 設定檔讀取失敗：{e}")
        return {"youtube": []}


def _load_id_cache() -> dict[str, str]:
    if not ID_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(ID_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_id_cache(cache: dict[str, str]) -> None:
    ID_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Channel ID 解析 ───────────────────────────────────────────────────────────

def _resolve_channel_id(handle: str) -> str | None:
    """
    以 HTTP GET 抓取 youtube.com/@handle 頁面，
    從 HTML 中擷取 channelId（UC... 格式，24 字元）。
    解析失敗回傳 None。
    """
    url = f"https://www.youtube.com/@{handle}"
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT,
                            headers={"Accept-Language": "zh-TW,zh;q=0.9"})
        resp.raise_for_status()
        # 嘗試多種出現位置
        patterns = [
            r'"channelId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"',
            r'youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})',
            r'"externalChannelId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"',
        ]
        for pat in patterns:
            m = re.search(pat, resp.text)
            if m and _CHANNEL_ID_RE.fullmatch(m.group(1)):
                return m.group(1)
    except Exception as e:
        print(f"[social] @{handle} 頻道 ID 解析失敗：{e}")
    return None


# ── YouTube RSS 抓取 ──────────────────────────────────────────────────────────

def _fetch_youtube_rss(info: dict, id_cache: dict[str, str]) -> list[dict]:
    """
    以 feedparser 抓取 YouTube RSS Feed，回傳與現有 SOURCES 格式相容的 dict 清單。
    """
    raw_channel = info.get("channel", "").lstrip("@")
    name        = info.get("name", raw_channel)

    if not _SAFE_HANDLE_RE.match(raw_channel):
        print(f"[social] 略過不合法的 channel handle：{raw_channel!r}")
        return []

    # ── 取得 Channel ID（快取優先） ───────────────────────────────────────────
    channel_id = id_cache.get(raw_channel)
    if not channel_id:
        print(f"[social] 解析 @{raw_channel} 頻道 ID（首次執行）...")
        channel_id = _resolve_channel_id(raw_channel)
        if not channel_id:
            print(f"[social] @{raw_channel}：無法取得頻道 ID，略過。")
            return []
        id_cache[raw_channel] = channel_id
        print(f"[social] @{raw_channel} → {channel_id}（已存入快取）")

    # ── 抓取 RSS ──────────────────────────────────────────────────────────────
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        feed = feedparser.parse(rss_url)
        if feed.bozo and not feed.entries:
            raise ValueError(f"RSS 解析失敗：{feed.bozo_exception}")
    except Exception as e:
        print(f"[social] YouTube @{raw_channel} RSS 失敗：{e}")
        return []

    results = []
    for entry in feed.entries[:MAX_VIDEOS]:
        title    = entry.get("title", "").strip()
        url      = entry.get("link", "")
        pub_date = entry.get("published", "") or entry.get("updated", "")

        # media:description 儲存在 media_group > media_description
        summary = ""
        media_group = entry.get("media_group", {})
        if media_group:
            summary = media_group.get("media_description", [{}])
            if isinstance(summary, list) and summary:
                summary = summary[0].get("value", "")
            elif isinstance(summary, str):
                pass
            else:
                summary = ""
        if not summary:
            summary = entry.get("summary", "") or entry.get("description", "")
        summary = summary.strip()[:500]

        if not title:
            continue

        # 摘要門檻：描述不足 MIN_SUMMARY_LEN 字視為無實質內容，略過
        if len(summary) < MIN_SUMMARY_LEN:
            print(f"[social] 略過（摘要不足 {MIN_SUMMARY_LEN} 字）：{title[:40]}")
            continue

        results.append({
            "source_id":   f"youtube_{raw_channel.lower()}",
            "source_name": f"YouTube｜{name}",
            "category":    "application",
            "title":       title,
            "summary":     summary,
            "url":         url,
            "published":   pub_date,
            "ai_keywords": [],
        })

    return results


# ── 主流程（同步，供 ingestion.py 呼叫） ─────────────────────────────────────

def fetch_social() -> list[dict]:
    """
    抓取所有 YouTube 頻道的最新影片，回傳統一格式的 dict 清單。
    """
    targets   = _load_targets()
    id_cache  = _load_id_cache()
    all_items: list[dict] = []

    for info in targets.get("youtube", []):
        print(f"[social] YouTube：{info.get('name', '')} ...")
        items = _fetch_youtube_rss(info, id_cache)
        print(f"[social]   取得 {len(items)} 則")
        all_items.extend(items)

    # 儲存更新後的快取（若有新解析的 ID）
    _save_id_cache(id_cache)

    print(f"[social] YouTube 共取得 {len(all_items)} 則")
    return all_items
