"""
myNSE200 / notifier.py
------------------------
Telegram notifications AND two-way interaction: sends the nightly signal
report in a portfolio-style format, reads your replies (BUY/SKIP), and
tracks what happens to positions after that — via portfolio.py. None of
this changes what Apex200 decides to signal; it's purely how results are
communicated and tracked.
"""

import logging
import re
from datetime import datetime, timedelta

import requests

import config
import db
import portfolio

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("notifier")

BUY_PATTERN = re.compile(r"^\s*BUY\s+([A-Za-z0-9&\-]+)\s+([\d.]+)\s*$", re.IGNORECASE)
SKIP_PATTERN = re.compile(r"^\s*SKIP\s+([A-Za-z0-9&\-]+)\s*$", re.IGNORECASE)


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


# ----------------------------------------------------------------------
# Reply handling — BUY TICKER PRICE / SKIP TICKER
# ----------------------------------------------------------------------
def _get_last_update_id():
    conn = db.get_conn()
    row = conn.execute("SELECT value FROM telegram_state WHERE key='last_update_id'").fetchone()
    conn.close()
    return int(row[0]) if row else 0


def _set_last_update_id(update_id):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO telegram_state (key, value) VALUES ('last_update_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(update_id),),
    )
    conn.commit()
    conn.close()


