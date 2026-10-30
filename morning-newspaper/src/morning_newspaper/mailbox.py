from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.policy import default
from email.utils import parsedate_to_datetime
import imaplib
import json
import os
import poplib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .common import compact_text, normalize_iso, positive_int, strip_html
from .mailbox_oauth import access_token_from_env, build_xoauth2_payload
from .models import make_item_id, utc_now_iso


def collect_mailbox(source: Dict[str, Any], *, runtime_dir: Path) -> Dict[str, Any]:
    username = _source_secret(source, "user_env", "IMAP_USER", "MAIL_USER")
    password = _source_secret(source, "pass_env", "IMAP_PASS", "MAIL_PASSWORD")
    access_token = access_token_from_env() or ""
    source_id = compact_text(source.get("id")) or "executive_mailbox"
    source_name = compact_text(source.get("source_name") or source.get("name")) or "邮箱提醒"
    max_items = positive_int(source.get("max_items"), 10)

    queue_path = runtime_dir / (compact_text(source.get("event_queue_file")) or "mail_event_queue.json")
    queue = _load_queue(queue_path)
    source = dict(source)
    source["_mail_queue_exists"] = queue_path.exists()

    if not access_token and (not username or not password):
        saved_queue = _save_queue(queue_path, queue)
        mailbox_payload = _mailbox_payload(source_name, [])
        return {
            "status": "skipped",
            "reason": "missing_credentials",
            "source_id": source_id,
            "queue_file": str(saved_queue),
            "mailbox": mailbox_payload,
            "queue_count": len(queue),
            "alert_count": 0,
        }

    alerts, events, fetch_status, fetch_error = _fetch_mail_data(
        source, username=username, password=password, access_token=access_token
    )
    queue = _merge_queue(queue, events)
    due_alerts, queue = _due_alerts_from_queue(source, queue, source_name=source_name)
    queue_file = _save_queue(queue_path, queue)

    merged_alerts = _dedup_alerts(alerts + due_alerts)[:max_items]
    mailbox_payload = _mailbox_payload(source_name, merged_alerts)
    return {
        "status": fetch_status,
        "reason": fetch_error,
        "source_id": source_id,
        "queue_file": str(queue_file),
        "mailbox": mailbox_payload,
        "queue_count": len(queue),
        "alert_count": len(merged_alerts),
    }


def _fetch_mail_data(
    source: Dict[str, Any],
    *,
    username: str,
    password: str,
    access_token: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, str]:
    imap_error = ""
    try:
        alerts, events = _fetch_imap(
            source, username=username, password=password, access_token=access_token
        )
        if alerts or events or not bool(source.get("pop3_fallback_enabled", False)):
            return alerts, events, "ok", ""
    except Exception as exc:
        imap_error = str(exc)

    # POP3 has no XOAUTH2 support in the stdlib, so the fallback only applies to Basic auth.
    if access_token:
        return [], [], "failed", imap_error or "imap returned no alerts/events"

    if bool(source.get("pop3_fallback_enabled", False)):
        try:
            alerts, events = _fetch_pop3(source, username=username, password=password)
            return alerts, events, "ok", ""
        except Exception as exc:
            return [], [], "failed", f"imap={imap_error}; pop3={exc}"

    return [], [], "failed", imap_error or "imap returned no alerts/events"


def _source_secret(source: Dict[str, Any], env_key: str, default_env: str, fallback_env: str) -> str:
    env_name = compact_text(source.get(env_key)) or default_env
    return compact_text(os.getenv(env_name)) or compact_text(os.getenv(fallback_env))


