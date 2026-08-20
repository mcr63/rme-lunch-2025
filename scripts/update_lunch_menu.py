"""Fetch LINQ Connect lunch entrees and write an ICS calendar.

Pulls a rolling window of menu data (past week through 4 weeks out) and
writes lunch-menu.ics with one all-day event per school day listing the
Main Entree options in the summary and the sides, vegetables, fruit, milk,
and condiments in the description. Designed to run daily from GitHub
Actions or cron; output is deterministic so git only sees a diff when the
menu changes.

Environment variables (all optional, defaults are Alpine SD):
    LINQ_IDENTIFIER   district code            default TFCNC9
    LINQ_DISTRICT_ID  district GUID            default Alpine
    LINQ_BUILDING_ID  school building GUID     default from capture
    ICS_PATH          output path              default lunch-menu.ics
    CAL_NAME          calendar display name    default "School Lunch"
    LINQ_RETRIES      attempts per chunk       default 4
    LINQ_PROXY        http(s) proxy for the    default none
                      LINQ calls, e.g. when
                      the runner's own egress
                      is blocked by the WAF
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta

import requests

BASE_URL = "https://api.linqconnect.com/api"

DISTRICT_ID = os.environ.get("LINQ_DISTRICT_ID", "a83d5cd9-a7a8-ed11-8e69-da0395d724bd")
BUILDING_ID = os.environ.get("LINQ_BUILDING_ID", "da12ddae-57ad-ed11-8e6a-9bfa3b2b51d1")
ICS_PATH = os.environ.get("ICS_PATH", "RME-Lunch.ics")
CAL_NAME = os.environ.get("CAL_NAME", "Rocky Mountain Lunch (RME)")

# Non-entree categories, in the order they read best in the description.
# Anything the API returns outside this list still shows up, after these.
SIDE_ORDER = ["Side", "Vegetable", "Fruit", "Milk", "Condiments"]

RETRIES = int(os.environ.get("LINQ_RETRIES", "4"))
PROXY = os.environ.get("LINQ_PROXY", "").strip()

# LINQ Connect sits behind a WAF that scores requests on how browser-like they
# look. A bare python-requests call (or one with a half-populated header set)
# gets a flat 403 from some networks -- notably GitHub-hosted runners. These
# headers mirror, field for field and in order, what Chrome sends from
# https://linqconnect.com, which is the request the API actually expects.
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://linqconnect.com",
    "Referer": "https://linqconnect.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
}


class MenuFetchError(RuntimeError):
    """The API could not be reached or refused us -- distinct from 'no menu'."""


def fmt_date(d: date) -> str:
    return f"{d.month}-{d.day}-{d.year}"


def _describe(resp: requests.Response) -> str:
    """Enough of the response to tell a WAF block from a real API error."""
    interesting = ("cf-ray", "cf-mitigated", "server", "x-amzn-waf-action", "content-type")
    seen = {k: v for k, v in resp.headers.items() if k.lower() in interesting}
    body = (resp.text or "")[:400].replace("\n", " ").strip()
    return f"HTTP {resp.status_code} {seen} body={body!r}"


def fetch_menu(start: date, end: date, session: requests.Session) -> dict:
    """GET one chunk, retrying transient failures and WAF blocks with backoff."""
    params = {
        "buildingId": BUILDING_ID,
        "districtId": DISTRICT_ID,
        "startDate": fmt_date(start),
        "endDate": fmt_date(end),
    }
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            resp = session.get(
                f"{BASE_URL}/FamilyMenu",
                params=params,
                headers={**HEADERS, "Linq-Nutrition-Url": str(uuid.uuid4())},
                timeout=60,
            )
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    last = f"bad JSON: {exc}; {_describe(resp)}"
                else:
                    last = ""
            elif resp.status_code in (403, 429, 500, 502, 503, 504):
                last = _describe(resp)
            else:
                # 4xx that retrying will not fix (bad GUID, bad date format).
                raise MenuFetchError(f"{start}..{end}: {_describe(resp)}")
        if attempt < RETRIES:
            delay = 2 ** attempt
            print(
                f"WARN: fetch {start}..{end} attempt {attempt}/{RETRIES} failed "
                f"({last}); retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise MenuFetchError(f"{start}..{end}: gave up after {RETRIES} attempts: {last}")


def extract_lunch_menu(menu: dict) -> dict[date, dict[str, list[str]]]:
    """{date: {category name: [recipe names]}} for the Lunch session."""
    out: dict[date, dict[str, list[str]]] = {}
    for sess in menu.get("FamilyMenuSessions", []):
        if sess.get("ServingSession", "").strip().lower() != "lunch":
            continue
        for plan in sess.get("MenuPlans", []):
            for day in plan.get("Days", []):
                try:
                    d = datetime.strptime(day["Date"], "%m/%d/%Y").date()
                except (KeyError, ValueError):
                    continue
                cats = out.setdefault(d, {})
                for meal in day.get("MenuMeals", []):
                    for cat in meal.get("RecipeCategories", []):
                        label = (cat.get("CategoryName") or "").strip()
                        if not label:
                            continue
                        names = cats.setdefault(label, [])
                        for r in cat.get("Recipes", []):
                            name = (r.get("RecipeName") or "").strip()
                            if name and name not in names:
                                names.append(name)
                if not cats:
                    del out[d]
    return out


def split_categories(cats: dict[str, list[str]]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Separate entrees (summary) from everything else (description)."""
    entrees: list[str] = []
    sides: list[tuple[str, list[str]]] = []
    for label, names in cats.items():
        if not names:
            continue
        if "entree" in label.lower():
            for n in names:
                if n not in entrees:
                    entrees.append(n)
        else:
            sides.append((label, names))
    # Known categories first in SIDE_ORDER, unknown ones after, alphabetically,
    # so the description stays stable run to run.
    def sort_key(item: tuple[str, list[str]]) -> tuple[int, str]:
        label = item[0]
        for i, known in enumerate(SIDE_ORDER):
            if label.lower() == known.lower():
                return (i, "")
        return (len(SIDE_ORDER), label.lower())

    sides.sort(key=sort_key)
    return entrees, sides


def ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts = []
    # First line gets the full 75; continuations are prefixed with a space
    # that counts against the limit, so they only get 74.
    limit = 75
    while encoded:
        chunk = encoded[:limit]
        # don't split mid multi-byte char
        while True:
            try:
                parts.append(chunk.decode("utf-8"))
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        encoded = encoded[len(chunk):]
        limit = 74
    return "\r\n ".join(parts)


def build_ics(menu_by_day: dict[date, dict[str, list[str]]]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Matt//Rocky Mountain Lunch//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(CAL_NAME)}",
        "X-WR-TIMEZONE:America/Denver",
    ]
    for d in sorted(menu_by_day):
        entrees, sides = split_categories(menu_by_day[d])
        if not entrees:
            continue
        summary = "| ".join(entrees)
        if sides:
            description = "\n".join(f"{label}: {', '.join(names)}" for label, names in sides)
        else:
            description = "See LINQ Connect for sides, fruit, milk, and condiments."
        dtstart = d.strftime("%Y%m%d")
        dtend = (d + timedelta(days=1)).strftime("%Y%m%d")
        lines += [
            "BEGIN:VEVENT",
            # Matches the hand-built file's UID scheme so existing
            # subscribers get in-place updates, not duplicates.
            f"UID:{dtstart}-rme-lunch@linq",
            # Fixed DTSTAMP keeps output deterministic; only menu changes diff.
            f"DTSTAMP:{dtstart}T000000Z",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"SUMMARY:{ics_escape(summary)}",
            f"DESCRIPTION:{ics_escape(description)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def main() -> int:
    today = date.today()
    start = today - timedelta(days=7)
    end = today + timedelta(days=35)

    # Fetch in one-week chunks; matches app behavior and keeps payloads sane.
    session = requests.Session()
    if PROXY:
        session.proxies.update({"http": PROXY, "https": PROXY})
        print(f"Routing LINQ requests through {PROXY.split('@')[-1]}")

    menu_by_day: dict[date, dict[str, list[str]]] = {}
    chunks = 0
    failures: list[str] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=6), end)
        chunks += 1
        try:
            menu = fetch_menu(chunk_start, chunk_end, session)
        except MenuFetchError as exc:
            print(f"WARN: {exc}", file=sys.stderr)
            failures.append(str(exc))
        else:
            menu_by_day.update(extract_lunch_menu(menu))
        chunk_start = chunk_end + timedelta(days=1)

    # Every single chunk failed -> this is a transport/WAF problem, not an empty
    # menu. Say so plainly instead of blaming the school for not posting lunch.
    if failures and len(failures) == chunks:
        print(
            f"ERROR: all {chunks} requests to {BASE_URL} failed; the API never "
            f"answered with menu data. First failure: {failures[0]}",
            file=sys.stderr,
        )
        if "HTTP 403" in failures[0]:
            print(
                "HINT: a blanket 403 usually means the LINQ WAF is refusing this "
                "network rather than this request. GitHub-hosted runners live in "
                "Azure ranges that are commonly blocked. Set LINQ_PROXY to an "
                "allowed egress, or run this job from a self-hosted runner.",
                file=sys.stderr,
            )
        return 1

    if failures:
        print(
            f"WARN: {len(failures)}/{chunks} chunks failed; building ICS from the rest",
            file=sys.stderr,
        )

    if not menu_by_day:
        print(
            "ERROR: API responded but returned no lunch entrees in window; "
            "leaving ICS untouched",
            file=sys.stderr,
        )
        return 1

    ics = build_ics(menu_by_day)
    old = ""
    if os.path.exists(ICS_PATH):
        with open(ICS_PATH, "r", encoding="utf-8", newline="") as f:
            old = f.read()
    if ics == old:
        print("No menu changes.")
        return 0
    with open(ICS_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(ics)
    days = sum(1 for cats in menu_by_day.values() if split_categories(cats)[0])
    entree_count = sum(len(split_categories(cats)[0]) for cats in menu_by_day.values())
    print(f"Wrote {ICS_PATH}: {days} days, {entree_count} entrees.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
