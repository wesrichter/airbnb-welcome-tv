#!/usr/bin/env python3
"""Airbnb welcome-screen server.

Serves the welcome page + JSON for each TV, an admin page for guest bookings,
syncs bookings from your Airbnb calendar link, and
optionally watches the SmartThings door lock and opens the welcome app on the TVs.

Python 3.9+ standard library only.  Run:   python3 welcome_server.py
Other commands:
    python3 welcome_server.py launch [tv-id ...]   # turn TVs on + open the app now
    python3 welcome_server.py sync-airbnb          # one Airbnb calendar sync, then exit
    python3 welcome_server.py smartthings-auth     # one-time OAuth setup (see README)
"""
import base64
import datetime as dt
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.normpath(os.path.join(HERE, "..", "web"))
DATA_DIR = os.path.join(HERE, "data")
CONFIG_PATH = os.environ.get("WELCOME_CONFIG", os.path.join(HERE, "config.json"))
RES_PATH = os.path.join(DATA_DIR, "reservations.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

_lock = threading.RLock()


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- storage
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and edit it.")
    return load_json(CONFIG_PATH, {})


CONFIG = {}


def tz():
    return ZoneInfo(CONFIG["property"].get("timezone", "America/New_York"))


def now_local():
    return dt.datetime.now(tz())


def reservations():
    with _lock:
        return load_json(RES_PATH, [])


def save_reservations(items):
    with _lock:
        items.sort(key=lambda r: r.get("checkin", ""))
        save_json(RES_PATH, items)


def state():
    with _lock:
        return load_json(STATE_PATH, {})


def update_state(**kw):
    with _lock:
        s = load_json(STATE_PATH, {})
        s.update(kw)
        save_json(STATE_PATH, s)
        return s


# --------------------------------------------------------------------------- guest logic
def parse_hhmm(s, default):
    try:
        h, m = (s or default).split(":")
        return dt.time(int(h), int(m))
    except ValueError:
        h, m = default.split(":")
        return dt.time(int(h), int(m))


def current_reservation(at=None):
    """The guest who should be welcomed right now.

    A stay is current from its check-in date until checkout time on its checkout
    date.  On a turnover day the departing guest wins until checkout time.
    """
    at = at or now_local()
    today = at.date()
    checkout_t = parse_hhmm(CONFIG["property"].get("checkout_time"), "11:00")
    active = []
    for r in reservations():
        try:
            ci = dt.date.fromisoformat(r["checkin"])
            co = dt.date.fromisoformat(r["checkout"])
        except (KeyError, ValueError):
            continue
        if ci <= today and (today < co or (today == co and at.time() < checkout_t)):
            active.append((co, r))
    if not active:
        return None
    active.sort(key=lambda x: x[0])
    return active[0][1]


def next_reservation(at=None):
    today = (at or now_local()).date()
    upcoming = [r for r in reservations() if r.get("checkin", "") > today.isoformat()]
    return min(upcoming, key=lambda r: r["checkin"]) if upcoming else None


def first_name(full):
    full = (full or "").strip()
    return full.split()[0] if full else ""


def fill(text, values):
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


# --------------------------------------------------------------------------- weather
_weather = {"at": 0, "data": None}


def weather():
    p = CONFIG["property"]
    if p.get("latitude") is None or p.get("longitude") is None:
        return None
    if time.time() - _weather["at"] < 20 * 60 and _weather["data"]:
        return _weather["data"]
    unit = "fahrenheit" if p.get("temperature_unit", "F").upper().startswith("F") else "celsius"
    q = urllib.parse.urlencode({
        "latitude": p["latitude"], "longitude": p["longitude"],
        "current": "temperature_2m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "temperature_unit": unit, "timezone": "auto", "forecast_days": 1,
    })
    try:
        with urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + q, timeout=8) as r:
            d = json.load(r)
        _weather["data"] = {
            "temp": round(d["current"]["temperature_2m"]),
            "code": d["current"]["weather_code"],
            "high": round(d["daily"]["temperature_2m_max"][0]),
            "low": round(d["daily"]["temperature_2m_min"][0]),
            "unit": "°F" if unit == "fahrenheit" else "°C",
        }
        _weather["at"] = time.time()
    except Exception as e:  # weather is a nice-to-have
        log("weather fetch failed:", e)
        _weather["at"] = time.time() - 15 * 60  # retry in ~5 min
    return _weather["data"]


