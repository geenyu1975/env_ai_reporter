"""
reporter.py — 將 processed_data.json 轉為 Markdown 與 PDF 報告

輸出格式：
  reports/YYYY-MM-DD_ai_digest.md
  reports/YYYY-MM-DD_ai_digest.pdf

報告結構：
  封面摘要（日期、來源統計、token 用量）
  ─ 各篇文章科普報告（claude_output 四區塊）
  ─ 附錄：處理失敗清單（若有）
"""

import html
import json
import os
import platform
import re
from datetime import datetime, timedelta, timezone

TZ_TAIWAN = timezone(timedelta(hours=8), "CST")
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(dotenv_path=(BASE_DIR / ".env").resolve(), override=True)

DATA_DIR       = BASE_DIR / os.getenv("DATA_OUTPUT_DIR", "data")
REPORTS_DIR    = BASE_DIR / os.getenv("REPORT_OUTPUT_DIR", "reports")
PROCESSED_PATH = DATA_DIR / "processed_data.json"
FONTS_DIR      = BASE_DIR / "assets" / "fonts"   # 專案內建字型目錄

REPORTS_DIR.mkdir(parents=True, exist_ok=True)
FONTS_DIR.mkdir(parents=True, exist_ok=True)


# ── 字型設定（ReportLab 繁體中文）────────────────────────────────────────────

def _find_project_fonts() -> list[tuple[Path, Path]]:
    """
    優先搜尋專案目錄 assets/fonts/ 下的 CJK 字型。
    使用相對路徑，不依賴系統安裝位置，適合容器化環境（GitHub Actions 等）。
    支援 .ttc / .otf / .ttf 格式；Regular + Bold 分開配對，無 Bold 時以 Regular 代替。
    """
    if not FONTS_DIR.exists():
        return []

    regs  = (sorted(FONTS_DIR.glob("*[Rr]egular*.ttc"))
             + sorted(FONTS_DIR.glob("*[Rr]egular*.otf"))
             + sorted(FONTS_DIR.glob("*[Rr]egular*.ttf")))
    bolds = (sorted(FONTS_DIR.glob("*[Bb]old*.ttc"))
             + sorted(FONTS_DIR.glob("*[Bb]old*.otf"))
             + sorted(FONTS_DIR.glob("*[Bb]old*.ttf")))

    if not regs:
        # 若無 Regular/Bold 區分，直接取全部字型檔
        all_fonts = (sorted(FONTS_DIR.glob("*.ttc"))
                     + sorted(FONTS_DIR.glob("*.otf"))
                     + sorted(FONTS_DIR.glob("*.ttf")))
        if all_fonts:
            return [(all_fonts[0], all_fonts[0])]
        return []

    bold = bolds[0] if bolds else regs[0]
    return [(regs[0], bold)]


def _find_noto_cjk_on_linux() -> list[tuple[Path, Path]]:
    """在 Linux 上搜尋 Noto CJK 字型（apt install fonts-noto-cjk 安裝後）。"""
    search_dirs = [
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/noto"),
        Path("/usr/local/share/fonts"),
    ]
    candidates: list[tuple[Path, Path]] = []
    for d in search_dirs:
        if not d.exists():
            continue
        regs  = sorted(d.glob("*CJK*Regular*.ttc")) + sorted(d.glob("*CJK*Regular*.otf"))
        bolds = sorted(d.glob("*CJK*Bold*.ttc"))    + sorted(d.glob("*CJK*Bold*.otf"))
        if regs:
            bold = bolds[0] if bolds else regs[0]
            candidates.append((regs[0], bold))
    return candidates