def check_and_process_replies():
    """Polls Telegram for new messages since the last check, looks for
    'BUY TICKER PRICE' or 'SKIP TICKER', and updates the matching pending
    position. Safe to call anytime (double-click launcher, nightly run,
    manual) — only ever processes each message once."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return []

    last_id = _get_last_update_id()
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": last_id + 1, "timeout": 5}, timeout=15)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as e:
        log.error(f"Telegram getUpdates failed: {e}")
        return []

    processed = []
    highest_id = last_id
    conn = db.get_conn()

    for update in updates:
        highest_id = max(highest_id, update.get("update_id", highest_id))
        msg = update.get("message", {})
        text = msg.get("text", "")
        if not text:
            continue

        buy_match = BUY_PATTERN.match(text)
        skip_match = SKIP_PATTERN.match(text)

        if buy_match:
            symbol, price = buy_match.group(1).upper(), float(buy_match.group(2))
            row = conn.execute(
                "SELECT id, quantity_recommended FROM positions WHERE symbol=? AND status='pending' "
                "ORDER BY signal_date DESC LIMIT 1", (symbol,)
            ).fetchone()
            if row:
                pos_id, qty = row
                conn.execute(
                    "UPDATE positions SET status='holding', buy_price=?, buy_date=?, "
                    "quantity_bought=?, last_updated=? WHERE id=?",
                    (price, datetime.now().strftime("%Y-%m-%d"), qty, datetime.now().isoformat(), pos_id),
                )
                conn.commit()
                send_telegram(f"✅ Confirmed: <b>{symbol}</b> bought at ₹{price}, qty {qty}. Now tracking as a holding.")
                processed.append(("BUY", symbol, price))
            else:
                send_telegram(f"⚠️ No pending signal found for <b>{symbol}</b> — nothing to confirm.")

        elif skip_match:
            symbol = skip_match.group(1).upper()
            row = conn.execute(
                "SELECT id FROM positions WHERE symbol=? AND status='pending' "
                "ORDER BY signal_date DESC LIMIT 1", (symbol,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE positions SET status='skipped', last_updated=? WHERE id=?",
                    (datetime.now().isoformat(), row[0]),
                )
                conn.commit()
                send_telegram(f"👍 Skipped <b>{symbol}</b> — removed from your pending list.")
                processed.append(("SKIP", symbol, None))
            else:
                send_telegram(f"⚠️ No pending signal found for <b>{symbol}</b> — nothing to skip.")

    conn.close()
    if highest_id > last_id:
        _set_last_update_id(highest_id)
    return processed


# ----------------------------------------------------------------------
# Portfolio-style message formatting
# ----------------------------------------------------------------------
def _current_price(symbol):
    conn = db.get_conn()
    price = portfolio.get_latest_price(conn, symbol)
    conn.close()
    return price


def _estimated_exit_date(from_date_str=None):
    base = datetime.fromisoformat(from_date_str) if from_date_str else datetime.now()
    # Rough calendar-day estimate for MAX_HOLDING_DAYS trading days (~1.4x for weekends)
    return (base + timedelta(days=int(config.MAX_HOLDING_DAYS * 1.45))).strftime("%Y-%m-%d")


def build_portfolio_message(run_date, new_symbols, just_closed):
    lines = [f"<b>Apex200 — {run_date}</b>\n"]

    # --- NEW SIGNALS ---
    lines.append("🆕 <b>NEW SIGNALS (found today)</b>")
    if new_symbols:
        for sym in new_symbols:
            lines.append(f"• <b>{sym}</b>")
    else:
        lines.append("None today.")
    lines.append("")

    # --- PENDING LIST ---
    pending = portfolio.get_pending()
    lines.append("📋 <b>PENDING LIST</b> — reply <code>BUY TICKER PRICE</code> to confirm, "
                  "<code>SKIP TICKER</code> to cancel")
    if pending.empty:
        lines.append("None.")
    else:
        for _, p in pending.iterrows():
            now_price = _current_price(p["symbol"]) or p["signal_price"]
            change_pct = (now_price / p["signal_price"] - 1) * 100 if p["signal_price"] else 0.0
            lines.append(f"• <b>{p['symbol']}</b> — signaled ₹{p['signal_price']:.2f}, "
                          f"now ₹{now_price:.2f} ({change_pct:+.1f}%)")
            if p["quantity_recommended"] and p["quantity_recommended"] > 0:
                cap_pct = (p["signal_price"] * p["quantity_recommended"] / config.ACCOUNT_CAPITAL * 100)
                lines.append(f"    Recommended qty: ~{int(p['quantity_recommended'])} sh")
            else:
                over_pct = (p["signal_price"] / config.ACCOUNT_CAPITAL * 100)
                lines.append(f"    Qty: 0 — OVER CAP (1 sh = {over_pct:.0f}% of capital)")
            lines.append(f"    SL ₹{p['stop_loss']:.2f}  ·  TGT ₹{p['target_price']:.2f}")
            lines.append(f"    If bought now: exits by ~{_estimated_exit_date()} at the latest")
    lines.append("")

    # --- HOLDING ---
    holding = portfolio.get_holding()
    lines.append(f"📈 <b>HOLDING ({len(holding)})</b>")
    if holding.empty:
        lines.append("None.")
    else:
        for _, h in holding.iterrows():
            now_price = _current_price(h["symbol"]) or h["buy_price"]
            change_pct = (now_price / h["buy_price"] - 1) * 100 if h["buy_price"] else 0.0
            pnl = (now_price - h["buy_price"]) * h["quantity_bought"]
            lines.append(f"• <b>{h['symbol']}</b> — bought ₹{h['buy_price']:.2f} x {int(h['quantity_bought'])}, "
                          f"now ₹{now_price:.2f} ({change_pct:+.1f}%, ₹{pnl:+.0f})")
            lines.append(f"    SL ₹{h['stop_loss']:.2f}  ·  TGT ₹{h['target_price']:.2f}  ·  "
                          f"exits by ~{_estimated_exit_date(h['buy_date'])}")
    lines.append("")

    # --- EXIT (only shown if something closed this run) ---
    if just_closed:
        lines.append("🔴 <b>EXIT</b>")
        reason_labels = {"closed_target": "TARGET HIT", "closed_stop": "STOP HIT", "closed_time": "TIME EXIT"}
        for pos in just_closed:
            pnl = (pos["exit_price"] - pos["buy_price"]) * pos["quantity_bought"]
            pnl_pct = (pos["exit_price"] / pos["buy_price"] - 1) * 100 if pos["buy_price"] else 0.0
            lines.append(f"• <b>{pos['symbol']}</b> — {reason_labels.get(pos['exit_reason'], pos['exit_reason'])} "
                          f"at ₹{pos['exit_price']:.2f} ({pnl_pct:+.1f}%, ₹{pnl:+.0f})")
        lines.append("")

    # --- PORTFOLIO ---
    summary = portfolio.compute_portfolio_summary()
    lines.append("💼 <b>PORTFOLIO</b>")
    lines.append(f"₹{summary['equity']:,.0f} ({summary['change_pct']:+.1f}%, ₹{summary['change']:+,.0f})")
    lines.append(f"Open {summary['open_count']}/{summary['max_positions']} · "
                 f"Pending {summary['pending_count']} · Cash ₹{summary['cash']:,.0f}")

    return "\n".join(lines)


def notify_portfolio(run_date, new_symbols, just_closed):
    message = build_portfolio_message(run_date, new_symbols, just_closed)
    sent = send_telegram(message)
    if not sent:
        log.info("Telegram not configured or failed — skipping notification.")
    return sent


# ----------------------------------------------------------------------
# Old simple format — kept for backward compatibility if needed elsewhere
# ----------------------------------------------------------------------
def format_buy_list_message(report_df, run_date):
    buys = report_df[report_df["status"]]
    if buys.empty:
        return f"*Apex200 — {run_date}*\nNo qualifying buy signals tonight. Sit on your hands. 🙂"

    lines = [f"*Apex200 — {run_date}*", f"{len(buys)} BUY signal(s) for tomorrow:\n"]
    for _, r in buys.iterrows():
        lines.append(
            f"*{r['symbol']}*  score {r['overall_score']:.1f}\n"
            f"  Entry: ₹{r['entry_price']}  |  SL: ₹{r['stop_loss']}  |  "
            f"Target: ₹{r['target_price']}  |  Qty: {int(r['quantity'])}\n"
        )
    lines.append(
        "\n_Entry = signal close; place order relative to tomorrow's open. "
        "Not investment advice — verify before placing any order._"
    )
    return "\n".join(lines)


def notify(report_df, run_date):
    message = format_buy_list_message(report_df, run_date)
    sent = send_telegram(message, parse_mode="Markdown")
    if not sent:
        log.info("Telegram not configured or failed — skipping notification.")
    return sent
