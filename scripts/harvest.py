#!/usr/bin/env python3
"""
Free Courts Newcastle — availability harvester.

Runs in GitHub Actions. Fetches GetVenueSessions for all 7 Newcastle park
venues for the next 7 days and writes a compact, pre-parsed
data/availability.json that the static site reads.

Fetch ladder (first rung that works wins, per venue):
  1. mobile-api        : api.clubspark.uk with ClubSpark Booker app headers, no auth
  2. mobile-api-auth   : same host with OAuth password-grant token
                         (needs CLUBSPARK_EMAIL / CLUBSPARK_PASSWORD env secrets)
  3. web-impersonate   : clubspark.lta.org.uk via curl_cffi Chrome TLS impersonation
                         (optional dependency; likely blocked by Cloudflare, kept as
                          a free extra chance)

Env:
  CLUBSPARK_EMAIL, CLUBSPARK_PASSWORD  (optional, rung 2)
  DEBUG_RAW=1  -> also write data/raw-sample.json (first successful raw response)
"""

import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:  # pragma: no cover
    LONDON = timezone.utc

MOBILE_BASE = "https://api.clubspark.uk"
WEB_BASE = "https://clubspark.lta.org.uk"
AUTH_TOKEN_URL = "https://auth.clubspark.uk/issue/oauth2/token"
# The ClubSpark Booker app's own client credential (public knowledge — it ships
# inside the app and appears in open-source booking projects).
APP_BASIC = "Basic Y2x1YnNwYXJrLWFwcDpsWlV5UFU3cm4wd1VHYm00WndOenpLSFhWK25wY08rZjc1YUx2UWZ6cTJzPQ=="

MOBILE_HEADERS = {
    "accept": "application/json",
    "appversion": "1.0.3",
    "appname": "cspl",
    "content-language": "en-US",
    "user-agent": "okhttp/3.12.1",
}

VENUES = [
    {"slug": "ArmstrongPark",       "name": "Armstrong Park",        "defaultPrice": 4},
    {"slug": "elswick-park",        "name": "Elswick Park",          "defaultPrice": 0},
    {"slug": "exhibitionpark",      "name": "Exhibition Park",       "defaultPrice": 4},
    {"slug": "GosforthCentralPark", "name": "Gosforth Central Park", "defaultPrice": 4},
    {"slug": "leazes-park",         "name": "Leazes Park",           "defaultPrice": 4},
    {"slug": "NunsMoorPark",        "name": "Nuns Moor Park",        "defaultPrice": 0},
    {"slug": "PaddyFreemansPark",   "name": "Paddy Freeman's Park",  "defaultPrice": 4},
]

DAYS_AHEAD = 7
TIMEOUT = 25


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def http_get(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read()


def http_post(url, headers, body_bytes):
    req = urllib.request.Request(url, headers=headers, data=body_bytes, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read()


def sessions_url(base, slug, start, end):
    return (f"{base}/v0/VenueBooking/{urllib.parse.quote(slug)}/GetVenueSessions"
            f"?resourceID=&startDate={start}&endDate={end}")


def get_mobile_token():
    """OAuth password grant exactly as the Booker app does it. Tries JSON body,
    then form-encoded as a fallback. Returns token string or None."""
    email = os.environ.get("CLUBSPARK_EMAIL", "").strip()
    password = os.environ.get("CLUBSPARK_PASSWORD", "").strip()
    if not email or not password:
        return None
    payload = {
        "username": email,
        "password": password,
        "scope": "https://api.clubspark.uk/token/",
        "grant_type": "password",
    }
    attempts = [
        ({"authorization": APP_BASIC, "content-type": "application/json",
          "user-agent": "okhttp/3.12.1"},
         json.dumps(payload).encode()),
        ({"authorization": APP_BASIC, "content-type": "application/x-www-form-urlencoded",
          "user-agent": "okhttp/3.12.1"},
         urllib.parse.urlencode(payload).encode()),
    ]
    for headers, body in attempts:
        try:
            status, raw = http_post(AUTH_TOKEN_URL, headers, body)
            if status == 200:
                tok = json.loads(raw).get("access_token")
                if tok:
                    print("  auth: obtained mobile token")
                    return tok
        except Exception as e:
            print(f"  auth attempt failed: {e}")
    return None


def fetch_venue(slug, start, end, token_box):
    """Walk the ladder. Returns (json_dict, method) or raises last error."""
    errors = []

    # Rung 1: mobile API, anonymous
    try:
        status, raw = http_get(sessions_url(MOBILE_BASE, slug, start, end), MOBILE_HEADERS)
        if status == 200:
            return json.loads(raw), "mobile-api"
        errors.append(f"mobile anon HTTP {status}")
    except urllib.error.HTTPError as e:
        errors.append(f"mobile anon HTTP {e.code}")
        # 401/403 → try authenticated rung
    except Exception as e:
        errors.append(f"mobile anon {type(e).__name__}: {e}")

    # Rung 2: mobile API with token
    if token_box.get("token") is None and not token_box.get("tried"):
        token_box["tried"] = True
        token_box["token"] = get_mobile_token()
    if token_box.get("token"):
        try:
            h = dict(MOBILE_HEADERS)
            h["authorization"] = f"ClubSpark-Auth {token_box['token']}"
            status, raw = http_get(sessions_url(MOBILE_BASE, slug, start, end), h)
            if status == 200:
                return json.loads(raw), "mobile-api-auth"
            errors.append(f"mobile auth HTTP {status}")
        except urllib.error.HTTPError as e:
            errors.append(f"mobile auth HTTP {e.code}")
        except Exception as e:
            errors.append(f"mobile auth {type(e).__name__}: {e}")

    # Rung 3: web host with Chrome impersonation (optional dep)
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore
        r = cffi_requests.get(
            sessions_url(WEB_BASE, slug, start, end),
            impersonate="chrome",
            timeout=TIMEOUT,
            headers={
                "accept": "application/json, text/plain, */*",
                "referer": f"{WEB_BASE}/{slug}/Booking/BookByDate",
            },
        )
        if r.status_code == 200:
            return r.json(), "web-impersonate"
        errors.append(f"web impersonate HTTP {r.status_code}")
    except ImportError:
        errors.append("web impersonate: curl_cffi not installed")
    except Exception as e:
        errors.append(f"web impersonate {type(e).__name__}: {e}")

    raise RuntimeError("; ".join(errors))


# ── Parsing (mirrors the front-end's verified-schema parser) ────────────────

def natural_key(name):
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(name))]


