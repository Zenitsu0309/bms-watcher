"""
BookMyShow watcher for Avengers: Doomsday (English): alerts your phone (ntfy) when tickets open for a movie
at your chosen Chennai theatres.

Usage:
  pip install requests
  python avengers_watcher.py test ET00518674   # pipeline test with a movie that's already on sale
  python avengers_watcher.py test              # looks for MOVIE_TITLE in the catalogue
  python avengers_watcher.py                   # real watcher (keeps running)
  python avengers_watcher.py once              # one check, then exit (used by GitHub Actions)
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------- settings you can change ----------
MOVIE_TITLE = "avengers: doomsday"          # matched case-insensitively, whole title
MOVIE_LANGUAGE = "english"
EVENT_CODE = "ET00439706"      # Avengers: Doomsday (English)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "NTFY_PLAYSTATIONVOUCHER")  # cloud: set as a secret
POLL_SECONDS = 60
MAX_DATES = 4                  # how many upcoming dates to check once booking opens

TARGET_VENUES = {
    "RSSC": "Rohini Silver Screens: Koyambedu",
    "PVSR": "PVR: Sathyam, Royapettah",
    "ACVM": "AGS Cinemas: Villivakkam",
    "PCAN": "PVR: VR Chennai, Anna Nagar",
}
# ---------------------------------------------

REGION = "CHEN"
CATALOG_URL = "https://in.bookmyshow.com/api/explore/v1/discover/movies-chennai"
SHOWTIMES_URL = ("https://in.bookmyshow.com/api/movies-data/v5/"
                 "showtimes-by-event/primary-dynamic")
STATE_FILE = Path("avengers_state.json")
IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://in.bookmyshow.com/explore/movies-chennai",
    "x-app-code": "WEB",
    "x-region-code": REGION,
    "x-region-slug": "chennai",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get_json(url, params):
    r = SESSION.get(url, params=params, timeout=20)
    if r.status_code in (403, 429) and not SESSION.cookies:
        SESSION.get("https://in.bookmyshow.com/explore/movies-chennai", timeout=20)
        r = SESSION.get(url, params=params, timeout=20)
    if not r.ok:
        print("BookMyShow said:", r.status_code, r.text[:200])
        r.raise_for_status()
    return r.json()


def walk(node):
    """Yield every dict anywhere inside a JSON structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def notify(title, message):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "urgent", "Tags": "movie_camera",
                 "Click": "https://in.bookmyshow.com/explore/movies-chennai"},
        timeout=20,
    )


# ---------- step 1: find the movie's event code in the catalogue ----------
def find_event_code():
    data = get_json(CATALOG_URL, {"region": REGION, "cat": "MT", "embedded": "true",
                                  "lat": 13.056, "lon": 80.206})
    cards = 0
    for d in walk(data):
        an = d.get("analytics")
        if not (isinstance(an, dict) and an.get("event_code") and "ctaUrl" in d):
            continue
        cards += 1
        text = " ".join(
            str(c.get("text", ""))
            for t in (d.get("text") if isinstance(d.get("text"), list) else [])
            for c in t.get("components", [])
        ).lower()
        title = (an.get("title") or "").strip().lower()
        if title == MOVIE_TITLE and MOVIE_LANGUAGE in text:
            return an["event_code"], cards
    return None, cards


# ---------- step 2: look for your theatres ----------
def fetch_showtimes(event_code, date_code):
    return get_json(SHOWTIMES_URL, {
        "etCodes": event_code, "dateCode": date_code, "isDesktop": "true",
        "regionCode": REGION, "xLocationShared": "false", "memberId": "", "lsId": "",
        "subCode": "", "appCode": "WEB", "language": MOVIE_LANGUAGE,
        "refEventCode": event_code,
    })


def available_dates(data):
    dates = []
    for d in walk(data):
        cta = d.get("cta")
        if isinstance(cta, dict) and cta.get("type") == "dateSelector":
            code = (cta.get("additionalData") or {}).get("dateCode")
            if code and code not in dates:
                dates.append(code)
    return dates


