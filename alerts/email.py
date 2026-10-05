"""Alert emails: composing them, and sending through Gmail SMTP with an app password.

Settings (.env): GMAIL_ADDRESS, GMAIL_APP_PASSWORD, and optionally ALERT_RECIPIENT (default: GMAIL_ADDRESS).
Check the setup with: python -m alerts.email --test
"""

import argparse
import html
import os
import smtplib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage

from common import config  # noqa: F401  (loads .env)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SUBJECT_PREFIX = "[Trade Tracker] "


@dataclass
class Message:
    subject: str
    text: str
    html: str


@dataclass
class Line:
    """One alerted trade, ready to render."""
    rule: str  # watchlist_buy | held_sale
    trade: dict
    score: int | None
    reasons: list[str]
    entry: str | None = None  # suggested entry timing (buys)


class ConfigError(Exception):
    pass


# --- composing ------------------------------------------------------------------------------------


def amount(low: int | None, high: int | None) -> str:
    if low is None:
        return "amount unknown"
    if high is None:
        return f"over ${low - 1:,}"
    return f"${low:,}" if low == high else f"${low:,}–${high:,}"


def _member(row: dict) -> str:
    tag = "-".join(p for p in (row.get("party"), row.get("state")) if p)
    return f"{row['member_name']} ({tag})" if tag else row["member_name"]


def _what(line: Line) -> str:
    t = line.trade
    if line.rule == "held_sale":
        return f"{'sold part of' if t['action'] == 'SELL_PARTIAL' else 'sold'} {t['symbol']} (you hold it)"
    return f"bought {t['symbol']}" + (" call options" if t["asset_type"] == "option" else "")


def _flags(t: dict) -> list[str]:
    flags = []
    if t["ticker_status"] == "unlisted":
        flags.append("ticker not on NYSE/Nasdaq (OTC or old symbol)")
    if t["ticker_status"] == "renamed":
        flags.append(f"filed as {t['ticker']}")
    if (t["confidence"] or 1) < 1:
        flags.append("low-confidence parse; check the filing")
    return flags


def compose_trades(lines: list[Line]) -> Message:
    """One email for one filing's alerted trades."""
    first = lines[0].trade
    buys = [ln for ln in lines if ln.rule == "watchlist_buy"]
    sales = [ln for ln in lines if ln.rule == "held_sale"]
    parts = []
    if buys:
        symbols = list(dict.fromkeys(ln.trade["symbol"] for ln in buys))
        more = f" (+{len(symbols) - 2})" if len(symbols) > 2 else ""
        parts.append(f"bought {', '.join(symbols[:2])}{more} · score {max(ln.score or 0 for ln in buys)}")
    if sales:
        symbols = list(dict.fromkeys(ln.trade["symbol"] for ln in sales))
        parts.append(f"sold {', '.join(symbols)} (you hold)")
    subject = f"{SUBJECT_PREFIX}{first['member_name']} {'; '.join(parts)}"

    text = [f"{_member(first)} filed a PTR (disclosed {first['disclosure_date']}).", ""]
    rows_html = []
    for ln in lines:
        t = ln.trade
        delay = f"{t['filing_delay_days']} days" if t["filing_delay_days"] is not None else "unknown"
        details = [
            f"Owner: {t['owner'] or '?'} · Amount: {amount(t['amount_min'], t['amount_max'])}",
            f"Traded {t['tx_date'] or '?'} · disclosed {t['disclosure_date'] or '?'} · delay {delay}",
        ]
        if ln.score is not None:
            details.append(f"Score {ln.score}: {', '.join(ln.reasons)}")
        if ln.entry:
            details.append(f"Entry: {ln.entry}")
        details += [f"Note: {f}" for f in _flags(t)]
        asset = t["asset_name"] or ""
        text += [f"* {_what(ln).upper()}  {asset}"] + [f"    {d}" for d in details] + [""]
        rows_html.append(
            f"<tr><td style='padding:8px;border-bottom:1px solid #ddd'><b>{html.escape(_what(ln))}</b><br>"
            f"<span style='color:#666'>{html.escape(asset)}</span><br>"
            + "<br>".join(html.escape(d) for d in details) + "</td></tr>"
        )
    footer = [f"Filing: {first['source_url'] or 'n/a'}", f"First seen: {first['first_seen_at']}"]
    text += footer
    body = (
        f"<p>{html.escape(_member(first))} filed a PTR (disclosed {html.escape(first['disclosure_date'] or '?')}).</p>"
        f"<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>{''.join(rows_html)}</table>"
        f"<p><a href='{html.escape(first['source_url'] or '')}'>Open the filing</a> · "
        f"first seen {html.escape(first['first_seen_at'])}</p>"
    )
    return Message(subject, "\n".join(text), body)


def compose_scan(filing: dict) -> Message:
    who = _member(filing)
    subject = f"{SUBJECT_PREFIX}{filing['member_name']} filed a scanned PTR (open the PDF)"
    text = (f"{who} filed a scanned (paper) PTR on {filing['filing_date'] or '?'}. Its trades can't be read "
            f"automatically yet, so open it:\n{filing['source_url']}\n\nFirst seen: {filing['first_seen_at']}")
    body = (f"<p>{html.escape(who)} filed a <b>scanned</b> PTR on {html.escape(filing['filing_date'] or '?')}. "
            f"Its trades can't be read automatically yet.</p>"
            f"<p><a href='{html.escape(filing['source_url'] or '')}'>Open the PDF</a> · "
            f"first seen {html.escape(filing['first_seen_at'])}</p>")
    return Message(subject, text, body)


# --- sending --------------------------------------------------------------------------------------


def settings() -> tuple[str, str, str]:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not address or not password:
        raise ConfigError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env (see .env.example)")
    return address, password, os.environ.get("ALERT_RECIPIENT", "").strip() or address


def gmail_sender() -> Callable[[Message], None]:
    """A send(message) function; raises ConfigError now if the settings are missing."""
    address, password, recipient = settings()

    def send(message: Message) -> None:
        email = EmailMessage()
        email["Subject"], email["From"], email["To"] = message.subject, address, recipient
        email.set_content(message.text)
        email.add_alternative(message.html, subtype="html")
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(address, password)
            smtp.send_message(email)

    return send


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gmail alert sender")
    parser.add_argument("--test", action="store_true", help="send a test email to ALERT_RECIPIENT")
    args = parser.parse_args(argv)
    if not args.test:
        parser.print_help()
        return 0
    try:
        send = gmail_sender()
        send(Message(f"{SUBJECT_PREFIX}test", "Email alerts are set up.", "<p>Email alerts are set up.</p>"))
    except (ConfigError, smtplib.SMTPException, OSError) as e:
        print(f"Sending failed: {e}", file=sys.stderr)
        return 1
    print(f"Sent a test email to {settings()[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