def _register_cjk_font() -> tuple[str, str]:
    """
    在 ReportLab 中註冊一組繁體中文字型（Regular + Bold）。
    回傳 (regular_name, bold_name)。

    搜尋優先順序：
      1. assets/fonts/（專案內建，適合容器化 CI 環境）
      2. 系統字型（Windows: 微軟正黑體 / macOS: PingFang / Linux: Noto CJK）
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    sys_name = platform.system()

    # ── 1. 專案內建字型（最高優先，路徑固定、無權限問題）────────────────────
    project_candidates = _find_project_fonts()

    # ── 2. 系統字型（各平台備援）────────────────────────────────────────────
    if sys_name == "Windows":
        win_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        system_candidates: list[tuple[Path, Path]] = [
            (win_dir / "msjh.ttc",   win_dir / "msjhbd.ttc"),   # 微軟正黑體
            (win_dir / "mingliu.ttc", win_dir / "mingliu.ttc"),  # 新細明體
            (win_dir / "kaiu.ttf",    win_dir / "kaiu.ttf"),     # 標楷體
            (win_dir / "msyh.ttc",    win_dir / "msyhbd.ttc"),   # 微軟雅黑
            (win_dir / "simsun.ttc",  win_dir / "simsun.ttc"),   # 新宋體
        ]
    elif sys_name == "Darwin":
        system_candidates = [
            (Path("/System/Library/Fonts/PingFang.ttc"),
             Path("/System/Library/Fonts/PingFang.ttc")),
            (Path("/Library/Fonts/Arial Unicode MS.ttf"),
             Path("/Library/Fonts/Arial Unicode MS.ttf")),
        ] + _find_noto_cjk_on_linux()
    else:  # Linux / GitHub Actions（系統路徑備援，assets/fonts/ 已優先）
        system_candidates = _find_noto_cjk_on_linux() + [
            (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
             Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        ]

    all_candidates = project_candidates + system_candidates

    for reg_path, bold_path in all_candidates:
        if not reg_path.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("CJK-Regular", str(reg_path)))
            if bold_path.exists() and bold_path != reg_path:
                pdfmetrics.registerFont(TTFont("CJK-Bold", str(bold_path)))
                bold_name = "CJK-Bold"
            else:
                bold_name = "CJK-Regular"
            print(f"[reporter] 字型：{reg_path.name}")
            return "CJK-Regular", bold_name
        except Exception:
            continue

    # 最終退路（不支援中文但不崩潰）
    print("[reporter] 警告：找不到 CJK 字型，PDF 中文可能顯示異常")
    return "Helvetica", "Helvetica-Bold"


# ── 安全輔助函式 ─────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """逸出 XML 特殊字元（&  <  >  "  '），供 ReportLab Paragraph 安全使用。"""
    return html.escape(str(text), quote=True)