def target_venues(data):
    """{venueCode: [showtime strings]} for your theatres that have shows."""
    found = {}
    for d in walk(data):
        if d.get("type") != "venue-card":
            continue
        code = (d.get("additionalData") or {}).get("venueCode") or d.get("id")
        if code not in TARGET_VENUES:
            continue
        times = []
        for sec in d.get("showtimesSections", []):
            for st in sec.get("showtimes", []):
                label = st.get("title")
                if label:
                    extra = st.get("screenAttr")
                    times.append(f"{label} ({extra})" if extra else label)
        if times:
            found[code] = times
    return found


def check_event(event_code, alerted):
    """Alert for newly seen (theatre, date) pairs. Returns the current set of pairs."""
    today = datetime.now(IST).strftime("%Y%m%d")
    first = get_json(SHOWTIMES_URL, {
        "etCodes": event_code, "dateCode": today, "isDesktop": "true",
        "regionCode": REGION, "xLocationShared": "false", "memberId": "", "lsId": "",
        "subCode": "", "appCode": "WEB", "language": MOVIE_LANGUAGE,
        "refEventCode": event_code,
    })
    first_date = ((first.get("data") or {}).get("additionalData") or {}).get("dateCode", today)
    dates = available_dates(first) or [first_date]

    current = set()
    for date_code in dates[:MAX_DATES]:
        data = first if date_code == first_date else fetch_showtimes(event_code, date_code)
        found = target_venues(data)
        lines = []
        for code, times in found.items():
            key = f"{code}:{date_code}"
            current.add(key)
            if key not in alerted:
                lines.append(f"{TARGET_VENUES[code]}: {', '.join(times[:6])}")
        if lines:
            pretty = datetime.strptime(date_code, "%Y%m%d").strftime("%a %d %b")
            notify(f"{MOVIE_TITLE.title()} tickets OPEN - {pretty}", "\n".join(lines))
            print("ALERT:", pretty, lines)
    return current


# ---------- state ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"event_code": "", "alerted": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def run_test():
    code = sys.argv[2] if len(sys.argv) > 2 else EVENT_CODE
    if not code:
        code, cards = find_event_code()
        print(f"Catalogue: read {cards} movie cards.")
        if not code:
            print(f'"{MOVIE_TITLE}" ({MOVIE_LANGUAGE}) is not in the catalogue yet.')
            notify("BMS watcher test", f"Catalogue reachable ({cards} movies). "
                                       f"'{MOVIE_TITLE}' not listed yet.")
            return
    print("Using event code:", code)
    current = check_event(code, set())
    print("Theatre/date pairs found:", sorted(current) or "none")
    if not current:
        notify("BMS watcher test", f"Event {code}: none of your 4 theatres have shows yet.")


def poll_once(state):
    """One full check. Returns the updated state. Never raises."""
    event_code = EVENT_CODE or state.get("event_code", "")
    alerted = set(state.get("alerted", []))
    try:
        if not event_code:
            event_code, cards = find_event_code()
            if event_code:
                print("Found", MOVIE_TITLE, "->", event_code)
                notify(f"{MOVIE_TITLE.title()} is now listed on BookMyShow",
                       f"Event {event_code}. Watching your 4 theatres...")
            else:
                print(time.strftime("%H:%M:%S"), f"not listed yet ({cards} movies seen)")
        if event_code:
            alerted = check_event(event_code, alerted)
    except Exception as e:
        print("error:", e)      # keep old state so we never send false repeats
        return state
    return {"event_code": event_code, "alerted": sorted(alerted)}


def main():
    state = load_state()
    while True:
        state = poll_once(state)
        save_state(state)
        time.sleep(POLL_SECONDS)


def run_once():
    state = poll_once(load_state())
    save_state(state)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        run_test()
    elif len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once()
    else:
        main()
