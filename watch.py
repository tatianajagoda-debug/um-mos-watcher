"""
Monitor https://um.mos.ru/excursions/ for newly available registration slots
and send notifications to a Telegram bot.

Designed to run in GitHub Actions on a 20-minute cron schedule.

Env vars required:
  TELEGRAM_TOKEN   - Bot token from @BotFather
  TELEGRAM_CHAT_ID - Your chat id (numeric)

Files:
  state.json - committed back to the repo; stores slot ids already announced

Author: assembled with Claude (Anthropic) for a.aleksandrov@goldex.tech
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://um.mos.ru/excursions/"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}

DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2})")
FREE_RE = re.compile(r"Свободных мест\s+(\d+)\s+из\s+(\d+)")
TOTAL_RE = re.compile(r"Найдено\s+(\d+)\s+экскурсий", re.IGNORECASE)
PAGER_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s+из\s+(\d+)")
EXC_HREF_RE = re.compile(r"^/excursions/[^/]+/?$")


def fetch_page(page: int) -> str:
    params = {"isOpen": "true", "page": str(page)}
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Failed to fetch page {page}: {last_err}")


def total_pages(html: str, per_page_hint: int = 12) -> int:
    m = PAGER_RE.search(html)
    if m:
        per_page = max(1, int(m.group(2)) - int(m.group(1)) + 1)
        total = int(m.group(3))
        return max(1, (total + per_page - 1) // per_page)
    m = TOTAL_RE.search(html)
    if m:
        total = int(m.group(1))
        return max(1, (total + per_page_hint - 1) // per_page_hint)
    return 1


def find_excursion_cards(soup: BeautifulSoup) -> Iterable[Tag]:
    """
    Each excursion card on the listing page contains:
      - a link to /excursions/<slug>/ (one or more)
      - a title
      - one or more (date, time) pairs
      - "Свободных мест X из Y" for each slot
    We locate cards by finding ancestor of each main link.
    """
    seen_cards: list[Tag] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not EXC_HREF_RE.match(href):
            continue
        # walk up until we find a container that includes a date pattern
        node: Tag = a
        for _ in range(8):
            parent = node.parent
            if not isinstance(parent, Tag):
                break
            node = parent
            text = node.get_text(" ", strip=True)
            if DATE_RE.search(text):
                if node not in seen_cards:
                    seen_cards.append(node)
                break
    return seen_cards


def parse_card(card: Tag) -> list[dict]:
    """Return list of slot dicts found inside a single card."""
    # Determine canonical link/slug + title
    title = None
    slug = None
    url = None
    for a in card.find_all("a", href=True):
        if not EXC_HREF_RE.match(a["href"]):
            continue
        link_text = a.get_text(" ", strip=True)
        if not link_text:
            continue  # image-only link
        title = link_text
        slug = a["href"].strip("/").split("/")[-1]
        url = f"https://um.mos.ru{a['href']}"
        if not url.endswith("/"):
            url += "/"
        break
    if not title or not slug:
        return []

    text = card.get_text(" ", strip=True)

    # Find all date/time occurrences and their nearby "Свободных мест"
    dates = list(DATE_RE.finditer(text))
    frees = list(FREE_RE.finditer(text))

    slots: list[dict] = []
    used_free_idx = 0
    for dm in dates:
        date = f"{dm.group(1)}, {dm.group(2)}"
        free = total = None
        # find next FREE_RE occurrence after this date
        for j in range(used_free_idx, len(frees)):
            if frees[j].start() > dm.end():
                free = int(frees[j].group(1))
                total = int(frees[j].group(2))
                used_free_idx = j + 1
                break
        slot_id = f"{slug}|{dm.group(1)}|{dm.group(2)}"
        slots.append(
            {
                "id": slot_id,
                "title": title,
                "slug": slug,
                "date": date,
                "free": free,
                "total": total,
                "url": url,
            }
        )
    return slots


def parse_all_slots(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in find_excursion_cards(soup):
        out.extend(parse_card(card))
    return out


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"seen": [], "last_run": None, "last_count": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def send_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID not set; skipping send", file=sys.stderr)
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(api, data=payload, timeout=20)
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()


def format_message(slots: list[dict], total_open: int) -> str:
    lines = [f"<b>🎫 Новые слоты на um.mos.ru — {len(slots)} шт.</b>"]
    lines.append(f"Всего открыта запись: {total_open}")
    lines.append("")
    for s in slots[:25]:  # cap to avoid 4096-char limit
        free_part = ""
        if s["free"] is not None:
            free_part = f"  🪑 {s['free']}/{s['total']}"
        title_html = (
            s["title"]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        lines.append(f"• <a href=\"{s['url']}\">{title_html}</a>")
        lines.append(f"  📅 {s['date']}{free_part}")
    if len(slots) > 25:
        lines.append("")
        lines.append(f"…и ещё {len(slots) - 25} (см. сайт)")
    return "\n".join(lines)


def main() -> int:
    state = load_state()
    seen = set(state.get("seen", []))
    bootstrap = not seen  # very first run: don't send a flood

    page1 = fetch_page(1)
    pages = total_pages(page1)
    # Safety cap so we never DOS the site if pager parsing breaks.
    pages = min(pages, 40)

    all_slots: list[dict] = []
    all_slots.extend(parse_all_slots(page1))

    for p in range(2, pages + 1):
        try:
            html = fetch_page(p)
        except Exception as e:  # noqa: BLE001
            print(f"page {p} fetch failed: {e}", file=sys.stderr)
            # Abort early — better than partial state and false notifications.
            return 2
        all_slots.extend(parse_all_slots(html))
        time.sleep(1.0)  # be nice

    # Deduplicate by slot id (same slot may appear on multiple pages around boundary)
    by_id: dict[str, dict] = {}
    for s in all_slots:
        by_id.setdefault(s["id"], s)
    current_ids = set(by_id)

    # Sanity check: never wipe state to zero. If we suddenly see 0 slots
    # but state has many, something broke — skip update.
    if not current_ids:
        prev = state.get("last_count", 0)
        if prev > 5:
            print(
                f"Suspicious: 0 slots parsed but previous run had {prev}. "
                "Aborting without state update.",
                file=sys.stderr,
            )
            return 3

    new_ids = current_ids - seen

    print(f"Pages: {pages}, total slots: {len(current_ids)}, new: {len(new_ids)}")

    if bootstrap:
        print("First run — recording state without notifications")
    elif new_ids:
        new_slots = [by_id[i] for i in new_ids]
        new_slots.sort(key=lambda s: s["date"])
        send_telegram(format_message(new_slots, len(current_ids)))

    state["seen"] = sorted(current_ids)
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    state["last_count"] = len(current_ids)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
