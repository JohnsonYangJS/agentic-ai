from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict


DEFAULT_PROJECT_ROOT = "~/projects/agentic-ai/morning-newspaper"
SYSTEMD_UNIT = "morning-newspaper-dashboard.service"
DASHBOARD_URL = "http://127.0.0.1:8510/dashboard.html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Morning Newspaper Assistant pipeline.")
    parser.add_argument("--project-root", default="", help="项目根目录；缺省时读 MORNING_NEWSPAPER_ROOT 再回落默认路径")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--skip-tavily", action="store_true", default=False, help="跳过 Tavily 检索源")
    parser.add_argument("--no-dashboard", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = _resolve_project_root(args.project_root)
    runtime = root / "runtime"

    _run(args.python, root / "scripts" / "collect_raw.py", root, extra=["--skip-tavily"] if args.skip_tavily else [])
    _run(args.python, root / "scripts" / "enrich_content.py", root)
    _run(args.python, root / "scripts" / "prepare_title_shortlist.py", root)

    title_shortlist_ready = _is_fresh_file(runtime / "title_shortlist_result.json", runtime / "title_candidates.json")
    shortlist_fresh = False
    draft_input_fresh = False
    if title_shortlist_ready:
        _run(args.python, root / "scripts" / "apply_title_shortlist.py", root)
        _run(args.python, root / "scripts" / "prepare_draft_input.py", root)
        shortlist_fresh = _is_fresh_shortlist(runtime / "shortlist.json", runtime / "content_enriched.json", runtime / "title_shortlist_result.json")
        draft_input_fresh = shortlist_fresh and _is_fresh_for_input(runtime / "draft_input.json", runtime / "shortlist.json")
    else:
        shortlist_fresh = _is_fresh_shortlist(runtime / "shortlist.json", runtime / "content_enriched.json", runtime / "title_shortlist_result.json")
        draft_input_fresh = shortlist_fresh and _is_fresh_for_input(runtime / "draft_input.json", runtime / "shortlist.json")

    draft_result_fresh = draft_input_fresh and _is_fresh_file(runtime / "draft_result.json", runtime / "draft_input.json")
    drafted_items_fresh = False
    if draft_result_fresh:
        _run(args.python, root / "scripts" / "apply_draft_results.py", root)
        drafted_items_fresh = _is_fresh_drafted(runtime / "drafted_items.json", runtime / "draft_input.json", runtime / "draft_result.json")
    else:
        drafted_items_fresh = draft_input_fresh and _is_fresh_drafted(runtime / "drafted_items.json", runtime / "draft_input.json", runtime / "draft_result.json")

    if drafted_items_fresh:
        _run(args.python, root / "scripts" / "prepare_top10_ranking.py", root)

    ranking_input_fresh = drafted_items_fresh and _is_fresh_for_input(runtime / "top10_ranking_input.json", runtime / "drafted_items.json")
    ranking_result_fresh = ranking_input_fresh and _is_fresh_file(runtime / "top10_ranking_result.json", runtime / "top10_ranking_input.json")
    publishable_fresh = False
    if ranking_result_fresh:
        _run(args.python, root / "scripts" / "apply_top10_ranking.py", root)
        publishable_fresh = _is_fresh_publishable(runtime / "top10_publishable.json", runtime / "drafted_items.json", runtime / "top10_ranking_result.json")
    else:
        publishable_fresh = drafted_items_fresh and _is_fresh_publishable(runtime / "top10_publishable.json", runtime / "drafted_items.json", runtime / "top10_ranking_result.json")

    dashboard_file = runtime / "dashboard.html"
    dashboard_html_fresh = False
    if publishable_fresh:
        _run(args.python, root / "scripts" / "build_dashboard.py", root)
        dashboard_html_fresh = _is_fresh_file(dashboard_file, runtime / "top10_publishable.json")
    else:
        dashboard_html_fresh = False

    dashboard_url = ""
    dashboard_launch = {"status": "skipped", "message": "动态看板未启动。"}
    if not args.no_dashboard:
        dashboard_launch = _maybe_launch_dashboard(root)
        dashboard_url = str(dashboard_launch.get("url", "")).strip()

    overview = {
        "collected_total": _json_count(runtime / "collected_raw.json"),
        "candidate_count": _json_count(runtime / "shortlist.json") if shortlist_fresh else 0,
        "drafted_count": _json_count(runtime / "drafted_items.json") if drafted_items_fresh else 0,
        "top10_count": _json_count(runtime / "top10_publishable.json") if publishable_fresh else 0,
        "ai_selected": publishable_fresh,
        "urgent_count": len(_missing_steps(title_shortlist_ready, draft_result_fresh, ranking_result_fresh)),
        "important_count": _failed_sources_count(runtime / "collect_report.json"),
        "dashboard_ready": dashboard_html_fresh,
    }

    missing_steps = _missing_steps(title_shortlist_ready, draft_result_fresh, ranking_result_fresh)
    status = "ok" if not missing_steps else "partial"
    message = (
        "早报助手链路已完成，静态页面可用。"
        if publishable_fresh and dashboard_html_fresh
        else "早报助手链路已完成到可继续人工/模型回填的阶段。"
    )

    result = {
        "status": status,
        "message": message,
        "project_root": str(root),
        "dashboard_file": str(dashboard_file),
        "dashboard_file_url": dashboard_file.resolve().as_uri() if dashboard_file.exists() else "",
        "dashboard_url": dashboard_url,
        "dashboard": dashboard_launch,
        "outputs": {
            "collected_raw": (runtime / "collected_raw.json").exists(),
            "content_enriched": (runtime / "content_enriched.json").exists(),
            "title_candidates": (runtime / "title_candidates.json").exists(),
            "shortlist": shortlist_fresh,
            "draft_input": draft_input_fresh,
            "drafted_items": drafted_items_fresh,
            "top10_ranking_input": ranking_input_fresh,
            "top10_publishable": publishable_fresh,
            "dashboard_html": dashboard_html_fresh,
        },
        "overview": overview,
        "missing_human_or_model_steps": missing_steps,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _run(python: str, script: Path, workdir: Path, *, extra: list[str] | None = None) -> None:
    cmd = [python, str(script)]
    if extra:
        cmd.extend(extra)
    completed = subprocess.run(cmd, cwd=str(workdir), text=True, capture_output=True, check=False)
    if completed.stdout.strip():
        print(completed.stdout.strip(), file=sys.stderr)
    if completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}")


def _resolve_project_root(explicit: str) -> Path:
    """定位项目根目录。

    优先级：--project-root 参数 > MORNING_NEWSPAPER_ROOT 环境变量 > 默认路径。
    环境变量里的 ~ 必须自行展开——OpenClaw 加载 ~/.openclaw/.env 时是
    process.env[key] = value 直接赋值，不会做 tilde 展开。
    """
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_root = (os.environ.get("MORNING_NEWSPAPER_ROOT") or "").strip()
    if env_root:
        candidates.append(env_root)
    candidates.append(DEFAULT_PROJECT_ROOT)

    for raw in candidates:
        expanded = Path(raw).expanduser()
        if (expanded / "scripts" / "collect_raw.py").exists():
            return expanded.resolve()

    raise SystemExit(
        "无法定位 morning-newspaper 项目根目录。已尝试: "
        + ", ".join(candidates)
        + "。请设置 MORNING_NEWSPAPER_ROOT 或用 --project-root 指定。"
    )


def _maybe_launch_dashboard(root: Path) -> Dict[str, Any]:
    if os.name != "nt":
        return _maybe_launch_dashboard_systemd()
    cmd_path = root / "run_dashboard.cmd"
    if not cmd_path.exists():
        return {"status": "missing", "message": "run_dashboard.cmd 不存在。", "url": ""}
    try:
        completed = subprocess.run(
            ["cmd", "/c", str(cmd_path)],
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "started", "message": "动态看板启动中。", "url": "http://127.0.0.1:8502"}
    if completed.returncode == 0:
        return {"status": "ok", "message": "动态看板已启动。", "url": "http://127.0.0.1:8502"}
    return {
        "status": "error",
        "message": completed.stderr.strip() or completed.stdout.strip() or "动态看板启动失败。",
        "url": "",
    }


def _maybe_launch_dashboard_systemd() -> Dict[str, Any]:
    """非 Windows：静态页由 systemd 托管（morning-newspaper-dashboard.service）。"""
    if shutil.which("systemctl") is None:
        return {"status": "missing", "message": "systemctl 不可用。", "url": ""}
    if subprocess.run(["systemctl", "cat", SYSTEMD_UNIT], capture_output=True).returncode != 0:
        return {"status": "missing", "message": f"{SYSTEMD_UNIT} 未安装。", "url": ""}
    active = subprocess.run(["systemctl", "is-active", "--quiet", SYSTEMD_UNIT]).returncode == 0
    if not active:
        subprocess.run(["systemctl", "start", SYSTEMD_UNIT], capture_output=True, check=False)
        time.sleep(1)
        active = subprocess.run(["systemctl", "is-active", "--quiet", SYSTEMD_UNIT]).returncode == 0
    if not active:
        return {"status": "error", "message": f"{SYSTEMD_UNIT} 启动失败。", "url": ""}
    return {"status": "ok", "message": f"{SYSTEMD_UNIT} 运行中。", "url": DASHBOARD_URL}


def _json_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return int(payload.get("count", 0) or 0) if isinstance(payload, dict) else 0


def _failed_sources_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    sources = payload.get("sources", [])
    if not isinstance(sources, list):
        return 0
    return sum(1 for row in sources if isinstance(row, dict) and str(row.get("status", "")).strip() != "ok")


def _missing_steps(title_shortlist_ready: bool, draft_result_fresh: bool, ranking_result_fresh: bool) -> list[str]:
    return [
        name for name, exists in {
            "title_shortlist_result.json": title_shortlist_ready,
            "draft_result.json": draft_result_fresh,
            "top10_ranking_result.json": ranking_result_fresh,
        }.items() if not exists
    ]


def _is_fresh_for_input(output_path: Path, input_path: Path, *, json_input: bool = True) -> bool:
    if not output_path.exists() or not input_path.exists():
        return False
    if json_input:
        try:
            output_payload = json.loads(output_path.read_text(encoding="utf-8"))
            if str(output_payload.get("input", "")).strip() != str(input_path):
                return False
        except Exception:
            return False
    return output_path.stat().st_mtime >= input_path.stat().st_mtime


def _is_fresh_drafted(output_path: Path, input_path: Path, result_path: Path) -> bool:
    if not output_path.exists() or not input_path.exists() or not result_path.exists():
        return False
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if str(payload.get("input", "")).strip() != str(input_path):
        return False
    if str(payload.get("draft_result_file", "")).strip() != str(result_path):
        return False
    output_mtime = output_path.stat().st_mtime
    return output_mtime >= input_path.stat().st_mtime and output_mtime >= result_path.stat().st_mtime


def _is_fresh_shortlist(output_path: Path, input_path: Path, selected_path: Path) -> bool:
    if not output_path.exists() or not input_path.exists() or not selected_path.exists():
        return False
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if str(payload.get("input", "")).strip() != str(input_path):
        return False
    if str(payload.get("selected_titles_file", "")).strip() != str(selected_path):
        return False
    output_mtime = output_path.stat().st_mtime
    return output_mtime >= input_path.stat().st_mtime and output_mtime >= selected_path.stat().st_mtime


def _is_fresh_publishable(output_path: Path, drafted_items_path: Path, ranking_result_path: Path) -> bool:
    if not output_path.exists() or not drafted_items_path.exists() or not ranking_result_path.exists():
        return False
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if str(payload.get("input", "")).strip() != str(drafted_items_path):
        return False
    if str(payload.get("ranking_result_file", "")).strip() != str(ranking_result_path):
        return False
    output_mtime = output_path.stat().st_mtime
    return output_mtime >= drafted_items_path.stat().st_mtime and output_mtime >= ranking_result_path.stat().st_mtime


def _is_fresh_file(output_path: Path, input_path: Path) -> bool:
    if not output_path.exists() or not input_path.exists():
        return False
    return output_path.stat().st_mtime >= input_path.stat().st_mtime


if __name__ == "__main__":
    main()