def _safe_url(url: str) -> str:
    """
    驗證並清理 URL，確保可安全嵌入 ReportLab <link href="...">。
    白名單：僅允許 http / https scheme；其他一律回傳空字串。
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return ""
    return html.escape(url, quote=True)


def _safe_para(text: str, style: "ParagraphStyle") -> "Paragraph":
    """
    唯一允許使用的 Paragraph 工廠函式（純文字路徑）。

    所有來自外部或 AI 的字串必須經此函式建立 Paragraph，
    內部強制呼叫 _esc()，防止 ReportLab XML 標籤注入。

    若需要刻意使用 ReportLab XML（如 <link>），
    必須改用 _xml_para()，並在 call site 明確標示。
    """
    from reportlab.platypus import Paragraph as _Para
    return _Para(_esc(text), style)


def _xml_para(pre_escaped_xml: str, style: "ParagraphStyle") -> "Paragraph":
    """
    含刻意 ReportLab XML 標籤的 Paragraph 工廠（如 <link href="...">）。

    呼叫者須自行確保：
      - 所有純文字片段已透過 _esc() 逸出
      - URL 已透過 _safe_url() 驗證
    此函式不做額外逸出，只作為「明確使用 XML」的呼叫標記。
    """
    from reportlab.platypus import Paragraph as _Para
    return _Para(pre_escaped_xml, style)


# ── Markdown 輔助函式 ─────────────────────────────────────────────────────────

SOURCE_ICONS = {
    # ── AI 技術新知來源 ────────────────────────────────────────────────────────
    "huggingface_daily": "【HF Daily Papers】",
    "google_ai_blog":    "【Google AI Blog】",
    "mit_techreview":    "【MIT Tech Review】",
    # ── AI 應用技巧來源（中文）──────────────────────────────────────────────────
    "technews_ai":       "【科技新報】",
    "playpcesor":        "【電腦玩物】",
    "buzzorange_ai":     "【科技橘報】",
    "mrjamie":           "【MR JAMIE】",
    # ── AI 應用技巧來源（英文）──────────────────────────────────────────────────
    "microsoft_ai_blog": "【Microsoft AI】",
    "venturebeat_ai":    "【VentureBeat】",
    "howtogeek":         "【How-To Geek】",
    "theverge_ai":       "【The Verge】",
    "makeuseof":         "【MakeUseOf】",
    "wired_ai":          "【WIRED】",
    "_youtube":          "【YouTube】",   # 前綴比對
}
MD_ICONS = {
    # ── AI 技術新知來源 ────────────────────────────────────────────────────────
    "huggingface_daily": "🤗",
    "google_ai_blog":    "🔵",
    "mit_techreview":    "🎓",
    # ── AI 應用技巧來源（中文）──────────────────────────────────────────────────
    "technews_ai":       "📰",
    "playpcesor":        "💻",
    "buzzorange_ai":     "🍊",
    "mrjamie":           "📝",
    # ── AI 應用技巧來源（英文）──────────────────────────────────────────────────
    "microsoft_ai_blog": "🪟",
    "venturebeat_ai":    "📊",
    "howtogeek":         "🛠️",
    "theverge_ai":       "🔷",
    "makeuseof":         "📌",
    "wired_ai":          "⚡",
    "_youtube":          "▶️",           # 前綴比對
}


def _source_label(source_id: str, source_name: str, *, for_pdf: bool = False) -> str:
    icons = SOURCE_ICONS if for_pdf else MD_ICONS
    default = "" if for_pdf else "🔗"

    # 精確比對
    icon = icons.get(source_id)
    # 前綴比對（社群媒體 youtube_xxx / threads_xxx / facebook_xxx）
    if icon is None:
        for prefix in ("youtube_", "threads_", "facebook_"):
            if source_id.startswith(prefix):
                icon = icons.get(f"_{prefix.rstrip('_')}", default)
                break
    icon = icon if icon is not None else default

    return f"{icon} {source_name}".strip()


def _format_date(iso_str: str) -> str:
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",   "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ):
        try:
            return datetime.strptime(iso_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return iso_str[:10] if iso_str else ""


def _build_article_block(item: dict, idx: int) -> str:
    """單篇文章的 Markdown 區塊。"""
    title    = item.get("title", "（無標題）")
    url      = item.get("url", "")
    pub_date = _format_date(item.get("published", ""))
    source   = _source_label(item.get("source_id", ""), item.get("source_name", ""))
    output   = item.get("claude_output", "").strip()

    meta_parts = [f"**來源**：{source}"]
    if pub_date:
        meta_parts.append(f"**發布日期**：{pub_date}")
    if url:
        meta_parts.append(f"**原文**：[閱讀全文]({url})")

    return "\n".join([
        f"## 📄 文章 {idx}",
        "",
        f"> 原文標題：{title}",
        "",
        "　｜　".join(meta_parts),
        "",
        "---",
        "",
        output,
        "",
    ])


def _build_failed_block(failed_items: list[dict]) -> str:
    if not failed_items:
        return ""
    lines = ["---", "", "## ⚠️ 附錄：處理失敗清單", ""]
    for item in failed_items:
        title = item.get("title", "（無標題）")
        err   = item.get("error", "未知錯誤")
        url   = item.get("url", "")
        link  = f" — [原文]({url})" if url else ""
        lines += [f"- **{title}**{link}", f"  - 錯誤：`{err}`"]
    lines.append("")
    return "\n".join(lines)


# ── Markdown 報告 ─────────────────────────────────────────────────────────────

def generate_report(
    processed_path: Path = PROCESSED_PATH,
    reports_dir: Path = REPORTS_DIR,
    report_date: str | None = None,
) -> Path:
    """讀取 processed_data.json，輸出 Markdown 報告。"""
    data = json.loads(processed_path.read_text(encoding="utf-8"))

    today      = report_date or datetime.now(TZ_TAIWAN).strftime("%Y-%m-%d")  # 台灣時間
    model      = data.get("model", "")
    total      = data.get("total", 0)
    succeeded  = data.get("succeeded", 0)
    failed_cnt = data.get("failed", 0)
    total_in   = data.get("total_input_tokens", 0)
    total_out  = data.get("total_output_tokens", 0)
    cache_hit  = data.get("cache_read_tokens", 0)
    items      = data.get("items", [])

    succeeded_items = [it for it in items if it.get("processed")]
    failed_items    = [it for it in items if not it.get("processed")]

    source_counts: dict[str, int] = {}
    for it in succeeded_items:
        name = it.get("source_name", "未知來源")
        source_counts[name] = source_counts.get(name, 0) + 1
    source_summary = "　".join(f"{n}（{c} 篇）" for n, c in source_counts.items())

    # 依類別分組
    tech_items = [it for it in succeeded_items if it.get("category") == "tech"]
    app_items  = [it for it in succeeded_items if it.get("category") == "application"]
    tech_cnt   = len(tech_items)
    app_cnt    = len(app_items)

    cover = f"""\
