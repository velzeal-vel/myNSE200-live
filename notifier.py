"""
myNSE200 / notifier.py
------------------------
Telegram notifications. SIMPLIFIED (Aug 2026): no more BUY/SKIP reply
handling, no pending list, no portfolio summary — just two things:
new buy signals (with everything needed to act on them), and exit alerts
when a tracked position hits target/stop/time. Nothing here changes what
Apex200 decides — purely how results are communicated.
"""

import logging
from datetime import datetime, timedelta

import requests

import config

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("notifier")


def send_telegram(message, parse_mode="HTML"):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": parse_mode,
        }, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def _estimated_exit_date():
    # Rough calendar-day estimate for MAX_HOLDING_DAYS trading days (~1.4x for weekends)
    return (datetime.now() + timedelta(days=int(config.MAX_HOLDING_DAYS * 1.45))).strftime("%Y-%m-%d")


def build_signal_message(run_date, new_signals, just_closed):
    lines = [f"<b>Apex200 — {run_date}</b>\n"]

    if just_closed:
        lines.append("🔴 <b>EXIT</b>")
        reason_labels = {"closed_target": "TARGET HIT", "closed_stop": "STOP HIT", "closed_time": "TIME EXIT"}
        for pos in just_closed:
            pnl_pct = (pos["exit_price"] / pos["buy_price"] - 1) * 100 if pos["buy_price"] else 0.0
            lines.append(f"• <b>{pos['symbol']}</b> — {reason_labels.get(pos['exit_reason'], pos['exit_reason'])}")
            lines.append(f"    Entry ₹{pos['buy_price']:.2f} → Exit ₹{pos['exit_price']:.2f}  ({pnl_pct:+.1f}%)")
        lines.append("")

    lines.append("🆕 <b>BUY SIGNAL</b>")
    if new_signals:
        for sig in new_signals:
            lines.append(f"• <b>{sig['symbol']}</b>")
            lines.append(f"    Entry (est.): ₹{sig['entry_price']:.2f}")
            lines.append(f"    SL: ₹{sig['stop_loss']:.2f}  ·  Target: ₹{sig['target_price']:.2f}")
            lines.append(f"    Exit by: {_estimated_exit_date()} (sooner if SL/target hits first)")
    else:
        lines.append("None today.")

    return "\n".join(lines)


def notify_signals(run_date, new_signals, just_closed):
    message = build_signal_message(run_date, new_signals, just_closed)
    sent = send_telegram(message)
    if not sent:
        log.info("Telegram not configured or failed — skipping notification.")
    return sent
