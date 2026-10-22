"""app/notifications/builder.py — Builds email payloads from chat session data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.chat.models import ChatMessage, ChatSession, MessageRole, Severity


@dataclass
class NotificationPayload:
    subject: str
    html: str
    plain: str


_SEVERITY_BADGE = {
    Severity.green: "✅ Self-care at home",
    Severity.amber: "⚠️ See a GP soon",
    Severity.red: "🚨 Call 999 / A&E",
}
_SEVERITY_COLOUR = {Severity.green: "#10B981", Severity.amber: "#F59E0B", Severity.red: "#EF4444"}
_SEVERITY_BG = {Severity.green: "#F0FDF4", Severity.amber: "#FFFBEB", Severity.red: "#FEF2F2"}


def _dominant_severity(messages: list[ChatMessage]) -> str:
    sevs = [m.severity for m in messages if m.role == MessageRole.assistant and m.severity]
    if Severity.red in sevs:
        return Severity.red
    if Severity.amber in sevs:
        return Severity.amber
    return Severity.green


def _all_citations(messages: list[ChatMessage]) -> list[dict]:
    seen, result = set(), []
    for msg in messages:
        if msg.role != MessageRole.assistant or not msg.citations:
            continue
        try:
            cits = json.loads(msg.citations)
        except Exception:
            continue
        for c in cits:
            k = c.get("source", "")
            if k and k not in seen:
                seen.add(k)
                result.append(c)
    return result


def _fmt_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%-d %B %Y at %H:%M UTC")


def _first_para(text: str, max_chars: int = 300) -> str:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paras:
        return text[:max_chars]
    p = paras[0]
    return p if len(p) <= max_chars else p[:max_chars].rsplit(" ", 1)[0] + "…"


def _base(title: str, body: str, frontend_url: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>{title}</title>
<style>
body{{font-family:-apple-system,sans-serif;background:#F4F7FF;margin:0;padding:24px 0;color:#0F172A}}
.wrap{{max-width:580px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)}}
.hdr{{background:#080D1A;padding:24px 32px;color:#D4A017;font-family:Georgia,serif;font-size:1.15rem;font-weight:700}}
.hdr span{{color:#E2E8F8;font-size:.75rem;font-family:sans-serif;font-weight:400;display:block;margin-top:4px;opacity:.7}}
.body{{padding:28px 32px;line-height:1.65}}
h2{{font-family:Georgia,serif;font-size:1.15rem;color:#0F172A;margin:0 0 6px}}
p{{margin:0 0 14px;color:#475569;font-size:.88rem}}
.sev{{display:inline-block;padding:5px 14px;border-radius:99px;font-size:.78rem;font-weight:700;margin:10px 0 18px}}
.card{{background:#F8FAFF;border:1px solid #DDE4F0;border-radius:10px;padding:16px 18px;margin-bottom:14px}}
.card p{{color:#0F172A;margin:0 0 8px;font-size:.84rem}}
.cit-lbl{{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#94A3B8;margin-bottom:6px}}
.cit{{display:inline-block;padding:2px 9px;margin:2px;background:#EEF2FF;border:1px solid #C7D2FE;border-radius:5px;font-size:.65rem;color:#4338CA}}
.btn{{display:inline-block;padding:11px 24px;margin:8px 0;background:#D4A017;color:#070C18;border-radius:9px;font-weight:700;font-size:.85rem;text-decoration:none}}
.ftr{{padding:18px 32px;background:#F8FAFF;border-top:1px solid #EEF2FF;font-size:.7rem;color:#94A3B8;line-height:1.6}}
</style></head><body>
<div class="wrap">
<div class="hdr">🤚 HandyDoctor<span>AI-powered medical triage</span></div>
<div class="body">{body}</div>
<div class="ftr">HandyDoctor is not a substitute for professional medical advice.
Always consult a qualified clinician.<br>
<a href="{frontend_url}" style="color:#D4A017">handydoctor.app</a>
&nbsp;·&nbsp;
<a href="{frontend_url}/#account-settings&tab=notifications" style="color:#94A3B8">Manage notifications</a>
</div></div></body></html>"""


