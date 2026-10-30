from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from morning_newspaper.common import load_yaml, positive_int, write_json, write_text
from morning_newspaper.models import utc_now_iso


DEFAULT_MAX_AGE_DAYS = 7


PROMPT_TEMPLATE = """你是 AI 早报编辑。
请只根据下面这批候选标题，按“AI 最新的技术进展和商业化落地”这个主题做相关性排序。

优先考虑：
1. AI 技术、模型、Agent、产品发布、开源工具、企业采用、融资并购直接相关的标题。
2. 对今天的中文 AI 早报更有信息密度、更新价值和代表性的标题。
3. 不同来源、不同子主题的覆盖面，不要让候选池被同一来源或同一类重复内容占满。

时效要求：
- 候选池已剔除发布超过 {max_age_days} 天的旧条目（本次共剔除 {dropped_count} 条）。
- 不要因为“看起来更完整”而把已被剔除的旧闻重新引入；本轮只从给定候选里选。
- 未标注日期的条目（多为持续更新的开源项目或无发布时间页面）不受时效限制，正常考虑即可。

明确要求：
- 这一步的目标不是把候选裁得很少，而是给后续 Top10 提供一个足够宽、足够多样的有序候选池。
- 尽量保留来自不同来源的代表项；如果 GitHub 高星项目或 Hacker News 热门故事中有明显相关项，应优先至少保留少量代表项，不要被单一来源全部挤出。
- 对完全重复、近似重复、同一页面不同搜索命中的标题，只保留最有代表性的一条，其余排到很后面或直接不选。
- 如果某个标题明显与 AI 主线无关，可以不选。

输出要求：
- 尽量返回前 15 条；如果高相关候选不足 15 条，可少于 15 条。
- 同标题不要重复返回。
- 只输出 JSON：

{{
  "ranked_titles": ["标题1", "标题2"]
}}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare title-only shortlist input for the AI morning report.")
    parser.add_argument("--input", default=str(PROJECT_ROOT / "runtime" / "content_enriched.json"))
    parser.add_argument("--titles-json", default=str(PROJECT_ROOT / "runtime" / "title_candidates.json"))
    parser.add_argument("--prompt-txt", default=str(PROJECT_ROOT / "runtime" / "title_shortlist_prompt.txt"))
    parser.add_argument("--preview-md", default=str(PROJECT_ROOT / "runtime" / "title_candidates_preview.md"))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "sources.yaml"))
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=0,
        help="候选池时效上限（天）；0 表示读 config.title_shortlist.max_age_days，默认 7",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"input not found: {input_path}")

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        raise SystemExit("input JSON must contain an items list")

    candidates = [_to_title_candidate(item) for item in items if isinstance(item, dict)]
    candidates = [item for item in candidates if item["title"]]
    candidates = _dedupe_candidates(candidates)

    max_age_days = _load_max_age_days(Path(args.config), args.max_age_days)
    now = datetime.now(timezone.utc)
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for item in candidates:
        age = _age_days(str(item.get("published_at", "")), now)
        if age is not None and age > max_age_days:
            dropped.append({"title": item["title"], "published_at": item.get("published_at", ""), "age_days": age})
            continue
        item["age_days"] = age
        kept.append(item)
    candidates = kept

    write_json(Path(args.titles_json), {
        "generated_at": utc_now_iso(),
        "input": str(input_path),
        "max_age_days": max_age_days,
        "count": len(candidates),
        "dropped_stale_count": len(dropped),
        "dropped_stale": dropped,
        "titles": candidates,
    })
    write_text(Path(args.prompt_txt), PROMPT_TEMPLATE.format(max_age_days=max_age_days, dropped_count=len(dropped)))
    write_text(Path(args.preview_md), _render_preview(candidates))

    print(f"title candidates={len(candidates)}")
    print(f"dropped stale (>{max_age_days}d)={len(dropped)}")
    print(f"wrote {args.titles_json}")
    print(f"wrote {args.prompt_txt}")


def _load_max_age_days(config_path: Path, override: int) -> int:
    """时效上限：命令行 > config.title_shortlist.max_age_days > 默认 7 天。"""
    if override > 0:
        return override
    try:
        config = load_yaml(config_path)
    except Exception:
        return DEFAULT_MAX_AGE_DAYS
    section = config.get("title_shortlist", {}) if isinstance(config, dict) else {}
    if not isinstance(section, dict):
        return DEFAULT_MAX_AGE_DAYS
    return max(1, positive_int(section.get("max_age_days"), DEFAULT_MAX_AGE_DAYS))


def _age_days(published_at: str, now: datetime) -> Optional[int]:
    """返回条目已发布多少天；日期缺失或无法解析时返回 None（表示未知，不做时效裁剪）。"""
    raw = (published_at or "").strip()
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, (now - parsed).days)


def _to_title_candidate(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "item_id": str(item.get("item_id", "")).strip(),
        "title": str(item.get("title", "")).strip(),
        "source_type": str(item.get("source_type", "")).strip(),
        "source_name": str(item.get("source_name", "")).strip(),
        "published_at": str(item.get("published_at", "")).strip(),
    }


def _dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen_titles: set[str] = set()
    for item in candidates:
        title = str(item.get("title", "")).strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        deduped.append(item)
    return deduped


def _render_preview(candidates: List[Dict[str, Any]]) -> str:
    lines = [
        "# Title Candidates",
        "",
        f"- generated_at: {utc_now_iso()}",
        f"- count: {len(candidates)}",
        "",
    ]
    for idx, item in enumerate(candidates, 1):
        lines.append(
            f"{idx}. [{item.get('source_type', '')}] {item.get('title', '')} "
            f"({item.get('published_at', '')})"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