def classify_session(s):
    if not s:
        return {"state": "closed"}
    try:
        cat = int(s.get("Category"))
    except (TypeError, ValueError):
        cat = -1
    try:
        sub = int(s.get("SubCategory"))
    except (TypeError, ValueError):
        sub = 0  # missing SubCategory on an otherwise-open session → treat as 0
    if cat == 0 and sub == 0:
        cost = s.get("Cost")
        cost = cost if isinstance(cost, (int, float)) and 0 <= cost < 100 else None
        return {"state": "open", "cost": cost}
    n = str(s.get("Name") or "")
    low = n.lower()
    if cat == 2 or any(w in low for w in ("coach", "lesson", "camp", "school", "junior")):
        return {"state": "coaching"}
    if any(w in low for w in ("clos", "maint", "works", "event")):
        return {"state": "closed"}
    return {"state": "booked"}


def parse_day(sessions, default_price, min_h=6, max_h=22):
    for s in sessions:
        st, en = s.get("StartTime"), s.get("EndTime")
        if isinstance(st, (int, float)):
            min_h = min(min_h, int(st // 60))
        if isinstance(en, (int, float)):
            max_h = max(max_h, -(-int(en) // 60))
    min_h, max_h = max(0, min_h), min(24, max_h)
    hours = list(range(min_h, max_h))
    slots = []
    for h in hours:
        start, end = h * 60, h * 60 + 60
        covering = next(
            (s for s in sessions
             if isinstance(s.get("StartTime"), (int, float))
             and isinstance(s.get("EndTime"), (int, float))
             and s["StartTime"] <= start and s["EndTime"] >= end),
            None,
        )
        k = classify_session(covering)
        if k["state"] != "open":
            slots.append({"h": h, "state": k["state"]})
        else:
            cost = k["cost"] if k["cost"] is not None else default_price
            slots.append({"h": h, "state": "open", "cost": cost})
    return hours, slots


def parse_venue(raw, dates, venue):
    """raw GetVenueSessions JSON -> {date: {hours, courts:[{name, slots}]}}"""
    resources = raw.get("Resources") if isinstance(raw, dict) else None
    resources = resources if isinstance(resources, list) else []
    out = {d: {"hours": [], "courts": []} for d in dates}

    courts = []
    for i, r in enumerate(resources):
        name = (r or {}).get("Name") or f"Court {i + 1}"
        days = (r or {}).get("Days")
        days = days if isinstance(days, list) else []
        by_date = {}
        for d in days:
            key = str((d or {}).get("Date") or "")[:10]
            by_date[key] = (d or {}).get("Sessions") or []
        courts.append({"name": name, "by_date": by_date})
    courts.sort(key=lambda c: natural_key(c["name"]))

    for d in dates:
        parsed_courts, all_hours = [], set()
        for c in courts:
            hours, slots = parse_day(c["by_date"].get(d, []), venue["defaultPrice"])
            all_hours.update(hours)
            parsed_courts.append({"name": c["name"], "slots": slots})
        hours = sorted(all_hours) if all_hours else list(range(6, 22))
        # normalise every court onto the shared hour axis
        for pc in parsed_courts:
            have = {s["h"]: s for s in pc["slots"]}
            pc["slots"] = [have.get(h, {"h": h, "state": "closed"}) for h in hours]
        out[d] = {"hours": hours, "courts": parsed_courts}
    return out


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    now_ldn = datetime.now(LONDON)
    dates = [(now_ldn + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS_AHEAD)]
    start, end = dates[0], dates[-1]
    print(f"Harvest {start} → {end}")

    token_box = {}
    result = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": dates,
        "source": None,
        "venues": {},
    }
    raw_sample_written = False
    ok_count = 0

    for v in VENUES:
        try:
            raw, method = fetch_venue(v["slug"], start, end, token_box)
            result["venues"][v["slug"]] = {"ok": True, "dates": parse_venue(raw, dates, v)}
            result["source"] = result["source"] or method
            ok_count += 1
            n_courts = len(raw.get("Resources") or []) if isinstance(raw, dict) else 0
            print(f"  OK  {v['slug']:<22} via {method} ({n_courts} courts)")
            if os.environ.get("DEBUG_RAW") == "1" and not raw_sample_written:
                os.makedirs("data", exist_ok=True)
                with open("data/raw-sample.json", "w") as f:
                    json.dump(raw, f, indent=1)
                raw_sample_written = True
        except Exception as e:
            result["venues"][v["slug"]] = {"ok": False, "error": str(e)[:300]}
            print(f"  ERR {v['slug']:<22} {e}")
        time.sleep(0.4)

    os.makedirs("data", exist_ok=True)
    with open("data/availability.json", "w") as f:
        json.dump(result, f, separators=(",", ":"))
    print(f"\nWrote data/availability.json — {ok_count}/{len(VENUES)} venues ok, "
          f"source={result['source']}")

    if ok_count == 0:
        print("ALL VENUES FAILED — see errors above.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
