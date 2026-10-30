from __future__ import annotations

from datetime import datetime, timezone, timedelta
from html import escape
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List

_BEIJING = timezone(timedelta(hours=8))


def _to_beijing_time(iso_str: str) -> str:
    """将 ISO 时间字符串转为北京时间显示格式，如 '2026-06-08 17:15 CST'。"""
    text = (iso_str or "").strip()
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    beijing = dt.astimezone(_BEIJING)
    return beijing.strftime("%Y-%m-%d %H:%M CST")


def write_static_dashboard(runtime_dir: Path, output_path: Path) -> Path:
    data = build_dashboard_payload(runtime_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # 原子写：先写同目录临时文件，再 os.replace 覆盖。
    # 8510 静态服务正在 serving 这个文件，直接 write_text 覆盖会让访问者
    # 在替换瞬间拿到半截页面。os.replace 在同一文件系统上是原子操作。
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    tmp_path.write_text(_render_html(data), encoding="utf-8")
    os.replace(tmp_path, output_path)
    return output_path


def build_dashboard_payload(runtime_dir: Path) -> Dict[str, Any]:
    collected = _read_json(runtime_dir / "collected_raw.json")
    shortlist = _read_json(runtime_dir / "shortlist.json")
    publishable = _read_json(runtime_dir / "top10_publishable.json")
    final_newspaper = _read_json(runtime_dir / "final_newspaper.json")
    collect_report = _read_json(runtime_dir / "collect_report.json")
    executive_mailbox = _read_json(runtime_dir / "executive_mailbox.json")

    primary_payload = final_newspaper if _safe_items(final_newspaper) else publishable
    top_items = [_to_display_item(item) for item in _safe_items(primary_payload)]
    source_health = _source_rows(collect_report)
    task_alerts = _mailbox_items(executive_mailbox)

    generated_at = (
        str(primary_payload.get("generated_at", "")).strip()
        or str(final_newspaper.get("generated_at", "")).strip()
        or str(publishable.get("generated_at", "")).strip()
    )
    top10_count = int(primary_payload.get("count", 0) or len(top_items) or 0)

    return {
        "generated_at": generated_at,
        "overview": {
            "collected_total": int(collected.get("count", 0) or 0),
            "candidate_count": int(shortlist.get("count", 0) or 0),
            "top10_count": top10_count,
            "ai_selected": bool(top_items),
            "important_count": min(3, len(top_items)),
            "urgent_task_count": len(task_alerts),
        },
        "headline": str(final_newspaper.get("headline", "")).strip() or "今日 AI 早报",
        "lead": _build_lead_bullets(top_items),
        "top_items": top_items,
        "mail_alerts": task_alerts,
        "source_health": source_health,
    }


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = payload.get("items", [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _to_display_item(item: Dict[str, Any]) -> Dict[str, Any]:
    rank = int(item.get("rank", 0) or 0)
    title = (
        str(item.get("title_zh", "")).strip()
        or str(item.get("card_title", "")).strip()
        or str(item.get("title", "")).strip()
    )
    summary = (
        str(item.get("summary_main", "")).strip()
        or str(item.get("card_summary", "")).strip()
        or str(item.get("summary_zh", "")).strip()
        or str(item.get("summary", "")).strip()
    )
    return {
        "rank": rank,
        "priority": str(item.get("priority", "")).strip() or _priority_from_rank(rank),
        "title": str(item.get("title", "")).strip() or title,
        "title_zh": title,
        "summary": str(item.get("summary", "")).strip() or summary,
        "summary_zh": summary,
        "source_name": str(item.get("source_name", "")).strip(),
        "source_type": str(item.get("source_type", "")).strip(),
        "published_at": str(item.get("published_at", "")).strip(),
        "url": str(item.get("url", "")).strip(),
        "topic_icon": _topic_icon(item),
    }


def _build_lead_bullets(items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    output: List[Dict[str, str]] = []
    for item in items[:3]:
        output.append(
            {
                "icon": str(item.get("topic_icon", "•")),
                "title": str(item.get("title_zh", "")).strip(),
                "summary": _trim_text(str(item.get("summary_zh", "")).strip(), 120),
            }
        )
    return output


def _mailbox_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return []

    cards: List[Dict[str, Any]] = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        priority = str(item.get("priority", "Important")).strip() or "Important"
        cards.append(
            {
                "rank": int(item.get("rank", idx) or idx),
                "priority": priority,
                "title_zh": str(item.get("title_zh") or item.get("title") or "(untitled)").strip(),
                "summary_zh": str(item.get("summary_zh") or item.get("summary") or "").strip(),
                "source_name": str(item.get("source_name", "邮箱")).strip() or "邮箱",
                "published_at": str(item.get("published_at", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "topic_icon": str(item.get("topic_icon", "📤")).strip() or "📤",
            }
        )
    cards.sort(key=lambda row: int(row.get("rank", 10**9)))
    return cards


def _source_rows(collect_report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for row in collect_report.get("sources", []):
        if not isinstance(row, dict):
            continue
        status = str(row.get("status", "")).strip()
        rows.append(
            {
                "source_id": str(row.get("source_id", "")).strip(),
                "items": int(row.get("item_count", 0) or 0),
                "status": "正常" if status == "ok" else "异常",
            }
        )
    return rows


def _render_html(data: Dict[str, Any]) -> str:
    overview = data.get("overview", {})
    headline = data.get("headline", "今日 AI 早报")
    lead = data.get("lead", [])
    top_items = data.get("top_items", [])
    mail_alerts = data.get("mail_alerts", [])
    source_health = data.get("source_health", [])
    generated_at = escape(_to_beijing_time(str(data.get("generated_at", ""))))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>今日 AI 早报</title>
  <style>
    body {{ margin: 0; font-family: Inter, Arial, "Microsoft YaHei", sans-serif; color: #182230; background: linear-gradient(180deg, #f8fafc 0%, #f3f6fb 100%); }}
    .wrap {{ max-width: 1180px; margin: 0 auto; padding: 36px 24px 48px; }}
    .hero {{ margin-bottom: 20px; padding: 24px 24px 18px; border: 1px solid #e4e7ec; border-radius: 18px; background: linear-gradient(135deg, #ffffff 0%, #f7fbff 100%); box-shadow: 0 10px 30px rgba(16,24,40,0.06); }}
    .leadbox {{ margin-top: 16px; max-width: 980px; }}
    .leadtitle {{ font-size: 13px; font-weight: 800; color: #175cd3; margin-bottom: 10px; letter-spacing: .04em; text-transform: uppercase; }}
    .leadgrid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .leadcard {{ background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%); border: 1px solid #dbe7ff; border-radius: 14px; padding: 14px 14px 13px; box-shadow: 0 4px 14px rgba(23,92,211,0.06); }}
    .leadcard-title {{ font-size: 15px; font-weight: 800; color: #182230; margin: 0 0 8px; display: flex; align-items: center; gap: 8px; }}
    .leadcard-summary {{ margin: 0; color: #475467; font-size: 14px; line-height: 1.75; }}
    h1 {{ margin: 0 0 8px; font-size: 34px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 16px; font-size: 21px; letter-spacing: 0; }}
    .muted {{ color: #667085; font-size: 13px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin: 20px 0; }}
    .metric {{ background: rgba(255,255,255,0.94); border: 1px solid #e4e7ec; border-radius: 14px; padding: 14px 15px; box-shadow: 0 4px 14px rgba(16,24,40,0.04); }}
    .metric b {{ display: block; font-size: 24px; margin-top: 6px; }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 2fr) minmax(300px, 0.92fr); gap: 20px; align-items: start; }}
    .panel {{ background: rgba(255,255,255,0.97); border: 1px solid #e4e7ec; border-radius: 18px; padding: 22px; box-shadow: 0 8px 24px rgba(16,24,40,0.05); }}
    .item {{ border-top: 1px solid #eef2f6; padding: 22px 0; }}
    .item:first-child {{ border-top: 0; padding-top: 8px; }}
    .head {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .badge {{ border-radius: 999px; padding: 4px 10px; font-size: 12px; font-weight: 700; border: 1px solid transparent; }}
    .Urgent {{ color: #b42318; background: #fff1f0; border-color: #fecdca; }}
    .Important {{ color: #b54708; background: #fff7ed; border-color: #fed7aa; }}
    .FYI {{ color: #175cd3; background: #eff8ff; border-color: #b2ddff; }}
    .title {{ font-weight: 800; font-size: 21px; margin: 12px 0 8px; line-height: 1.4; letter-spacing: 0; }}
    .summary {{ line-height: 1.82; color: #344054; margin: 0 0 14px; font-size: 15px; }}
    .label {{ color: #344054; font-weight: 700; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 12px 18px; margin-top: 10px; }}
    .row {{ color: #667085; font-size: 13px; background: #f8fafc; border: 1px solid #eef2f6; border-radius: 999px; padding: 6px 10px; }}
    .link {{ display: inline-block; margin-top: 14px; color: #175cd3; font-weight: 700; text-decoration: none; border: 1px solid #c7d7fe; border-radius: 10px; padding: 8px 12px; background: #f5f8ff; }}
    .link:hover {{ background: #eaf1ff; }}
    .nolink {{ color: #98a2b3; }}
    a {{ color: #175cd3; text-decoration: none; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    td, th {{ border-top: 1px solid #eaecf0; padding: 8px; text-align: left; }}
    @media (max-width: 900px) {{ .metrics {{ grid-template-columns: repeat(2, 1fr); }} .grid {{ grid-template-columns: 1fr; }} .leadgrid {{ grid-template-columns: 1fr; }} .hero {{ padding: 20px 18px 16px; }} .panel {{ padding: 18px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>{escape(str(headline))}</h1>
      <div class="muted">聚焦近 3 天 AI 技术与商业信号 | 更新时间：{generated_at}</div>
      {_lead_cards(lead)}
    </div>
    <div class="metrics">
      {_metric("今日采集", overview.get("collected_total", 0))}
      {_metric("候选池", overview.get("candidate_count", 0))}
      {_metric("Top10", overview.get("top10_count", 0))}
      {_metric("AI 精选", "是" if overview.get("ai_selected") else "否")}
      {_metric("重点信号", overview.get("important_count", 0))}
      {_metric("今日待办", overview.get("urgent_task_count", 0))}
    </div>
    <div class="grid">
      <section class="panel">
        <h2>Top10</h2>
        {_items(top_items, empty_text="暂无 Top10 内容。")}
      </section>
      <aside>
        <section class="panel">
          <h2>今日待办提醒</h2>
          {_items(mail_alerts, empty_text="今天暂无需要提醒的事项。")}
        </section>
        <br>
        <section class="panel">
          <h2>来源统计</h2>
          {_sources(source_health)}
        </section>
      </aside>
    </div>
  </div>
</body>
</html>
"""


def _metric(label: str, value: Any) -> str:
    return f'<div class="metric"><span class="muted">{escape(str(label))}</span><b>{escape(str(value))}</b></div>'


def _lead_cards(items: Iterable[Dict[str, Any]]) -> str:
    cards = []
    for item in items:
        icon = escape(str(item.get("icon", "•")))
        title = escape(str(item.get("title", "")))
        summary = escape(str(item.get("summary", "")))
        cards.append(f'<div class="leadcard"><div class="leadcard-title">{icon} {title}</div><p class="leadcard-summary">{summary}</p></div>')
    if not cards:
        return ""
    return f'<div class="leadbox"><div class="leadtitle">今日看点</div><div class="leadgrid">{"".join(cards)}</div></div>'


def _items(items: Iterable[Dict[str, Any]], *, empty_text: str) -> str:
    chunks = []
    for item in items:
        priority = escape(str(item.get("priority", "FYI")))
        icon = escape(str(item.get("topic_icon", "•")))
        title = escape(str(item.get("title_zh") or item.get("title", "(untitled)")))
        source = escape(str(item.get("source_name", "-")))
        summary = escape(str(item.get("summary_zh") or item.get("summary", "")))
        published_at = escape(str(item.get("published_at", "")))
        url = str(item.get("url", "")).strip()
        link = (
            f'<a class="link" href="{escape(url)}" target="_blank" rel="noopener noreferrer">访问链接</a>'
            if url
            else '<span class="nolink">无外部链接</span>'
        )
        chunks.append(
            f"""<div class="item">
  <div class="head"><span>#{escape(str(item.get("rank", "-")))}</span><span class="badge {priority}">{priority}</span><span class="muted">{source}</span></div>
  <div class="title">{icon} {title}</div>
  <p class="summary">{summary}</p>
  <div class="meta">
    <div class="row"><span class="label">来源</span> | {source}</div>
    <div class="row"><span class="label">发布时间</span> | {published_at or "-"}</div>
  </div>
  <div>{link}</div>
</div>"""
        )
    return "\n".join(chunks) if chunks else f'<p class="muted">{escape(empty_text)}</p>'


def _sources(rows: Iterable[Dict[str, Any]]) -> str:
    body = "\n".join(
        f"<tr><td>{escape(str(row.get('source_id', '')))}</td><td>{escape(str(row.get('items', 0)))}</td><td>{escape(str(row.get('status', '')))}</td></tr>"
        for row in rows
    )
    return f"<table><thead><tr><th>来源</th><th>数量</th><th>状态</th></tr></thead><tbody>{body}</tbody></table>"


def _priority_from_rank(rank: int) -> str:
    if rank <= 3:
        return "Important"
    return "FYI"


def _topic_icon(item: Dict[str, Any]) -> str:
    source_type = str(item.get("source_type", "")).strip()
    title = str(item.get("title", "")).lower()
    url = str(item.get("url", "")).lower()
    if source_type == "github_high_stars" and "agent" in title:
        return "🤖"
    if source_type == "github_high_stars":
        return "🧰"
    if source_type == "hackernews_top":
        return "📰"
    if source_type == "rss":
        return "🌐"
    if source_type in {"tavily_skill", "tavily_api"}:
        if "github.com" in url:
            return "🧰"
        if "news.ycombinator.com" in url:
            return "📰"
        if any(host in url for host in ["openai.com", "anthropic.com", "deepmind.google", "blog.google", "ai.meta.com", "huggingface.co", "arxiv.org"]):
            return "🧠"
        if any(host in url for host in ["linkedin.com", "finance.yahoo.com", "reuters.com", "bloomberg.com", "cnbc.com", "ft.com", "wsj.com", "techcrunch.com", "theverge.com"]):
            return "💼"
        return "🔎"
    return "•"


def _trim_text(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split()).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip(" ，,；;。") + "…"
