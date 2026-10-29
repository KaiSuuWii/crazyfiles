
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("holiday-bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
TZ = ZoneInfo("Asia/Manila")

# Shared persistent file; mount /data in both Docker services.
HOLIDAYS_FILE = Path(
    os.getenv("HOLIDAYS_FILE", "/data/holidays.json")
)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def load_holidays():
    HOLIDAYS_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not HOLIDAYS_FILE.exists():
        return []

    with HOLIDAYS_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError("holidays.json must contain a JSON list.")

    return data


def save_holidays(holidays):
    HOLIDAYS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Atomic replacement helps prevent partially written JSON.
    temporary_file = HOLIDAYS_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(sorted(set(holidays)), file, indent=2)
        file.write("\n")

    temporary_file.replace(HOLIDAYS_FILE)


def send_message(chat_id, message):
    response = requests.post(
        f"{API_URL}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
        },
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message.")


def handle_command(message):
    chat_id = str(message.get("chat", {}).get("id", ""))

    # Only accept commands from the configured Telegram chat.
    if chat_id != CHAT_ID:
        logger.warning("Ignored command from unauthorized chat %s", chat_id)
        return

    text = message.get("text", "").strip()

    # Accept /holiday 12/25/2026, including /holiday@YourBotName.
    match = re.fullmatch(
        r"/holiday(?:@\w+)?(?:\s+(\d{2}/\d{2}/\d{4}))?",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        if text.split(maxsplit=1)[0].split("@")[0].lower() == "/holiday":
            send_message(
                chat_id,
                "Usage: /holiday MM/DD/YYYY\n"
                "Example: /holiday 12/25/2026",
            )
        return

    date_text = match.group(1)

    if not date_text:
        send_message(
            chat_id,
            "Usage: /holiday MM/DD/YYYY\n"
            "Example: /holiday 12/25/2026",
        )
        return

    try:
        holiday_date = datetime.strptime(
            date_text, "%m/%d/%Y"
        ).date()
    except ValueError:
        send_message(
            chat_id,
            "❌ Invalid date. Use MM/DD/YYYY, "
            "for example /holiday 12/25/2026.",
        )
        return

    date_key = holiday_date.isoformat()
    holidays = load_holidays()

    if date_key in holidays:
        send_message(
            chat_id,
            f"ℹ️ {holiday_date.strftime('%B %d, %Y')} "
            "is already saved as a holiday.",
        )
        return

    holidays.append(date_key)
    save_holidays(holidays)

    send_message(
        chat_id,
        "🎉 Holiday saved!\n\n"
        f"Date: {holiday_date.strftime('%B %d, %Y')}\n"
        "Attendance checks will be skipped on this date.",
    )

    logger.info("Saved holiday: %s", date_key)


def main():
    offset = None
    logger.info("Holiday command listener started.")

    while True:
        try:
            params = {
                "timeout": 30,
                "allowed_updates": json.dumps(["message"]),
            }

            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"{API_URL}/getUpdates",
                params=params,
                timeout=40,
            )
            response.raise_for_status()
            result = response.json()

            if not result.get("ok"):
                raise RuntimeError("Telegram getUpdates failed.")

            for update in result["result"]:
                offset = update["update_id"] + 1

                try:
                    message = update.get("message")
                    if message:
                        handle_command(message)
                except Exception:
                    logger.exception("Failed to process Telegram command")

        except KeyboardInterrupt:
            logger.info("Holiday command listener stopped.")
            break
        except Exception:
            logger.exception("Telegram polling failed; retrying in 5 seconds.")
            time.sleep(5)


if __name__ == "__main__":
    main()