# 環境部 AI 科技新知摘要
## {today}

> 本報告由 AI 自動生成，供同仁快速掌握 AI 科技趨勢與環境部潛在應用。

---

### 本期摘要

| 項目 | 數值 |
|------|------|
| 報告日期 | {today} |
| 文章總數 | {total} 篇（成功 {succeeded} 篇 / 失敗 {failed_cnt} 篇）|
| AI 技術發展新知 | {tech_cnt} 篇 |
| AI 應用技巧 | {app_cnt} 篇 |
| 資料來源 | {source_summary or "—"} |
| 使用模型 | {model} |
| Token 用量 | 輸入 {total_in:,}　輸出 {total_out:,}　快取命中 {cache_hit:,} |

---

"""

    # ── 第一部分：AI 技術發展新知 ─────────────────────────────────────────────
    tech_section = (
        "## 一、AI 技術發展新知\n\n"
        "> 精選 AI 研究前沿，說明最新技術突破對環境工作的潛在影響。\n\n"
    )
    tech_blocks = [_build_article_block(it, i) for i, it in enumerate(tech_items, 1)]
    tech_section += "\n\n".join(tech_blocks) if tech_blocks else "*本期無技術新知文章。*\n"

    # ── 第二部分：AI 應用技巧 ──────────────────────────────────────────────────
    app_section = (
        "\n\n---\n\n"
        "## 二、AI 應用技巧\n\n"
        "> 精選可立即上手的 AI 工具操作建議，附逐步提示詞範例。\n\n"
    )
    app_blocks = [_build_article_block(it, i) for i, it in enumerate(app_items, 1)]
    app_section += "\n\n".join(app_blocks) if app_blocks else "*本期無應用技巧文章。*\n"

    footer = (
        f"\n\n---\n\n"
        f"*本報告由 `env_ai_reporter` 自動產生，"
        f"生成時間：{datetime.now(TZ_TAIWAN).strftime('%Y-%m-%d %H:%M CST')}*\n"
    )

    full_report = cover + tech_section + app_section + "\n" + _build_failed_block(failed_items) + footer
    out_path = reports_dir / f"{today}_ai_digest.md"
    out_path.write_text(full_report, encoding="utf-8")

    print(f"[reporter] Markdown 報告 → {out_path}")
    print(f"[reporter] 共 {succeeded} 篇文章，{len(full_report):,} 字元")
    return out_path


# ── PDF 報告 ──────────────────────────────────────────────────────────────────

def _parse_claude_sections(text: str) -> dict[str, str]:
    """
    解析 claude_output 中的四個 ### 區塊，回傳 {區塊名: 內容} dict。
    """
    sections: dict[str, str] = {}
    current_key = None
    current_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("### "):
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = line[4:].strip()
            current_lines = []
        else:
            if current_key is not None:
                current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    return sections


def generate_pdf(
    processed_path: Path = PROCESSED_PATH,
    reports_dir: Path = REPORTS_DIR,
    report_date: str | None = None,
) -> Path:
    """
    讀取 processed_data.json，輸出排版精美的 PDF 報告（繁體中文）。

    依賴：reportlab（pip install reportlab）
    字型：優先使用微軟正黑體（Windows 內建），其次 Noto / PingFang。
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
        TableStyle,
    )

    # ── 字型 ──────────────────────────────────────────────────────────────────
    font_reg, font_bold = _register_cjk_font()

    data       = json.loads(processed_path.read_text(encoding="utf-8"))
    today      = report_date or datetime.now(TZ_TAIWAN).strftime("%Y-%m-%d")  # 台灣時間
    model      = data.get("model", "")
    total      = data.get("total", 0)
    succeeded  = data.get("succeeded", 0)
    failed_cnt = data.get("failed", 0)
    total_in   = data.get("total_input_tokens", 0)
    total_out  = data.get("total_output_tokens", 0)
    cache_hit  = data.get("cache_read_tokens", 0)
    items      = data.get("items", [])

    succeeded_items = [it for it in items if it.get("processed")]
    failed_items    = [it for it in items if not it.get("processed")]

    source_counts: dict[str, int] = {}
    for it in succeeded_items:
        name = it.get("source_name", "未知來源")
        source_counts[name] = source_counts.get(name, 0) + 1

    # ── 樣式 ──────────────────────────────────────────────────────────────────
    MOENV_GREEN  = colors.HexColor("#2e7d32")
    MOENV_LIGHT  = colors.HexColor("#e8f5e9")
    SECTION_BG   = colors.HexColor("#f5f5f5")
    ACCENT_BLUE  = colors.HexColor("#1565c0")
    TEXT_DARK    = colors.HexColor("#212121")
    TEXT_MUTED   = colors.HexColor("#757575")

    def _style(name, **kw) -> ParagraphStyle:
        base = dict(fontName=font_reg, fontSize=11, leading=18,
                    textColor=TEXT_DARK, wordWrap="CJK")
        base.update(kw)
        return ParagraphStyle(name, **base)

    sty = {
        "cover_title": _style("cover_title", fontName=font_bold, fontSize=24,
                               leading=34, textColor=MOENV_GREEN, alignment=1,
                               spaceAfter=6),
        "cover_date":  _style("cover_date", fontSize=16, textColor=TEXT_MUTED,
                               alignment=1, spaceAfter=20),
        "cover_note":  _style("cover_note", fontSize=13, textColor=TEXT_MUTED,
                               alignment=1),
        "section_h":   _style("section_h", fontName=font_bold, fontSize=15,
                               textColor=MOENV_GREEN, spaceAfter=4, spaceBefore=10),
        "article_num": _style("article_num", fontName=font_bold, fontSize=16,
                               textColor=colors.white, leading=24),
        "orig_title":  _style("orig_title", fontSize=12, textColor=TEXT_MUTED,
                               leading=19, spaceAfter=4),
        "meta":        _style("meta", fontSize=12, textColor=TEXT_MUTED,
                               leading=19, spaceAfter=8),
        "sci_title":   _style("sci_title", fontName=font_bold, fontSize=17,
                               textColor=ACCENT_BLUE, leading=26, spaceAfter=6),
        "body":        _style("body", fontSize=14, leading=24, spaceAfter=5),
        "tip_item":    _style("tip_item", fontSize=14, leading=23,
                               leftIndent=16, spaceAfter=3),
        "label":       _style("label", fontName=font_bold, fontSize=14,
                               textColor=MOENV_GREEN, spaceAfter=3, spaceBefore=10),
        "footer":      _style("footer", fontSize=10, textColor=TEXT_MUTED,
                               alignment=1),
        "failed_head": _style("failed_head", fontName=font_bold, fontSize=14,
                               textColor=colors.HexColor("#b71c1c")),
        "failed_item": _style("failed_item", fontSize=13, textColor=TEXT_MUTED,
                               leading=20),
    }

    # ── 頁首 / 頁尾回呼 ───────────────────────────────────────────────────────
    def _on_page(canvas, doc):
        canvas.saveState()
        # 頂部色條
        canvas.setFillColor(MOENV_GREEN)
        canvas.rect(0, A4[1] - 1.0 * cm, A4[0], 1.0 * cm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont(font_bold, 9)
        canvas.drawString(1.5 * cm, A4[1] - 0.65 * cm, "環境部 AI 科技新知摘要")
        canvas.drawRightString(A4[0] - 1.5 * cm, A4[1] - 0.65 * cm, today)
        # 底部頁碼
        canvas.setFillColor(TEXT_MUTED)
        canvas.setFont(font_reg, 8)
        canvas.drawCentredString(A4[0] / 2, 0.6 * cm, f"第 {doc.page} 頁")
        canvas.restoreState()

    # ── 組合頁面內容 ──────────────────────────────────────────────────────────
    out_path = reports_dir / f"{today}_ai_digest.pdf"
    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2.2*cm, bottomMargin=1.8*cm,
        title=f"環境部 AI 科技新知摘要 {today}",
        author="env_ai_reporter",
    )

    story = []

    # ── 封面 ──────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 1.5 * cm))
    story.append(Paragraph("環境部 AI 科技新知摘要", sty["cover_title"]))
    story.append(Paragraph(today, sty["cover_date"]))
    story.append(HRFlowable(width="100%", thickness=2, color=MOENV_GREEN, spaceAfter=16))
    story.append(Paragraph("本報告由 AI 自動生成，供同仁快速掌握 AI 科技趨勢與環境部潛在應用。",
                            sty["cover_note"]))
    story.append(Spacer(1, 0.8 * cm))

    # 依類別分組
    tech_items_pdf = [it for it in succeeded_items if it.get("category") == "tech"]
    app_items_pdf  = [it for it in succeeded_items if it.get("category") == "application"]

    # 統計表
    source_rows = "\n".join(
        f"  {name}：{cnt} 篇" for name, cnt in source_counts.items()
    ) or "  —"
    table_data = [
        ["項目", "數值"],
        ["報告日期", today],
        ["AI 技術發展新知", f"{len(tech_items_pdf)} 篇"],
        ["AI 應用技巧", f"{len(app_items_pdf)} 篇"],
        ["資料來源", source_rows.strip()],
        ["使用模型", model],
        ["Token 用量", f"輸入 {total_in:,}  輸出 {total_out:,}  快取命中 {cache_hit:,}"],
    ]
    col_w = [4.5 * cm, 12 * cm]
    tbl = Table(table_data, colWidths=col_w)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), MOENV_GREEN),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), font_bold),
        ("FONTSIZE",   (0, 0), (-1, 0), 10),
        ("BACKGROUND", (0, 1), (0, -1), MOENV_LIGHT),
        ("FONTNAME",   (0, 1), (0, -1), font_bold),
        ("FONTNAME",   (1, 1), (1, -1), font_reg),
        ("FONTSIZE",   (0, 1), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SECTION_BG]),
        ("GRID",       (0, 0), (-1, -1), 0.5, colors.HexColor("#bdbdbd")),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
    ]))
    story.append(tbl)
    story.append(PageBreak())

    # ── 各篇文章（兩段式：技術新知 + 應用技巧）────────────────────────────────
    # 涵蓋兩種 system prompt 產生的所有 section 名稱
    SECTION_LABELS = {
        "標題":               None,          # 取內文作為大標，不顯示 label
        # Tech sections
        "技術核心":           "技術核心",
        "未來應用場景":        "未來應用場景",
        # App sections
        "同仁可以怎麼用？":    "同仁可以怎麼用？",
        "實務操作步驟":        "實務操作步驟",
        # 共用
        "一句話看懂":          "一句話看懂",
        # 舊格式相容
        "為什麼這對環境部重要？": "為什麼這對環境部重要？",
        "實務技巧小教室":        "實務技巧小教室",
    }

    def _render_article(item: dict, art_idx: int, art_label: str, band_color) -> None:
        """將單篇文章內容加入 story。"""
        title    = item.get("title", "（無標題）")
        url      = item.get("url", "")
        pub_date = _format_date(item.get("published", ""))
        src_id   = item.get("source_id", "")
        src_name = item.get("source_name", "")
        output   = item.get("claude_output", "").strip()

        sections  = _parse_claude_sections(output)
        sci_title = sections.get("標題", title)

        # 文章編號色帶（顏色依類別區分）
        num_data = [[Paragraph(art_label, sty["article_num"])]]
        num_tbl  = Table(num_data, colWidths=[16.5 * cm])
        num_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), band_color),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ]))
        story.append(num_tbl)
        story.append(Spacer(1, 4))

        story.append(_safe_para(sci_title, sty["sci_title"]))

        # meta 行混合純文字（_esc）與刻意的 <link> XML，使用 _xml_para 明確標示
        meta_xml = f"原文：{_esc(title)}"
        if pub_date:
            meta_xml += f"　｜　{_esc(pub_date)}"
        meta_xml += f"　｜　{_esc(_source_label(src_id, src_name, for_pdf=True))}"
        safe_url = _safe_url(url)
        if safe_url:
            meta_xml += f'　｜　<link href="{safe_url}" color="#1565c0">閱讀原文</link>'
        story.append(_xml_para(meta_xml, sty["orig_title"]))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#e0e0e0"), spaceAfter=8))

        for section_key, label in SECTION_LABELS.items():
            if section_key == "標題":
                continue
            content = sections.get(section_key, "").strip()
            if not content:
                continue
            story.append(_safe_para(label, sty["label"]))
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped:
                    story.append(Spacer(1, 3))
                    continue
                if re.match(r"^\d+[\.、]\s+", stripped):
                    story.append(_safe_para(stripped, sty["tip_item"]))
                else:
                    story.append(_safe_para(stripped, sty["body"]))

        story.append(Spacer(1, 0.5 * cm))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=MOENV_LIGHT, spaceAfter=10))

    def _section_banner(title_text: str, subtitle_text: str, color) -> None:
        """插入分節標題色帶。"""
        banner_data = [[
            Paragraph(title_text,    sty["article_num"]),
            Paragraph(subtitle_text, _style("banner_sub", fontSize=11,
                                            textColor=colors.white,
                                            leading=16, wordWrap="CJK")),
        ]]
        banner_tbl = Table(banner_data, colWidths=[8 * cm, 8.5 * cm])
        banner_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), color),
            ("TOPPADDING",    (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING",   (0, 0), (-1, -1), 12),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(banner_tbl)
        story.append(Spacer(1, 0.4 * cm))

    # ── 第一部分：AI 技術發展新知 ─────────────────────────────────────────────
    _section_banner("一、AI 技術發展新知",
                    "精選研究前沿，解析技術突破對環境工作的潛在影響",
                    MOENV_GREEN)
    if tech_items_pdf:
        for i, item in enumerate(tech_items_pdf, 1):
            _render_article(item, i, f"技術新知 {i}", MOENV_GREEN)
    else:
        story.append(_safe_para("本期無技術新知文章。", sty["body"]))

    story.append(PageBreak())

    # ── 第二部分：AI 應用技巧 ──────────────────────────────────────────────────
    _section_banner("二、AI 應用技巧",
                    "精選可立即上手的工具操作建議，附逐步提示詞範例",
                    ACCENT_BLUE)
    if app_items_pdf:
        for i, item in enumerate(app_items_pdf, 1):
            _render_article(item, i, f"應用技巧 {i}", ACCENT_BLUE)
            if i % 2 == 0 and i < len(app_items_pdf):
                story.append(PageBreak())
    else:
        story.append(_safe_para("本期無應用技巧文章。", sty["body"]))

    # ── 失敗附錄 ──────────────────────────────────────────────────────────────
    if failed_items:
        story.append(PageBreak())
        story.append(_safe_para("附錄：處理失敗清單", sty["failed_head"]))
        story.append(Spacer(1, 0.3 * cm))
        for item in failed_items:
            t = item.get("title", "（無標題）")
            e = item.get("error", "未知錯誤")
            story.append(_safe_para(f"• {t}", sty["failed_item"]))
            story.append(_safe_para(f"  錯誤：{e}", sty["failed_item"]))

    # ── 頁尾 ──────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.8 * cm))
    story.append(Paragraph(
        f"本報告由 env_ai_reporter 自動產生，"
        f"生成時間：{datetime.now(TZ_TAIWAN).strftime('%Y-%m-%d %H:%M CST')}",
        sty["footer"],
    ))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    print(f"[reporter] PDF 報告  → {out_path}")
    return out_path


# ── 直接執行入口 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    md_path  = generate_report()
    pdf_path = generate_pdf()
    print(f"\n[reporter] Done.")
    print(f"   Markdown : {md_path}")
    print(f"   PDF      : {pdf_path}")