def build_session_summary(
    session: ChatSession,
    messages: list[ChatMessage],
    user_name: str,
    frontend_url: str,
) -> NotificationPayload:
    asst_msgs = [m for m in messages if m.role == MessageRole.assistant]
    dominant_sev = _dominant_severity(messages)
    citations = _all_citations(messages)
    badge_text = _SEVERITY_BADGE.get(dominant_sev, "✅ Self-care at home")
    badge_colour = _SEVERITY_COLOUR.get(dominant_sev, "#10B981")
    badge_bg = _SEVERITY_BG.get(dominant_sev, "#F0FDF4")

    cards_html = ""
    for msg in asst_msgs[:3]:
        sev_col = _SEVERITY_COLOUR.get(msg.severity or dominant_sev, "#10B981")
        sev_lbl = _SEVERITY_BADGE.get(msg.severity or dominant_sev, "")
        cards_html += f"""<div class="card"><p>{_first_para(msg.content)}</p>
<span style="font-size:.7rem;font-weight:700;color:{sev_col}">{sev_lbl}</span></div>"""

    cit_html = ""
    if citations:
        cit_html = '<div class="cit-lbl">Sources used</div>'
        for c in citations[:6]:
            url = c.get("url", "")
            src = c.get("source", "Unknown")
            lnk = f'<a href="{url}" style="text-decoration:none">' if url else ""
            end = "</a>" if url else ""
            cit_html += f'{lnk}<span class="cit">📄 {src}</span>{end}'

    session_date = _fmt_time(session.created_at)
    first_name = user_name.split()[0]

    body = f"""<h2>Your consultation summary</h2>
<p>Hi {first_name}, here's a summary of your session from {session_date}.</p>
<p><strong style="color:#0F172A">{session.title}</strong></p>
<span class="sev" style="background:{badge_bg};color:{badge_colour}">{badge_text}</span>
{cards_html}{cit_html}
<p style="margin-top:18px">If symptoms change or worsen, seek professional advice promptly.</p>
<a class="btn" href="{frontend_url}/#past">View full consultation</a>
&nbsp;<a href="{frontend_url}/#chat" style="font-size:.82rem;color:#D4A017;text-decoration:none">New chat →</a>"""

    plain = (
        f"HandyDoctor Consultation Summary\n{'=' * 40}\n\nSession: {session.title}\n"
        f"Date: {session_date}\nTriage: {badge_text}\n\n"
        + "\n\n".join(_first_para(m.content) for m in asst_msgs[:3])
        + (
            "\n\nSources:\n" + "\n".join(f"  • {c.get('source', '')}" for c in citations)
            if citations
            else ""
        )
        + f"\n\nView consultation: {frontend_url}/#past\n"
        + "\nNot a substitute for professional medical advice.\n"
    )

    return NotificationPayload(
        subject=f"Your HandyDoctor summary: {session.title}",
        html=_base(f"Summary — {session.title}", body, frontend_url),
        plain=plain,
    )


def build_followup_reminder(
    session: ChatSession,
    messages: list[ChatMessage],
    user_name: str,
    frontend_url: str,
    hours_since: int,
) -> NotificationPayload:
    dominant_sev = _dominant_severity(messages)
    badge_text = _SEVERITY_BADGE.get(dominant_sev, "✅ Self-care at home")
    last_ai = next((m for m in reversed(messages) if m.role == MessageRole.assistant), None)
    excerpt = _first_para(last_ai.content) if last_ai else ""
    first_name = user_name.split()[0]

    if dominant_sev == Severity.amber:
        urgency = (
            "Your previous consultation recommended seeing a GP soon. "
            "If you haven't already, please book an appointment."
        )
        cta_label, cta_url = "Find nearby care", f"{frontend_url}/#hospitals"
    else:
        urgency = (
            "Your previous consultation recommended self-care. "
            "If symptoms haven't improved or worsened, it's time to see a GP."
        )
        cta_label, cta_url = "New consultation", f"{frontend_url}/#chat"

    body = f"""<h2>Following up on your recent consultation</h2>
<p>Hi {first_name},</p>
<p>It's been {hours_since} hours since your HandyDoctor consultation about
<strong style="color:#0F172A">"{session.title}"</strong>. We wanted to check in.</p>
<div class="card">
<p style="font-size:.75rem;color:#94A3B8;margin-bottom:6px">Original advice · {badge_text}</p>
<p>{excerpt}</p></div>
<p>{urgency}</p>
<p>If you have new or worsening symptoms, start a new consultation for updated guidance.</p>
<a class="btn" href="{cta_url}">{cta_label}</a>
&nbsp;<a href="{frontend_url}/#chat" style="font-size:.82rem;color:#D4A017;text-decoration:none">New consultation →</a>
<p style="margin-top:18px;font-size:.78rem;color:#94A3B8">
In an emergency call 999. For urgent non-emergency advice call NHS 111.</p>"""

    plain = (
        f"HandyDoctor Follow-up Reminder\n{'=' * 40}\n\n"
        f"It's been {hours_since} hours since: {session.title}\n\n"
        f"Original advice: {badge_text}\n{excerpt}\n\n"
        f"{urgency}\n\n"
        f"New consultation: {frontend_url}/#chat\n"
        f"Find nearby care: {frontend_url}/#hospitals\n\n"
        "Not a substitute for professional medical advice.\n"
    )

    return NotificationPayload(
        subject=f"Following up: {session.title}",
        html=_base(f"Follow-up — {session.title}", body, frontend_url),
        plain=plain,
    )