def _fetch_imap(
    source: Dict[str, Any],
    *,
    username: str,
    password: str,
    access_token: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    host = compact_text(source.get("host"))
    port = positive_int(source.get("port"), 993)
    folders = source.get("folders", ["INBOX"])
    if not isinstance(folders, list) or not folders:
        folders = ["INBOX"]

    alerts: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    with imaplib.IMAP4_SSL(host, port) as client:
        if access_token:
            # Outlook.com refuses Basic auth, so sign in with the OAuth2 SASL exchange.
            payload = build_xoauth2_payload(username, access_token)
            client.authenticate("XOAUTH2", lambda _challenge: payload)
        else:
            client.login(username, password)
        for folder in folders:
            status, _ = client.select(str(folder), readonly=True)
            if status != "OK":
                continue
            status, data = client.search(None, "ALL")
            if status != "OK" or not data:
                continue
            ids = data[0].split()
            max_messages = positive_int(source.get("max_messages"), 50)
            for msg_id in reversed(ids[-max_messages:]):
                status, fetched = client.fetch(msg_id, "(RFC822)")
                if status != "OK" or not fetched:
                    continue
                blob = _first_message_blob(fetched)
                if not blob:
                    continue
                alert, event = _message_to_records(source, blob)
                if alert:
                    alerts.append(alert)
                if event:
                    events.append(event)
    return _dedup_alerts(alerts), _dedup_events(events)


def _fetch_pop3(
    source: Dict[str, Any],
    *,
    username: str,
    password: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    host = compact_text(source.get("pop_host"))
    port = positive_int(source.get("pop_port"), 995)
    max_messages = positive_int(source.get("max_messages"), 50)

    alerts: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    client = poplib.POP3_SSL(host, port, timeout=20)
    try:
        client.user(username)
        client.pass_(password)
        _, lines, _ = client.list()
        count = len(lines)
        start = max(1, count - max_messages + 1)
        for index in range(count, start - 1, -1):
            _, msg_lines, _ = client.retr(index)
            blob = b"\n".join(msg_lines)
            alert, event = _message_to_records(source, blob)
            if alert:
                alerts.append(alert)
            if event:
                events.append(event)
    finally:
        try:
            client.quit()
        except Exception:
            pass
    return _dedup_alerts(alerts), _dedup_events(events)


def _first_message_blob(fetched: Any) -> bytes | None:
    for part in fetched:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return bytes(part[1])
    return None


def _message_to_records(source: Dict[str, Any], blob: bytes) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    message = message_from_bytes(blob, policy=default)
    received_dt = _parse_mail_datetime(message.get("Date"))
    if received_dt < datetime.now(timezone.utc) - timedelta(hours=_mail_lookback_hours(source)):
        return None, None

    subject = _decode_mime_header(message.get("Subject")) or "(no subject)"
    sender = _decode_mime_header(message.get("From"))
    if _is_ignored_sender(source, sender.lower()) or _is_ignored_subject(source, subject.lower()):
        return None, None

    message_id = _decode_mime_header(message.get("Message-ID"))
    body = _extract_text_body(message)
    action = _detect_action(source, subject=subject, body=body)
    event = _mail_event(
        source,
        sender=sender,
        subject=subject,
        body=body,
        message_id=message_id,
        received_dt=received_dt,
        action=action,
    )

    event_date = _parse_date(compact_text(action.get("event_date"))) if action else None
    if event_date is not None and event_date < datetime.now(timezone.utc).astimezone().date():
        return None, None

    priority, _ = _classify_mail(source, sender=sender, subject=subject, body=body, action_hit=str(action.get("label", "")))
    if priority not in {"Urgent", "Important"}:
        return None, event

    summary = _compose_summary(sender=sender, body=body, event_date=str(action.get("event_date", "")))
    alert = {
        "rank": 0,
        "priority": priority,
        "title_zh": subject,
        "summary_zh": summary,
        "source_name": compact_text(source.get("source_name") or source.get("name")) or "邮箱提醒",
        "published_at": received_dt.astimezone().replace(microsecond=0).isoformat(),
        "url": f"mail:{message_id}" if message_id else "",
        "topic_icon": "📤",
    }
    return alert, event


def _compose_summary(*, sender: str, body: str, event_date: str) -> str:
    parts: List[str] = []
    if sender:
        parts.append(f"发件人：{sender}")
    if event_date:
        parts.append(f"事项日期：{event_date}")
    if body:
        parts.append(body[:180].strip())
    return "；".join(part for part in parts if part).strip("；")


def _decode_mime_header(value: str | None) -> str:
    raw = value or ""
    decoded: List[str] = []
    for text, enc in decode_header(raw):
        if isinstance(text, bytes):
            decoded.append(text.decode(enc or "utf-8", errors="ignore"))
        else:
            decoded.append(str(text))
    return compact_text("".join(decoded))


def _parse_mail_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_text_body(message: Message) -> str:
    texts: List[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        content_type = compact_text(part.get_content_type()).lower()
        disposition = compact_text(part.get("Content-Disposition", "")).lower()
        if "attachment" in disposition or content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            texts.append(payload.decode(charset, errors="ignore"))
        except Exception:
            texts.append(payload.decode("utf-8", errors="ignore"))
    # strip_html drops <script>/<style> blocks wholesale; a bare tag strip would leave
    # CSS declarations ("@media (max-width:600px){...}") inside the body text.
    body = compact_text(strip_html(" ".join(texts)))
    return body[:3000]


def _classify_mail(
    source: Dict[str, Any],
    *,
    sender: str,
    subject: str,
    body: str,
    action_hit: str = "",
) -> Tuple[str, str]:
    combined = f"{subject}\n{body}".lower()
    sender_lower = sender.lower()

    vip_senders = _string_list(source.get("vip_senders", []))
    urgent_keywords = _string_list(source.get("urgent_keywords", []))
    important_keywords = _string_list(source.get("important_keywords", []))

    for vip in vip_senders:
        if vip and vip in sender_lower:
            return "Urgent", vip
    hit = _first_hit(combined, urgent_keywords)
    if hit:
        return "Urgent", hit
    if action_hit and str(action_hit).strip():
        return "Urgent", action_hit
    hit = _first_hit(combined, important_keywords)
    if hit:
        return "Important", hit
    return "FYI", ""


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [compact_text(item).lower() for item in value if compact_text(item)]


def _first_hit(text: str, candidates: Iterable[str]) -> str:
    for candidate in candidates:
        if candidate and candidate in text:
            return candidate
    return ""


def _is_ignored_sender(source: Dict[str, Any], sender_lower: str) -> bool:
    return any(item and item in sender_lower for item in _string_list(source.get("ignored_senders", [])))


def _is_ignored_subject(source: Dict[str, Any], subject_lower: str) -> bool:
    return any(item and item in subject_lower for item in _string_list(source.get("ignored_subject_keywords", [])))


def _detect_action(source: Dict[str, Any], *, subject: str, body: str) -> Dict[str, Any]:
    text = f"{subject}\n{body}"
    lower = text.lower()
    action_words = [
        "meeting", "appointment", "deadline", "due", "review", "call", "interview", "reminder",
        "会议", "开会", "截止", "到期", "电话", "面试", "审批", "提醒",
    ]
    if not any(word in lower for word in action_words):
        return {}

    today = datetime.now(timezone.utc).astimezone().date()
    emit_window_days = int(source.get("event_emit_window_days", 0) or 0)
    relative = _relative_date_hit(lower)
    if relative:
        event_date = _relative_date_to_date(relative, today)
        delta = (event_date - today).days
        return {
            "label": relative,
            "event_date": event_date.isoformat(),
            "is_due": 0 <= delta <= emit_window_days,
            "is_past": delta < 0,
        }

    candidates = _extract_candidate_dates(text, today.year)
    if candidates:
        nearest = min(candidates, key=lambda candidate: abs((candidate - today).days))
        delta = (nearest - today).days
        return {
            "label": nearest.isoformat(),
            "event_date": nearest.isoformat(),
            "is_due": 0 <= delta <= emit_window_days,
            "is_past": delta < 0,
        }
    return {}


def _relative_date_hit(text: str) -> str:
    patterns = [
        ("today", "today"),
        ("tomorrow", "tomorrow"),
        ("今天", "今天"),
        ("今日", "今日"),
        ("明天", "明天"),
        ("明日", "明日"),
    ]
    for pattern, label in patterns:
        if pattern in text:
            return label
    return ""


def _relative_date_to_date(label: str, today: date) -> date:
    if label in {"tomorrow", "明天", "明日"}:
        return today + timedelta(days=1)
    return today


def _extract_candidate_dates(text: str, default_year: int) -> List[date]:
    candidates: List[date] = []
    for match in re.finditer(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text):
        candidates.extend(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
    for match in re.finditer(r"\b(\d{1,2})[-/.](\d{1,2})\b", text):
        candidates.extend(_safe_date(default_year, int(match.group(1)), int(match.group(2))))
    for match in re.finditer(r"(20\d{2})年(\d{1,2})月(\d{1,2})日?", text):
        candidates.extend(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
    for match in re.finditer(r"(\d{1,2})月(\d{1,2})日?", text):
        candidates.extend(_safe_date(default_year, int(match.group(1)), int(match.group(2))))
    return candidates


def _safe_date(year: int, month: int, day: int) -> List[date]:
    try:
        return [date(year, month, day)]
    except ValueError:
        return []


def _mail_lookback_hours(source: Dict[str, Any]) -> int:
    lookback = positive_int(source.get("lookback_hours"), 72)
    if bool(source.get("_mail_queue_exists", False)):
        return lookback
    return positive_int(source.get("initial_backfill_hours"), lookback)


def _mail_event(
    source: Dict[str, Any],
    *,
    sender: str,
    subject: str,
    body: str,
    message_id: str,
    received_dt: datetime,
    action: Dict[str, Any],
) -> Dict[str, Any] | None:
    event_date = compact_text(action.get("event_date"))
    if not event_date:
        return None
    source_id = compact_text(source.get("id")) or "executive_mailbox"
    url = f"mail:{message_id}" if message_id else ""
    return {
        "event_id": make_item_id(source_id, f"{event_date}|{subject}", url or subject),
        "event_date": event_date,
        "source_id": source_id,
        "subject": subject,
        "sender": sender,
        "url": url,
        "received_at": received_dt.replace(microsecond=0).isoformat(),
        "snippet": body[:500],
        "status": "pending",
    }


def _load_queue(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _save_queue(path: Path, items: List[Dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": utc_now_iso(),
                "count": len(items),
                "items": items,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _merge_queue(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in existing + incoming:
        event_id = compact_text(item.get("event_id"))
        if event_id:
            by_id[event_id] = item
    return sorted(by_id.values(), key=lambda item: compact_text(item.get("event_date")))


def _due_alerts_from_queue(
    source: Dict[str, Any],
    queue: List[Dict[str, Any]],
    *,
    source_name: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    today = datetime.now(timezone.utc).astimezone().date()
    emit_window_days = int(source.get("event_emit_window_days", 0) or 0)
    alerts: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []
    for event in queue:
        event_date = _parse_date(compact_text(event.get("event_date")))
        if event_date is None:
            continue
        delta = (event_date - today).days
        if 0 <= delta <= emit_window_days:
            alerts.append({
                "rank": 0,
                "priority": "Urgent",
                "title_zh": compact_text(event.get("subject")) or "(no subject)",
                "summary_zh": _compose_summary(
                    sender=compact_text(event.get("sender")),
                    body=compact_text(event.get("snippet")),
                    event_date=compact_text(event.get("event_date")),
                ),
                "source_name": source_name,
                "published_at": compact_text(event.get("received_at")) or compact_text(event.get("event_date")),
                "url": compact_text(event.get("url")),
                "topic_icon": "📤",
            })
            continue
        if delta >= 0:
            remaining.append(event)
    return alerts, remaining


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _dedup_events(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    output: List[Dict[str, Any]] = []
    for item in items:
        key = compact_text(item.get("event_id"))
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _dedup_alerts(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    output: List[Dict[str, Any]] = []
    for item in items:
        key = compact_text(item.get("url")) or compact_text(item.get("title_zh"))
        lowered = key.lower()
        if not lowered or lowered in seen:
            continue
        seen.add(lowered)
        output.append(item)
    return output


def _mailbox_payload(source_name: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    ranked_items = []
    for index, item in enumerate(items, 1):
        row = dict(item)
        row["rank"] = index
        row["source_name"] = compact_text(row.get("source_name")) or source_name
        row["published_at"] = normalize_iso(row.get("published_at"), fallback=compact_text(row.get("published_at")))
        ranked_items.append(row)
    return {
        "generated_at": utc_now_iso(),
        "count": len(ranked_items),
        "items": ranked_items,
    }