# --------------------------------------------------------------------------- API payload
def welcome_payload(tv_id):
    p = CONFIG["property"]
    tvs = CONFIG.get("tvs", {})
    tv = tvs.get(tv_id) or {}
    r = current_reservation()
    guest = None
    values = {"property": p.get("name", ""), "host": p.get("host_name", ""), "first_name": "", "guest": ""}
    if r:
        fn = r.get("display_name") or first_name(r.get("guest_name"))
        values.update(first_name=fn, guest=fn)
        guest = {
            "first_name": fn,
            "checkin": r["checkin"],
            "checkout": r["checkout"],
            "guest_count": r.get("guest_count"),
        }
    named = bool(guest and guest["first_name"])
    msg = (r or {}).get("tv_messages", {}).get(tv_id) or (tv.get("message", "") if named else "")
    if not msg:  # no guest, or a booking whose name hasn't been added yet
        msg = tv.get("message_no_guest", p.get("message_no_guest", ""))
    return {
        "tv": {"id": tv_id, "label": tv.get("label", tv_id), "message": fill(msg, values),
               "background": tv.get("background", ""), "accent": tv.get("accent", "")},
        "tvs": [{"id": k, "label": v.get("label", k)} for k, v in tvs.items()],
        "guest": guest,
        "property": {
            "name": p.get("name", ""),
            "host_name": p.get("host_name", ""),
            "host_phone": p.get("host_phone", ""),
            "wifi_name": p.get("wifi_name", ""),
            "wifi_password": p.get("wifi_password", ""),
            "wifi_security": p.get("wifi_security", "WPA"),
            "checkout_time": p.get("checkout_time", "11:00"),
            "notes": [fill(n, values) for n in p.get("house_notes", [])],
            "background": p.get("background", ""),
        },
        "weather": weather(),
        "server_time": now_local().isoformat(),
    }


# --------------------------------------------------------------------------- SmartThings
ST_API = "https://api.smartthings.com/v1"
ST_AUTHORIZE = "https://api.smartthings.com/oauth/authorize"
ST_TOKEN = "https://auth-global.api.smartthings.com/oauth/token"
ST_SCOPES = "r:devices:* x:devices:*"


