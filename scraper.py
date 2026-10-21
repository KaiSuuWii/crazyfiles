
import os
import re
import sys
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

logger = logging.getLogger(__name__)

# ============================================================
# EDIT HERE: Enter the employee names you want to check.
# Use the names as they appear in the attendance table.
# ============================================================
TARGET_NAMES = [
    "Justin John Bonita",
    "Mark Christian Lid-awan",
    "Hennah",
    "Kaiveni Tom",
    "Bernard",
    "Jabez Joshua Vestidas",
    "Ronny"
]

# Philippine time
TZ = ZoneInfo("Asia/Manila")

BASE_URL = "https://desk.echo.priisms.online"
ATTENDANCE_URL = f"{BASE_URL}/attendance"
LEAVE_URL = f"{BASE_URL}/leave/requests"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

USERNAME = os.environ["SITE_USERNAME"]
PASSWORD = os.environ["SITE_PASSWORD"]

# These are configurable if the actual page uses different selectors.
USERNAME_SELECTOR = os.getenv(
    "USERNAME_SELECTOR",
    'input[name="username"], input[placeholder*="username" i], input[type="email"]'
)
PASSWORD_SELECTOR = os.getenv(
    "PASSWORD_SELECTOR",
    'input[name="password"], input[type="password"]'
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("attendance-monitor")


def normalize_name(name):
    """Ignore capitalization and repeated whitespace."""
    return " ".join(name.casefold().split())


def parse_time_in(value):
    """
    Parse values such as:
      Oct. 21 07:32 AM
      Oct 21 07:32 AM
      October 21 07:32 AM

    The website's displayed value has no year.
    Returns (month, day), or None if parsing fails.
    """
    value = value.strip()
    value = re.sub(
        r"^([A-Za-z]{3})\.\s*",
        r"\1 ",
        value
    )

    formats = [
        "%b %d %I:%M %p",
        "%B %d %I:%M %p",
        "%b %d, %I:%M %p",
        "%B %d, %I:%M %p",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.month, parsed.day
        except ValueError:
            continue

    return None


def send_telegram(message):
    """Send a plain-text message to the configured Telegram chat."""
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message")



def login_if_needed(page):
    import os

    username = os.getenv("SITE_USERNAME")
    password = os.getenv("SITE_PASSWORD")

    if not username or not password:
        raise RuntimeError(
            "SITE_USERNAME or SITE_PASSWORD is missing from .env"
        )

    attendance_url = "https://desk.echo.priisms.online/attendance"
    sign_in_url = "https://desk.echo.priisms.online/sign-in"

    page.goto(attendance_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    # If redirected to sign-in, submit the login form.
    if "/sign-in" in page.url:
        logger.info("Sign-in page detected; attempting login.")

        email_input = page.locator('input[type="email"]')
        password_input = page.locator('input[type="password"]')

        email_input.wait_for(state="visible", timeout=15000)
        password_input.wait_for(state="visible", timeout=15000)

        email_input.fill(username)
        password_input.fill(password)

        # Submit the form using its Sign in button.
        page.get_by_role(
            "button",
            name="Sign in",
            exact=True
        ).click()

        # Wait for navigation or the application to update.
        try:
            page.wait_for_url(
                lambda url: "/sign-in" not in str(url),
                timeout=20000
            )
        except Exception:
            page.wait_for_timeout(3000)

    logger.info("URL after login attempt: %s", page.url)

    # Explicitly reject failed authentication rather than reporting
    # everyone as absent.
    if "/sign-in" in page.url:
        try:
            visible_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            visible_text = ""

        raise RuntimeError(
            "Login did not complete. Check credentials and any "
            "visible login error. Page text: " + visible_text[:1000]
        )

    # Ensure the attendance route is loaded after login.
    if "/attendance" not in page.url:
        page.goto(attendance_url, wait_until="domcontentloaded")

    page.wait_for_timeout(3000)
    logger.info("Page ready for attendance extraction: %s", page.url)


def extract_attendance(page):
    """
    Locate the table by its Employee and Time In headers.
    Returns records containing employee and time_in.
    """
    tables = page.locator("table")
    records = []

    for table_index in range(tables.count()):
        table = tables.nth(table_index)

        headers = table.locator("thead th").all_inner_texts()

        # Fallback for tables without a thead.
        if not headers:
            first_row = table.locator("tr").first
            headers = first_row.locator("th, td").all_inner_texts()

        normalized_headers = [
            " ".join(h.casefold().split()).strip()
            for h in headers
        ]

        if "employee" not in normalized_headers:
            continue

        if "time in" not in normalized_headers:
            continue

        employee_index = normalized_headers.index("employee")
        time_index = normalized_headers.index("time in")

        rows = table.locator("tbody tr")

        # If there is no tbody, skip the header row.
        if rows.count() == 0:
            rows = table.locator("tr").nth(1)

        for row_index in range(rows.count()):
            row = rows.nth(row_index)
            cells = row.locator("td").all_inner_texts()

            if len(cells) <= max(employee_index, time_index):
                continue

            records.append({
                "employee": cells[employee_index].strip(),
                "time_in": cells[time_index].strip(),
            })

        if records:
            break

    if not records:
        raise RuntimeError(
            "No attendance rows were extracted. The page may use a "
            "different table layout, be loading data dynamically, "
            "or require pagination/filter configuration. "
            "Refusing to report everyone as missing."
        )

    return records


def extract_leaves(page):
    """
    Extract leave requests from the visible Leave Requests page.
    Supports names joined to leave types, such as 'Jabez Joshua VestidasSICK'.
    Pending requests are included.
    """
    import re
    from datetime import date

    leave_url = f"{BASE_URL}/leave/requests"

    page.goto(leave_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    if "/sign-in" in page.url:
        raise RuntimeError(
            "Redirected to sign-in while opening leave requests."
        )

    body_text = page.locator("body").inner_text(timeout=15000)
    logger.info("Leave page URL: %s", page.url)
    logger.info("Leave page visible text:\n%s", body_text[:10000])

    lines = [
        " ".join(line.split()).strip()
        for line in body_text.splitlines()
        if line.strip()
    ]

    # Handles both single dates and date ranges:
    # 2026-10-21 · 1 day(s)
    # 2026-11-16 – 2026-11-20 · 5 day(s)
    date_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2})"
        r"(?:\s*[–—-]\s*(\d{4}-\d{2}-\d{2}))?"
        r"\s*·\s*(\d+)\s*day\(s\)",
        re.IGNORECASE,
    )

    # The employee name and leave type may be on the same line.
    type_pattern = re.compile(
        r"^(.*?)\s*(SICK(?:\s+LEAVE)?|VACATION(?:\s+LEAVE)?)$",
        re.IGNORECASE,
    )

    records = []

    for index, line in enumerate(lines):
        date_match = date_pattern.search(line)
        if not date_match:
            continue

        start_date = date.fromisoformat(date_match.group(1))
        end_date = (
            date.fromisoformat(date_match.group(2))
            if date_match.group(2)
            else start_date
        )

        employee = None
        leave_type = None

        # Search backward for the request's employee/type line.
        # The status (Pending/Approved/etc.) and reason may be between
        # the employee line and the date line.
        for previous_line in reversed(lines[max(0, index - 6):index]):
            match = type_pattern.match(previous_line)
            if not match:
                continue

            candidate_name = match.group(1).strip()
            candidate_type = match.group(2).upper()

            # Avoid interpreting page filter labels as employees.
            if not candidate_name:
                continue

            if candidate_name.casefold() in {
                "leave", "leave request", "leave requests",
                "status", "kind", "employee", "anyone",
            }:
                continue

            employee = candidate_name
            leave_type = (
                "SICK LEAVE"
                if candidate_type.startswith("SICK")
                else "VACATION LEAVE"
            )
            break

        if not employee:
            logger.warning(
                "Could not identify employee for leave dated %s. "
                "Nearby lines: %s",
                start_date,
                lines[max(0, index - 5):index + 2],
            )
            continue

        # Inclusive duration: a one-day request has one day.
        days = (end_date - start_date).days + 1

        records.append({
            "employee": employee,
            "leave_type": leave_type,
            "start_date": start_date,
            "days": days,
        })

        logger.info(
            "Extracted leave: employee=%s, type=%s, start=%s, days=%s",
            employee,
            leave_type,
            start_date,
            days,
        )

    logger.info("Extracted %d leave requests", len(records))
    return records


def check_attendance(records, target_names, target_date):
    """
    A name is filed only if its Employee and Time In date match.
    Multiple matching rows still count as filed.
    """
    filed = set()

    for record in records:
        employee = record["employee"]
        time_in = record["time_in"]

        parsed_date = parse_time_in(time_in)

        if not employee or parsed_date is None:
            continue

        if parsed_date == (target_date.month, target_date.day):
            filed.add(normalize_name(employee))

    results = {}

    for name in target_names:
        results[name] = (
            "FILED"
            if normalize_name(name) in filed
            else "NOT FOUND"
        )

    return results


def combine_attendance_and_leaves(
    attendance_records,
    leave_records,
    target_names,
    target_date,
):
    """
    Leave requests covering the target date take precedence in the
    displayed report. Otherwise, check attendance as usual.
    """
    attendance_results = check_attendance(
        attendance_records,
        target_names,
        target_date,
    )

    leave_by_name = {}

    for record in leave_records:
        start_date = record["start_date"]
        end_date = datetime.fromordinal(
            start_date.toordinal() + record["days"] - 1
        ).date()

        if not (start_date <= target_date <= end_date):
            continue

        employee_key = normalize_name(record["employee"])

        for target_name in target_names:
            if normalize_name(target_name) == employee_key:
                leave_by_name[target_name] = record["leave_type"]

    results = {}

    for name in target_names:
        if name in leave_by_name:
            results[name] = leave_by_name[name]
        else:
            results[name] = attendance_results[name]

    return results



def build_report(results, target_date, checked_at):
    filed = [n for n, status in results.items() if status == "FILED"]
    sick = [n for n, status in results.items() if status == "SICK LEAVE"]
    vacation = [
        n for n, status in results.items()
        if status == "VACATION LEAVE"
    ]
    missing = [n for n, status in results.items() if status == "NOT FOUND"]

    lines = [
        "📋 ATTENDANCE CHECK",
        f"Date: {target_date.strftime('%b %d, %Y')}",
        f"Checked: {checked_at.strftime('%I:%M %p')} PHT",
        "",
        f"✅ Filed: {len(filed)}",
        f"🤒 Sick Leave: {len(sick)}",
        f"🌴 Vacation Leave: {len(vacation)}",
        f"❌ Not found: {len(missing)}",
        "",
    ]

    if filed:
        lines.append("FILED")
        lines.extend(f"✅ {name}" for name in filed)
        lines.append("")

    if sick:
        lines.append("SICK LEAVE")
        lines.extend(f"🤒 {name}" for name in sick)
        lines.append("")

    if vacation:
        lines.append("VACATION LEAVE")
        lines.extend(f"🌴 {name}" for name in vacation)
        lines.append("")

    if missing:
        lines.append("NOT FOUND")
        lines.extend(f"❌ {name}" for name in missing)

    return "\n".join(lines)



def main():
    target_names = [
        name.strip()
        for name in TARGET_NAMES
        if name.strip()
    ]

    if not target_names:
        raise RuntimeError("TARGET_NAMES is empty.")

    checked_at = datetime.now(TZ)
    target_date = checked_at.date()

    log.info("Checking attendance for %s", target_date)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            login_if_needed(page)

            # Verify we are not still on a login page.
            if page.locator(PASSWORD_SELECTOR).first.is_visible():
                raise RuntimeError(
                    "Still on a login page; no attendance check performed."
                )

            attendance_records = extract_attendance(page)
            log.info(
                "Extracted %d attendance rows",
                len(attendance_records),
            )

            # Then, navigate to leave requests using the same logged-in session.
            leave_records = extract_leaves(page)

            # Combine attendance and leave status for the target date.
            results = combine_attendance_and_leaves(
                attendance_records,
                leave_records,
                target_names,
                target_date,
            )
            report = build_report(
                results,
                target_date,
                checked_at,
            )

            print(report)
            send_telegram(report)
            log.info("Telegram report sent successfully.")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("Attendance check failed")

        # Send an error notice rather than a false absence report.
        try:
            send_telegram(
                "⚠️ Attendance monitor failed.\n"
                f"Reason: {str(exc)[:800]}\n"
                "Please verify the website and scraper."
            )
        except Exception:
            log.exception("Could not send Telegram error notification")

        sys.exit(1)
