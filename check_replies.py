"""
myNSE200 / check_replies.py
------------------------------
A tiny, fast script that does ONLY one thing: checks for new Telegram
replies (BUY/SKIP) and processes them. Doesn't touch prices, fundamentals,
or generate any new signals — that's main.py's job, once a night.

This exists so a "listener" job can run frequently (e.g. every 10 minutes)
without repeating the full, slower nightly pipeline each time — your
BUY/SKIP replies get picked up promptly during the day, not just once
overnight.

Nothing about the strategy itself lives here — same as notifier.py and
portfolio.py, this is purely the tracking/interaction layer.
"""

import db
import notifier

if __name__ == "__main__":
    db.init_db()
    processed = notifier.check_and_process_replies()
    if processed:
        print(f"Processed {len(processed)} Telegram repl{'y' if len(processed) == 1 else 'ies'}: "
              f"{', '.join(f'{action} {sym}' for action, sym, _ in processed)}")
    else:
        print("No new replies to process.")