def _st_token_request(form):
    st = CONFIG.get("smartthings", {})
    basic = base64.b64encode(f"{st['client_id']}:{st['client_secret']}".encode()).decode()
    req = urllib.request.Request(
        ST_TOKEN, data=urllib.parse.urlencode(form).encode(), method="POST",
        headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.load(r)
    update_state(st_access_token=tok["access_token"],
                 st_refresh_token=tok.get("refresh_token") or state().get("st_refresh_token"),
                 st_expires_at=time.time() + int(tok.get("expires_in", 3600)) - 120)
    return tok["access_token"]


def st_access_token():
    st = CONFIG.get("smartthings", {})
    if st.get("client_id"):  # OAuth (recommended: does not expire after 24 h)
        s = state()
        if s.get("st_access_token") and time.time() < s.get("st_expires_at", 0):
            return s["st_access_token"]
        if not s.get("st_refresh_token"):
            raise RuntimeError("SmartThings not authorized yet: run  python3 welcome_server.py smartthings-auth")
        return _st_token_request({"grant_type": "refresh_token", "refresh_token": s["st_refresh_token"]})
    if st.get("token"):
        return st["token"]
    raise RuntimeError("No SmartThings credentials in config.json")


def st_call(method, path, body=None):
    req = urllib.request.Request(
        ST_API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + st_access_token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def st_command(device_id, capability, command, args=None):
    cmd = {"component": "main", "capability": capability, "command": command}
    if args:
        cmd["arguments"] = args
    return st_call("POST", f"/devices/{device_id}/commands", {"commands": [cmd]})


def launch_on_tv(tv_id):
    """Turn one TV on and open the welcome app on it. Returns a status string."""
    tv = CONFIG.get("tvs", {}).get(tv_id, {})
    dev = tv.get("smartthings_device_id")
    app_id = CONFIG.get("smartthings", {}).get("app_id", "WlcmScrn01.Welcome")
    if not dev:
        return f"{tv_id}: no smartthings_device_id in config"
    try:
        st_command(dev, "switch", "on")
    except Exception as e:
        log(f"{tv_id}: power-on failed ({e}); trying app launch anyway")
    last = None
    for attempt in range(6):  # the TV needs a few seconds to boot
        time.sleep(4 if attempt == 0 else 5)
        try:
            st_command(dev, "custom.launchapp", "launchApp", [app_id])
            log(f"{tv_id}: welcome app launched")
            return f"{tv_id}: launched"
        except Exception as e:
            last = e
    log(f"{tv_id}: launch failed: {last}")
    return f"{tv_id}: launch failed: {last}"


def launch_all(tv_ids=None):
    ids = tv_ids or list(CONFIG.get("tvs", {}))
    results, threads = [], []
    for t in ids:
        th = threading.Thread(target=lambda t=t: results.append(launch_on_tv(t)))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    return results


def lock_watcher():
    st = CONFIG.get("smartthings", {})
    lock_id = st.get("lock_device_id")
    if not lock_id:
        return
    mode = st.get("launch_on_unlock", "first_per_stay")  # "first_per_stay" | "every_time" | "off"
    if mode == "off":
        return
    log(f"watching lock {lock_id} (mode: {mode})")
    prev = None
    while True:
        try:
            s = st_call("GET", f"/devices/{lock_id}/components/main/capabilities/lock/status")
            val = s.get("lock", {}).get("value")
            if prev == "locked" and val == "unlocked":
                r = current_reservation()
                launched = state().get("launched_for", [])
                if not r:
                    log("door unlocked, but no current guest: not launching")
                elif mode == "first_per_stay" and r["id"] in launched:
                    log("door unlocked, welcome already shown this stay")
                else:
                    log(f"door unlocked for {r.get('guest_name')}: launching welcome screens")
                    update_state(launched_for=(launched + [r["id"]])[-50:])
                    threading.Thread(target=launch_all, daemon=True).start()
            prev = val or prev
        except Exception as e:
            log("lock poll failed:", e)
        time.sleep(max(5, int(st.get("poll_seconds", 15))))


def smartthings_auth_cli():
    st = CONFIG.get("smartthings", {})
    if not (st.get("client_id") and st.get("client_secret")):
        sys.exit("Put smartthings.client_id and client_secret in config.json first (see README).")
    redirect = st.get("redirect_uri", "https://httpbin.org/get")
    url = ST_AUTHORIZE + "?" + urllib.parse.urlencode({
        "client_id": st["client_id"], "response_type": "code",
        "redirect_uri": redirect, "scope": ST_SCOPES})
    print("\n1. Open this link and sign in to SmartThings:\n\n   " + url)
    print("\n2. After approving, your browser goes to " + redirect)
    print("   Copy the 'code' value from that page's address (or from the page itself).\n")
    code = input("Paste the code here: ").strip()
    _st_token_request({"grant_type": "authorization_code", "code": code,
                       "redirect_uri": redirect, "client_id": st["client_id"]})
    print("Saved. Testing...")
    devices = st_call("GET", "/devices").get("items", [])
    print("\nYour SmartThings devices (copy the IDs into config.json):")
    for d in devices:
        print(f"  {d['deviceId']}  {d.get('label') or d.get('name')}")


# --------------------------------------------------------------------------- Airbnb calendar sync
# Airbnb's calendar export (iCal) has each booking's dates and confirmation code,
# but not the guest's name. New bookings appear on the admin page as
# "Reserved - add name", with a link to the booking on Airbnb.
CODE_RE = re.compile(r"\b(HM[A-Z0-9]{8})\b")


def _ical_events(text):
    """Minimal iCal parser: returns a list of {PROPERTY: value} dicts, one per VEVENT."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]  # folded continuation line
        else:
            lines.append(raw)
    events, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, _, val = line.partition(":")
            cur[key.split(";")[0].upper()] = val.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")
    return events


def _ical_date(v):
    v = (v or "").strip()[:8]
    return dt.date(int(v[:4]), int(v[4:6]), int(v[6:8]))


def parse_airbnb_ical(text):
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("That link didn't return an Airbnb calendar")
    stays = []
    for ev in _ical_events(text):
        desc = ev.get("DESCRIPTION", "")
        if ev.get("SUMMARY", "").strip().lower() != "reserved" and "reservation" not in desc.lower():
            continue  # blocked dates ("Airbnb (Not available)") etc.
        try:
            ci, co = _ical_date(ev.get("DTSTART")), _ical_date(ev.get("DTEND"))
        except (ValueError, TypeError):
            continue
        code = CODE_RE.search(desc)
        url = re.search(r"https://\S*airbnb\.\S+", desc)
        phone = re.search(r"Last 4 Digits\)?:\s*(\d{4})", desc)
        stays.append({
            "id": code.group(1) if code else "ical-" + (ev.get("UID") or f"{ci}-{co}")[:40],
            "checkin": ci.isoformat(),
            "checkout": co.isoformat(),
            "airbnb_url": url.group(0) if url else "",
            "phone_last4": phone.group(1) if phone else "",
        })
    return stays


def airbnb_sync_once():
    url = CONFIG.get("airbnb_calendar", {}).get("url")
    if not url:
        raise RuntimeError("airbnb_calendar.url is not set in config.json")
    req = urllib.request.Request(url, headers={"User-Agent": "WelcomeTV/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        stays = parse_airbnb_ical(r.read().decode("utf-8", "replace"))
    today = now_local().date().isoformat()
    added = updated = removed = 0
    with _lock:
        items = reservations()
        by_id = {r["id"]: r for r in items}
        seen = set()
        for s in stays:
            seen.add(s["id"])
            old = by_id.get(s["id"])
            if old is None:
                by_id[s["id"]] = dict(s, guest_name="", source="airbnb")
                added += 1
            else:
                changed = {k: v for k, v in s.items() if v and old.get(k) != v}
                if changed:
                    old.update(changed)
                    updated += 1
        # A synced stay that is still in the future but no longer in the feed was cancelled.
        # (Past stays drop off the feed naturally, so they're kept.)
        # Skip this if the feed came back empty, so a glitch on Airbnb's side can't wipe the list.
        for rid, r in list(by_id.items()) if stays else []:
            if r.get("source") == "airbnb" and rid not in seen and r.get("checkin", "") > today:
                del by_id[rid]
                removed += 1
        save_reservations(list(by_id.values()))
    update_state(airbnb_last_sync=now_local().isoformat(timespec="minutes"))
    log(f"airbnb sync: {added} new, {updated} changed, {removed} cancelled")
    return {"added": added, "updated": updated, "removed": removed}


def airbnb_sync_loop():
    cal = CONFIG.get("airbnb_calendar", {})
    if not cal.get("url"):
        return
    while True:
        try:
            airbnb_sync_once()
        except Exception as e:
            log("airbnb sync failed:", e)
        time.sleep(max(10, int(cal.get("poll_minutes", 30))) * 60)


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "WelcomeTV/1.0"

    def log_message(self, fmt, *args):
        if "/api/welcome" not in str(args[0] if args else ""):
            log(self.address_string(), fmt % args)

    # -- helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")  # the TV app runs from file://
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def authorized(self):
        pw = CONFIG.get("admin_password", "")
        h = self.headers.get("Authorization", "")
        if pw and h.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(h[6:]).decode().partition(":")
                if given == pw:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Welcome TV admin"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def serve_file(self, rel):
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR + os.sep) or not os.path.isfile(path):
            return self.send_error(404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/welcome":
            return self.send_json(welcome_payload((q.get("tv") or [""])[0]))
        if u.path in ("/", "/index.html"):
            return self.serve_file("index.html")
        if u.path.startswith("/admin") or u.path.startswith("/api/admin"):
            if not self.authorized():
                return
            if u.path in ("/admin", "/admin/"):
                return self.serve_file("admin.html")
            if u.path == "/api/admin/reservations":
                return self.send_json({
                    "reservations": reservations(),
                    "current": (current_reservation() or {}).get("id"),
                    "tvs": [{"id": k, "label": v.get("label", k)} for k, v in CONFIG.get("tvs", {}).items()],
                    "airbnb_sync_enabled": bool(CONFIG.get("airbnb_calendar", {}).get("url")),
                    "airbnb_last_sync": state().get("airbnb_last_sync"),
                    "smartthings_enabled": bool(CONFIG.get("smartthings", {}).get("token")
                                                or CONFIG.get("smartthings", {}).get("client_id")),
                })
            return self.send_error(404)
        return self.serve_file(u.path.lstrip("/"))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/api/admin") or not self.authorized():
            return None if u.path.startswith("/api/admin") else self.send_error(404)
        try:
            body = self.read_json()
            if u.path == "/api/admin/reservations":
                r = {k: body.get(k) for k in ("guest_name", "display_name", "checkin", "checkout", "guest_count", "tv_messages")}
                dt.date.fromisoformat(r["checkin"])
                dt.date.fromisoformat(r["checkout"])
                if r["checkout"] <= r["checkin"]:
                    return self.send_json({"error": "Checkout must be after check-in"}, 400)
                if not (r["guest_name"] or "").strip():
                    return self.send_json({"error": "Guest name is required"}, 400)
                with _lock:
                    items = reservations()
                    rid = body.get("id")
                    existing = next((x for x in items if x["id"] == rid), None) if rid else None
                    if existing:
                        existing.update(r, edited=True, needs_review=False)
                    else:
                        items.append(dict(r, id=uuid.uuid4().hex[:10], source="manual"))
                    save_reservations(items)
                return self.send_json({"ok": True})
            if u.path == "/api/admin/delete":
                with _lock:
                    save_reservations([x for x in reservations() if x["id"] != body.get("id")])
                return self.send_json({"ok": True})
            if u.path == "/api/admin/launch":
                return self.send_json({"results": launch_all(body.get("tvs") or None)})
            if u.path == "/api/admin/sync-airbnb":
                return self.send_json(airbnb_sync_once())
        except Exception as e:
            traceback.print_exc()
            return self.send_json({"error": str(e)}, 500)
        return self.send_error(404)


def main():
    global CONFIG
    CONFIG = load_config()
    os.makedirs(DATA_DIR, exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "launch":
        print("\n".join(launch_all(sys.argv[2:] or None)))
        return
    if cmd == "sync-airbnb":
        print(airbnb_sync_once())
        return
    if cmd == "smartthings-auth":
        smartthings_auth_cli()
        return
    for target in (airbnb_sync_loop, lock_watcher):
        threading.Thread(target=target, daemon=True).start()
    port = int(CONFIG.get("port", 8080))
    log(f"Welcome server on http://0.0.0.0:{port}   admin: http://<this-computer-ip>:{port}/admin")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
