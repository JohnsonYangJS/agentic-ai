from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT_ROOT / 'runtime'

# 采集报告里一个来源可能占多行（计划行 + 结果行）。只有真正失败的才算失败：
# tavily_search 的计划行状态是 plan_executed（计划已执行），其结果行才是 ok，
# 早前把 plan_executed 当失败会把「Tavily 实际采到十几条」误报成采集失败。
SUCCESS_SOURCE_STATUSES = {'ok', 'plan_executed', 'executed', 'success', 'cached'}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _count(name: str) -> int:
    return int(_load(RUNTIME / name).get('count', 0) or 0)


def main() -> None:
    collect = _load(RUNTIME / 'collect_report.json')
    sources = collect.get('sources', []) if isinstance(collect.get('sources'), list) else []
    failed_sources = [
        row for row in sources
        if isinstance(row, dict)
        and str(row.get('status', '')).strip().lower() not in SUCCESS_SOURCE_STATUSES
    ]

    shortlist = _load(RUNTIME / 'shortlist.json')
    shortlist_items = shortlist.get('items', []) if isinstance(shortlist.get('items'), list) else []
    draft_input = _load(RUNTIME / 'draft_input.json')
    draft_input_items = draft_input.get('items', []) if isinstance(draft_input.get('items'), list) else []
    drafted = _load(RUNTIME / 'drafted_items.json')
    drafted_items = drafted.get('items', []) if isinstance(drafted.get('items'), list) else []
    ranking_input = _load(RUNTIME / 'top10_ranking_input.json')
    ranking_input_items = ranking_input.get('items', []) if isinstance(ranking_input.get('items'), list) else []
    publishable = _load(RUNTIME / 'top10_publishable.json')
    publishable_items = publishable.get('items', []) if isinstance(publishable.get('items'), list) else []

    summary_placeholders = [
        row.get('title', '') for row in publishable_items
        if isinstance(row, dict) and str(row.get('summary', '')).strip().startswith('[TEST]')
    ]

    report = {
        'collected_total': _count('collected_raw.json'),
        'candidate_count': _count('shortlist.json'),
        'draft_input_count': len(draft_input_items),
        'drafted_count': _count('drafted_items.json'),
        'ranking_input_count': len(ranking_input_items),
        'top10_count': _count('top10_publishable.json'),
        'mailbox_count': _count('executive_mailbox.json'),
        'failed_source_count': len(failed_sources),
        'failed_sources': [row.get('source_id', '') for row in failed_sources],
        'dashboard_exists': (RUNTIME / 'dashboard.html').exists(),
        'summary_placeholders': summary_placeholders,
        'shortlist_titles': [row.get('title', '') for row in shortlist_items if isinstance(row, dict)],
        'drafted_titles': [row.get('title', '') for row in drafted_items if isinstance(row, dict)],
        'publishable_titles': [row.get('title', '') for row in publishable_items if isinstance(row, dict)],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    errors: list[str] = []
    if report['top10_count'] <= 0:
        errors.append(f"top10_count={report['top10_count']}")
    if report['candidate_count'] <= 0:
        errors.append(f"candidate_count={report['candidate_count']}")
    if report['drafted_count'] <= 0:
        errors.append(f"drafted_count={report['drafted_count']}")
    if report['top10_count'] > report['drafted_count']:
        errors.append(f"top10_exceeds_drafted={report['top10_count']}>{report['drafted_count']}")
    if not report['dashboard_exists']:
        errors.append('dashboard_missing')
    if report['summary_placeholders']:
        errors.append('summary_placeholders_present')

    if errors:
        raise SystemExit('runtime check failed: ' + ', '.join(errors))


if __name__ == '__main__':
    main()
