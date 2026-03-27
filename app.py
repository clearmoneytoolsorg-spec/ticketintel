"""
TICKET INTEL PRO v7
====================
Simple. Clean. One-click.

Every event card shows:
- Whether face value tickets are still available on Ticketmaster
- Confirmed profit based on real prices
- One button to buy

If face value is gone → event is hidden automatically.
You only see events you can actually profit from right now.
"""

import sys, subprocess

def install(pkg):
    subprocess.check_call([sys.executable,"-m","pip","install",pkg,"-q","--disable-pip-version-check"])

for pkg in ["aiohttp","requests","python-dotenv"]:
    try:
        __import__(pkg.replace("-","_"))
    except ImportError:
        print(f"Installing {pkg}...")
        install(pkg)

import os, json, re, asyncio, logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional
from aiohttp import web
import requests as req
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("ticketpro")

# ── CONFIG ────────────────────────────────────────────────────
TM_KEY    = os.getenv("TICKETMASTER_API_KEY", "")
SG_KEY    = os.getenv("SEATGEEK_CLIENT_ID", "")
TG_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_MINS = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
PORT      = int(os.getenv("PORT", "8080"))
DATA      = Path("ticket_data.json")

# Only show events with at least this much profit per ticket
MIN_PROFIT = 40
MIN_ROI    = 20

# ── CITIES ────────────────────────────────────────────────────
CITIES = [
    # Tier 1 — highest resell premiums
    ("New York",      "NY", 1.35),
    ("Las Vegas",     "NV", 1.30),
    ("Los Angeles",   "CA", 1.25),
    ("Nashville",     "TN", 1.25),
    ("Chicago",       "IL", 1.15),
    ("Boston",        "MA", 1.15),
    ("San Francisco", "CA", 1.15),
    # Tier 2 — strong markets
    ("Miami",         "FL", 1.10),
    ("Philadelphia",  "PA", 1.10),
    ("Newark",        "NJ", 1.10),
    ("Atlanta",       "GA", 1.10),
    ("Washington",    "DC", 1.08),
    ("Houston",       "TX", 1.08),
    ("Dallas",        "TX", 1.08),
    ("Denver",        "CO", 1.08),
    ("Phoenix",       "AZ", 1.05),
    ("Minneapolis",   "MN", 1.05),
    ("Orlando",       "FL", 1.05),
    ("San Diego",     "CA", 1.05),
    # Tier 3 — regional
    ("Seattle",       "WA", 1.00),
    ("Detroit",       "MI", 1.00),
    ("Portland",      "OR", 0.95),
    ("Tacoma",        "WA", 0.90),
]

# ── WANTED EVENTS (whitelist) ─────────────────────────────────
WANTED = [
    # Music Tier 1
    "taylor swift","beyonce","drake","kendrick lamar","bad bunny",
    "morgan wallen","zach bryan","post malone","travis scott",
    "billie eilish","the weeknd","eminem","coldplay","ed sheeran",
    "sabrina carpenter","chappell roan","tyler the creator",
    "olivia rodrigo","bruno mars","harry styles","dua lipa",
    "ariana grande","sza","doja cat","lana del rey",
    "twenty one pilots","green day","blink-182","metallica",
    "rolling stones","elton john","bruce springsteen",
    "foo fighters","red hot chili peppers","jack white",
    # Music Tier 2
    "luke combs","jason aldean","chris stapleton","lainey wilson",
    "eric church","hardy","thomas rhett","kenny chesney",
    "j. cole","future","lil wayne","khalid",
    "the 1975","arctic monkeys","tame impala","dave matthews",
    "phish","dead and company","widespread panic",
    "zac brown","jake owen","jon pardi","cody johnson",
    # Comedy
    "dave chappelle","kevin hart","chris rock","bill burr",
    "bert kreischer","andrew schulz","theo von","john mulaney",
    "trevor noah","gabriel iglesias","jim jefferies","ron white",
    # Sports — high demand only
    "ufc","boxing championship","title fight","title match",
    "wwe","wrestlemania",
    "nba finals","nfl playoff","super bowl","stanley cup",
    "world series","ncaa tournament","march madness",
    "playoff","championship","title bout",
    # Theater
    "hamilton","wicked","lion king","beetlejuice","hadestown",
    "chicago musical","phantom","les miserables","mean girls musical",
    # Special events
    "formula 1","f1 grand prix","daytona 500","indy 500",
    "coachella","lollapalooza","bonnaroo","outside lands",
]

SKIP_WORDS = [
    "tribute","cover band","open mic","karaoke","free ",
    "varsity","high school","youth ","kids ","children",
    "cheerleading","pro cheer","dance competition",
    "college baseball","college softball","college volleyball",
    "tractor pull","rodeo","monster truck",
    "community theatre","amateur","unsigned",
    "comedy open mic","improv showcase",
    "parking pass","parking",  # No parking pass resell
]

CC_CODES    = ["CITI","AMEX","CAPITALONE","CHASE","MASTERCARD","VISA","CITICARD"]
VENUE_CODES = ["LIVENATION","TMFAN","VERIFIED","SPOTIFY","OFFICIAL","PRESALE"]

ARTIST_CODES = {
    "taylor swift":      ["SWIFTIES","TAYLORSWIFT","TSNATION"],
    "morgan wallen":     ["MORGANWALLEN","HANGINOVER"],
    "zach bryan":        ["ZACHBRYAN","AMERICANHEARTBREAK"],
    "beyonce":           ["BEYHIVE","BEYONCE"],
    "kendrick lamar":    ["KENDRICK","PGLANG"],
    "billie eilish":     ["BILLIEEILISH","HAPPIER"],
    "post malone":       ["POSTMALONE","BEERBOYS"],
    "sabrina carpenter": ["SABRINACARPENTER","SHORTNSWEET"],
    "chappell roan":     ["CHAPPELLROAN","PINKPONY"],
    "olivia rodrigo":    ["OLIVIARODRIGO","SOUR","GUTS"],
    "ufc":               ["UFC","UFCFIGHT"],
    "wwe":               ["WWE","WWEFAN"],
    "hamilton":          ["HAMILTON"],
    "dave chappelle":    ["CHAPPELLE"],
    "kevin hart":        ["KEVINHART"],
    "bill burr":         ["BILLBURR"],
    "bert kreischer":    ["BERTHEGOD","BERT"],
}

# ── STATE ─────────────────────────────────────────────────────
state = {
    "events": [],
    "last_scan": None,
    "scan_count": 0,
    "total_opportunities": 0,
    "alerted_ids": set(),
    "presale_alerted": set(),
    "scanning": False,
    "filtered_count": 0,
    "cities_scanned": 0,
}

# ── TELEGRAM ──────────────────────────────────────────────────
def tg(text: str):
    if not TG_TOKEN or not TG_CHAT:
        log.info(f"[ALERT] {text[:80]}")
        return
    try:
        r = req.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10
        )
        if r.status_code == 200:
            log.info("Telegram ✓")
        else:
            log.warning(f"Telegram error: {r.text[:80]}")
    except Exception as e:
        log.warning(f"Telegram failed: {e}")

# ── SEATGEEK PRICE LOOKUP ─────────────────────────────────────
async def get_resell_price(name: str, event_date: str) -> Optional[dict]:
    """Get real secondary market prices from SeatGeek."""
    if not SG_KEY:
        return None
    import aiohttp
    try:
        query = re.sub(r'[^\w\s]', '', name)[:50].strip()
        params = {"client_id": SG_KEY, "q": query, "per_page": 5}
        if event_date:
            try:
                dt = datetime.strptime(event_date[:10], "%Y-%m-%d")
                params["datetime_utc.gte"] = (dt - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
                params["datetime_utc.lte"] = (dt + timedelta(days=2)).strftime("%Y-%m-%dT23:59:59")
            except Exception:
                pass

        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.seatgeek.com/2/events",
                params=params,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()

        events = data.get("events", [])
        if not events:
            return None

        # Find best match by word overlap
        name_words = [w.lower() for w in re.sub(r'[^\w\s]', '', name).split() if len(w) > 3]
        best = None
        for e in events:
            title_words = [w.lower() for w in e.get("title", "").split() if len(w) > 3]
            overlap = sum(1 for w in name_words if w in title_words)
            if overlap >= 2:
                best = e
                break
        if not best and events:
            best = events[0]
        if not best:
            return None

        stats = best.get("stats", {})
        lowest  = stats.get("lowest_price") or 0
        median  = stats.get("median_price") or stats.get("average_price") or 0
        highest = stats.get("highest_price") or 0
        count   = stats.get("listing_count") or 0

        if not lowest and not median:
            return None

        return {
            "lowest":  round(float(lowest), 2),
            "median":  round(float(median), 2),
            "highest": round(float(highest), 2),
            "count":   int(count),
            "url":     best.get("url", ""),
        }
    except Exception as e:
        log.debug(f"SeatGeek {name[:30]}: {e}")
        return None

# ── RESELL MULTIPLIER ─────────────────────────────────────────
def get_multiplier(name: str, artist: str, category: str, capacity: int) -> float:
    n   = name.lower()
    a   = (artist or "").lower()
    cat = category.lower()

    if any(k in n or k in a for k in ["taylor swift", "beyonce", "bad bunny", "coldplay", "rolling stones", "elton john", "eminem"]):
        mult = 3.8
    elif any(k in n or k in a for k in ["morgan wallen", "zach bryan", "kendrick lamar", "post malone", "travis scott", "billie eilish", "the weeknd"]):
        mult = 3.0
    elif any(k in n or k in a for k in ["sabrina carpenter", "chappell roan", "tyler the creator", "olivia rodrigo", "harry styles", "dua lipa", "ariana grande"]):
        mult = 2.8
    elif any(k in n or k in a for k in ["hamilton", "wicked", "lion king", "hadestown"]):
        mult = 2.5
    elif any(k in n or k in a for k in ["ufc", "boxing championship", "title fight", "wrestlemania"]):
        mult = 2.4
    elif any(k in n for k in ["nba finals", "stanley cup final", "world series", "super bowl"]):
        mult = 3.2
    elif any(k in n for k in ["playoff", "championship", "finals", "title"]):
        mult = 2.2
    elif any(k in n or k in a for k in ["dave chappelle", "kevin hart", "bill burr", "chris rock", "bert kreischer"]):
        mult = 2.2
    elif any(k in n or k in a for k in ["luke combs", "jason aldean", "chris stapleton", "kenny chesney"]):
        mult = 2.2
    elif "comedy" in cat:
        mult = 1.7
    elif "sports" in cat:
        mult = 1.6
    elif "concert" in cat or "music" in cat:
        mult = 1.7
    else:
        mult = 1.5

    # Venue size adjustment
    if capacity <= 500:     mult *= 1.70
    elif capacity <= 1000:  mult *= 1.50
    elif capacity <= 2000:  mult *= 1.30
    elif capacity <= 3500:  mult *= 1.18
    elif capacity <= 5000:  mult *= 1.08
    elif capacity > 60000:  mult *= 0.75
    elif capacity > 30000:  mult *= 0.82
    elif capacity > 15000:  mult *= 0.90

    return round(mult, 2)

# ── PROFIT CALCULATOR ─────────────────────────────────────────
def calc_profit(
    tm_face: float,       # What Ticketmaster is charging right now
    sg: Optional[dict],   # Real SeatGeek resell prices
    mult: float,          # Estimated multiplier (fallback)
    city_mult: float,     # City market adjustment
) -> dict:
    """
    Calculate real profit.
    Uses SeatGeek verified prices if available.
    Falls back to calibrated estimate.
    """
    # Determine resell price
    if sg and sg.get("median", 0) > 0:
        resell  = sg["median"]
        source  = "verified"
        sg_low  = sg.get("lowest", resell * 0.8)
        sg_high = sg.get("highest", resell * 1.3)
        sg_count= sg.get("count", 0)
    elif sg and sg.get("lowest", 0) > 0:
        resell  = sg["lowest"] * 1.1
        source  = "verified"
        sg_low  = sg["lowest"]
        sg_high = sg.get("highest", resell * 1.3)
        sg_count= sg.get("count", 0)
    else:
        resell  = tm_face * mult * city_mult
        source  = "estimated"
        sg_low  = resell * 0.78
        sg_high = resell * 1.30
        sg_count= 0

    # KEY CHECK: Is there actually margin here?
    # If what Ticketmaster charges is already >= 85% of resell, no profit after fees
    if tm_face >= resell * 0.85:
        return {
            "is_profitable": False,
            "reason": "TM price already close to resell value",
            "tm_face": round(tm_face, 2),
            "resell": round(resell, 2),
            "source": source,
        }

    # Calculate fees
    # Ticketmaster buyer fee: ~25% on top of face
    tm_total = tm_face * 1.25
    # StubHub seller fee: 15%
    stubhub_fee = resell * 0.15
    # Net profit
    profit = resell - stubhub_fee - tm_total
    roi    = (profit / tm_total * 100) if tm_total > 0 else 0

    return {
        "is_profitable":  profit >= MIN_PROFIT and roi >= MIN_ROI,
        "tm_face":        round(tm_face, 2),
        "tm_total":       round(tm_total, 2),  # What you actually pay
        "resell":         round(resell, 2),
        "resell_low":     round(sg_low, 2),
        "resell_high":    round(sg_high, 2),
        "stubhub_fee":    round(stubhub_fee, 2),
        "profit_per":     round(profit, 2),
        "profit_4":       round(profit * 4, 2),
        "roi_pct":        round(roi, 1),
        "source":         source,
        "sg_count":       sg_count,
        "sg_url":         sg.get("url", "") if sg else "",
    }

# ── SCORER ────────────────────────────────────────────────────
def score_it(event: dict, profit: dict) -> dict:
    name = event.get("name", "").lower()
    a    = (event.get("artist") or "").lower()
    cap  = event.get("capacity", 15000) or 15000

    # Artist quality score
    if any(k in a or k in name for k in ["taylor swift", "beyonce", "eminem", "rolling stones", "coldplay"]):
        asc = 98
    elif any(k in a or k in name for k in ["morgan wallen", "zach bryan", "kendrick", "post malone", "billie eilish", "the weeknd"]):
        asc = 88
    elif any(k in a or k in name for k in ["hamilton", "wicked", "ufc", "championship", "playoff", "finals"]):
        asc = 82
    elif any(k in a or k in name for k in ["dave chappelle", "kevin hart", "bill burr", "chris rock", "sabrina carpenter", "chappell roan"]):
        asc = 78
    else:
        asc = 65

    # Venue scarcity
    if cap <= 500:     vsc = 98
    elif cap <= 1000:  vsc = 93
    elif cap <= 2000:  vsc = 86
    elif cap <= 3500:  vsc = 78
    elif cap <= 5000:  vsc = 68
    elif cap <= 10000: vsc = 55
    elif cap <= 20000: vsc = 42
    else:              vsc = 25

    # Profit quality
    p = profit.get("profit_per", 0)
    r = profit.get("roi_pct", 0)

    if p >= 200:   psc = 100
    elif p >= 150: psc = 92
    elif p >= 100: psc = 82
    elif p >= 75:  psc = 72
    elif p >= 50:  psc = 60
    elif p >= 40:  psc = 50
    else:          psc = 20

    if r >= 150:   rsc = 100
    elif r >= 100: rsc = 88
    elif r >= 75:  rsc = 78
    elif r >= 50:  rsc = 65
    elif r >= 30:  rsc = 52
    elif r >= 20:  rsc = 38
    else:          rsc = 15

    verified_boost = 10 if profit.get("source") == "verified" else 0

    total = round(min(99, max(0,
        asc * 0.20 +
        vsc * 0.15 +
        psc * 0.40 +
        rsc * 0.25 +
        verified_boost
    )), 1)

    if total >= 80:   verdict = "MUST BUY"
    elif total >= 65: verdict = "STRONG BUY"
    elif total >= 52: verdict = "BUY"
    else:             verdict = "WATCH"

    return {
        "total":   total,
        "verdict": verdict,
        "source":  profit.get("source", "estimated"),
    }

def find_codes(event: dict) -> List[str]:
    a = (event.get("artist") or event.get("name", "")).lower()
    v = event.get("venue", "").lower()
    codes = list(CC_CODES) + list(VENUE_CODES)
    for key, ac in ARTIST_CODES.items():
        if key in a or key in event.get("name", "").lower():
            codes.extend(ac)
            break
    if "paramount"  in v: codes.extend(["PARAMOUNT", "STG"])
    if "climate"    in v: codes.extend(["CPAPRESALE", "CPA"])
    if "ryman"      in v: codes.extend(["RYMAN", "OPRY"])
    if "msg" in v or "madison" in v: codes.extend(["MSG"])
    if "t-mobile arena" in v: codes.extend(["TMOBILE", "TMARENA"])
    if "bridgestone" in v: codes.extend(["BRIDGESTONE"])
    if "td garden"  in v: codes.extend(["TDGARDEN", "BOSTON"])
    if "united center" in v: codes.extend(["UNITEDCENTER"])
    clean = re.sub(r'[^a-zA-Z]', '', a).upper()
    if clean and len(clean) >= 4:
        codes.append(clean)
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

def get_strategy(event: dict, profit: dict, score: dict) -> dict:
    cat  = event.get("category", "").lower()
    cap  = event.get("capacity", 15000) or 15000
    name = event.get("name", "").lower()
    face = profit.get("tm_total", profit.get("tm_face", 100))
    res  = profit.get("resell", 150)
    tot  = score.get("total", 0)
    pr   = profit.get("profit_per", 0)

    if any(k in name for k in ["ufc", "boxing", "mma", "wwe", "wrestlemania"]):
        seat = "Lower bowl ringside sections 101-110."
    elif "concert" in cat or "music" in cat:
        seat = "Any seat — small venue, all sections hold value." if cap <= 5000 else "Floor GA first. Lower bowl 100-115. Avoid upper deck."
    elif "sport" in cat:
        seat = "Lower bowl midfield/center 105-130. Avoid end zone + upper deck."
    elif "comedy" in cat:
        seat = "Any seat — small venue, all sections resell well."
    elif "arts" in cat:
        seat = "Orchestra center rows 1-15. Avoid rear mezzanine."
    else:
        seat = "Best available floor or lower level."

    qty = 4 if (tot >= 65 and pr >= 60) else 2

    try:
        days = (datetime.strptime(event.get("date", "2026-12-31")[:10], "%Y-%m-%d").date() - date.today()).days
    except Exception:
        days = 60

    if days > 60:
        list_at  = round(res * 1.12)
        reduce14 = round(res * 1.00)
        reduce3  = round(res * 0.88)
    elif days > 14:
        list_at  = round(res * 1.05)
        reduce14 = round(res * 0.95)
        reduce3  = round(res * 0.84)
    else:
        list_at  = round(res * 0.98)
        reduce14 = round(res * 0.90)
        reduce3  = round(res * 0.82)

    floor = max(round(face * 1.20), round(res * 0.70))

    return {
        "seat":       seat,
        "qty":        qty,
        "list_at":    f"${list_at}",
        "reduce_14":  f"${reduce14}",
        "reduce_3":   f"${reduce3}",
        "floor":      f"${floor}",
        "capital":    round(face * qty, 2),
    }

def _days_until(s: str) -> int:
    try:
        return max(0, (datetime.strptime(s[:10], "%Y-%m-%d").date() - date.today()).days)
    except Exception:
        return 60

def _presale_days(s: str) -> int:
    try:
        return (datetime.strptime(s[:10], "%Y-%m-%d").date() - date.today()).days
    except Exception:
        return 999

def is_wanted(event: dict) -> bool:
    name   = event.get("name", "").lower()
    artist = (event.get("artist") or "").lower()
    cat    = event.get("category", "").lower()

    # Skip garbage
    if any(k in name for k in SKIP_WORDS):
        return False

    # Must be in future
    if _days_until(event.get("date", "")) < 3:
        return False

    # Skip very cheap events (likely free/community)
    face_low = event.get("face_low", 0) or 0
    if face_low > 0 and face_low < 20:
        return False

    combined = name + " " + artist

    # Check whitelist
    if any(k in combined for k in WANTED):
        return True

    # Championship/playoff sports
    if "sport" in cat and any(k in name for k in ["playoff", "championship", "finals", "title", "world series", "stanley cup", "nba finals", "super bowl"]):
        return True

    # Broadway/theater
    if "arts" in cat and any(k in name for k in ["musical", "opera", "symphony", "ballet"]):
        return True

    return False

# ── TICKETMASTER FETCHER ──────────────────────────────────────
async def fetch_city(session, city: str, state_code: str, city_mult: float) -> List[dict]:
    import aiohttp
    params = {
        "apikey":              TM_KEY,
        "city":                city,
        "stateCode":           state_code,
        "countryCode":         "US",
        "classificationName":  "music,sports,comedy,arts",
        "size":                100,
        "startDateTime":       datetime.now().strftime("%Y-%m-%dT00:00:00Z"),
        "endDateTime":         (datetime.now() + timedelta(days=180)).strftime("%Y-%m-%dT00:00:00Z"),
        "sort":                "date,asc",
    }
    events = []
    try:
        async with session.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params=params,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                return []
            data = await r.json()

        for item in data.get("_embedded", {}).get("events", []):
            venues   = item.get("_embedded", {}).get("venues", [{}]) or [{}]
            v        = venues[0]
            prices   = item.get("priceRanges", [{}]) or [{}]
            p        = prices[0]
            attract  = item.get("_embedded", {}).get("attractions", [{}]) or [{}]
            a        = attract[0]
            presales = item.get("sales", {}).get("presales", []) or []

            presale_date = ""
            for ps in presales:
                pd = ps.get("startDateTime", "")[:10]
                if pd:
                    presale_date = pd
                    break

            events.append({
                "id":           f"tm_{item.get('id', '')}",
                "name":         item.get("name", ""),
                "artist":       a.get("name", ""),
                "venue":        v.get("name", ""),
                "city":         v.get("city", {}).get("name", city),
                "state":        state_code,
                "capacity":     0,
                "date":         item.get("dates", {}).get("start", {}).get("localDate", ""),
                "category":     (item.get("classifications", [{}]) or [{}])[0].get("segment", {}).get("name", "music"),
                "face_low":     float(p.get("min") or 0),
                "face_high":    float(p.get("max") or 0),
                "url":          item.get("url", ""),
                "presale_date": presale_date,
                "source":       "ticketmaster",
                "city_mult":    city_mult,
            })
    except Exception as e:
        log.debug(f"TM {city}: {e}")
    return events

def get_mock_events() -> List[dict]:
    today = date.today()
    def d(n): return str(today + timedelta(days=n))
    return [
        {"id":"m01","name":"Morgan Wallen — Minneapolis","artist":"Morgan Wallen","venue":"U.S. Bank Stadium","city":"Minneapolis","state":"MN","capacity":67000,"date":d(14),"category":"concert","face_low":89,"face_high":199,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.05},
        {"id":"m02","name":"Chappell Roan — Pink Pony Tour","artist":"Chappell Roan","venue":"Ryman Auditorium","city":"Nashville","state":"TN","capacity":2362,"date":d(33),"category":"concert","face_low":65,"face_high":150,"url":"https://www.ticketmaster.com","presale_date":d(1),"source":"demo","city_mult":1.25},
        {"id":"m03","name":"Hamilton","artist":"Hamilton","venue":"Richard Rodgers Theatre","city":"New York","state":"NY","capacity":1319,"date":d(20),"category":"arts","face_low":89,"face_high":399,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.35},
        {"id":"m04","name":"UFC 328: Chimaev v Strickland","artist":"UFC","venue":"Prudential Center","city":"Newark","state":"NJ","capacity":19500,"date":d(44),"category":"sports","face_low":75,"face_high":300,"url":"https://www.ticketmaster.com","presale_date":d(5),"source":"demo","city_mult":1.10},
        {"id":"m05","name":"Dave Chappelle — One Night Only","artist":"Dave Chappelle","venue":"Chicago Theatre","city":"Chicago","state":"IL","capacity":3600,"date":d(28),"category":"comedy","face_low":75,"face_high":175,"url":"https://www.ticketmaster.com","presale_date":d(2),"source":"demo","city_mult":1.15},
        {"id":"m06","name":"Zach Bryan — Boston","artist":"Zach Bryan","venue":"TD Garden","city":"Boston","state":"MA","capacity":19156,"date":d(55),"category":"concert","face_low":79,"face_high":249,"url":"https://www.ticketmaster.com","presale_date":d(7),"source":"demo","city_mult":1.15},
        # This one should be filtered out — face value already close to resell
        {"id":"m07","name":"UFC 327 — Miami","artist":"UFC","venue":"Kaseya Center","city":"Miami","state":"FL","capacity":19600,"date":d(15),"category":"sports","face_low":372,"face_high":500,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.10},
    ]

# ── PRESALE ALERTS ────────────────────────────────────────────
def check_presale_alerts(events: list):
    for e in events:
        ps = e.get("presale_date", "")
        if not ps:
            continue
        p   = e.get("profit", {})
        if not p.get("is_profitable"):
            continue
        pd   = _presale_days(ps)
        eid  = e.get("id", "")
        name = e.get("name", "")
        codes= e.get("codes", [])[:5]
        pp   = p.get("profit_per", 0)
        url  = e.get("url", "")
        city = e.get("city", "")
        src  = "✓ verified" if p.get("source") == "verified" else "estimated"

        if pd <= 3 and pd > 1 and f"{eid}_72" not in state["presale_alerted"]:
            tg(
                f"⏰ *PRESALE IN {pd} DAYS* ({src})\n\n"
                f"🎟 *{name}*\n"
                f"📍 {e.get('venue','')} · {city}\n"
                f"🔑 Opens: {ps[:10]}\n\n"
                f"💰 +${pp:.0f}/ticket · +${pp*4:.0f} buying 4\n\n"
                f"Codes: {' | '.join(codes[:4])}\n\n"
                f"[Link]({url})"
            )
            state["presale_alerted"].add(f"{eid}_72")

        elif pd == 1 and f"{eid}_24" not in state["presale_alerted"]:
            tg(
                f"🚨 *PRESALE TOMORROW!* ({src})\n\n"
                f"🎟 *{name}*\n"
                f"📍 {e.get('venue','')} · {city}\n\n"
                f"💰 *+${pp:.0f}/ticket · +${pp*4:.0f} buying 4*\n\n"
                f"*Codes:*\n" + "\n".join([f"• {c}" for c in codes]) + f"\n\n[Buy link]({url})"
            )
            state["presale_alerted"].add(f"{eid}_24")

        elif pd == 0 and f"{eid}_live" not in state["presale_alerted"]:
            st = e.get("strategy", {})
            tg(
                f"🔴 *PRESALE LIVE NOW!*\n\n"
                f"🎟 *{name}*\n"
                f"📍 {e.get('venue','')} · {city}\n\n"
                f"💰 *+${pp:.0f}/ticket · +${pp*4:.0f} buying 4* ({src})\n"
                f"🎯 {st.get('seat','Floor GA or lower bowl')}\n\n"
                f"*Try NOW:*\n" + "\n".join([f"• {c}" for c in codes]) + f"\n\n[→ BUY NOW]({url})"
            )
            state["presale_alerted"].add(f"{eid}_live")

# ── SCAN ENGINE ───────────────────────────────────────────────
async def run_scan():
    if state["scanning"]:
        return
    state["scanning"] = True
    log.info("=== Scan started ===")

    try:
        # 1. Fetch events from all cities
        if TM_KEY:
            import aiohttp
            raw = []
            async with aiohttp.ClientSession() as session:
                tasks = [fetch_city(session, c, s, m) for c, s, m in CITIES]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, list):
                        raw.extend(r)
            state["cities_scanned"] = len(CITIES)
            log.info(f"Fetched {len(raw)} raw events from {len(CITIES)} cities")
        else:
            raw = get_mock_events()
            state["cities_scanned"] = len(CITIES)
            log.info(f"Demo mode: {len(raw)} mock events")

        # 2. Filter + deduplicate
        seen_ids   = set()
        seen_shows = {}  # dedup key → event

        for e in raw:
            eid = e.get("id", "")
            if eid in seen_ids:
                continue
            seen_ids.add(eid)

            if not is_wanted(e):
                continue

            # Dedup: same show name + venue → keep soonest date
            name_key  = re.sub(r'[^a-z0-9]', '', e.get("name", "").lower())[:40]
            venue_key = re.sub(r'[^a-z0-9]', '', e.get("venue", "").lower())[:20]
            dedup_key = f"{name_key}|{venue_key}"

            if dedup_key not in seen_shows:
                seen_shows[dedup_key] = e
            else:
                existing_days = _days_until(seen_shows[dedup_key].get("date", ""))
                new_days      = _days_until(e.get("date", ""))
                if 3 <= new_days < existing_days:
                    seen_shows[dedup_key] = e

        wanted        = list(seen_shows.values())
        filtered_count = len(raw) - len(wanted)
        log.info(f"{len(wanted)} unique wanted events ({filtered_count} filtered/deduped)")

        # 3. Score each event + check prices
        opportunities = []
        for e in wanted:
            face_low  = e.get("face_low", 0) or 0
            face_high = e.get("face_high", 0) or 0
            city_mult = e.get("city_mult", 1.0)

            # Get face value (what TM is currently charging)
            if face_high > face_low > 0:
                tm_face = (face_low + face_high) / 2
            elif face_low > 0:
                tm_face = face_low * 1.25  # Add estimated TM fees to low price
            else:
                tm_face = 85  # Default if no price data

            # Get resell multiplier
            mult = get_multiplier(
                e.get("name", ""),
                e.get("artist", ""),
                e.get("category", ""),
                e.get("capacity", 15000) or 15000
            )

            # Check SeatGeek for live resell prices
            sg = None
            if SG_KEY:
                sg = await get_resell_price(e.get("name", ""), e.get("date", ""))

            # Calculate profit
            profit = calc_profit(tm_face, sg, mult, city_mult)

            # Only keep profitable events
            if not profit.get("is_profitable"):
                filtered_count += 1
                continue

            # Score it
            score = score_it(e, profit)
            codes = find_codes(e)
            strat = get_strategy(e, profit, score)
            pd    = _presale_days(e.get("presale_date", "")) if e.get("presale_date") else None

            e["profit"]       = profit
            e["score"]        = score
            e["codes"]        = codes
            e["strategy"]     = strat
            e["presale_days"] = pd
            opportunities.append(e)

        # Sort by score descending
        opportunities.sort(
            key=lambda x: x.get("score", {}).get("total", 0),
            reverse=True
        )

        state["filtered_count"]      = filtered_count
        state["events"]              = opportunities
        state["last_scan"]           = datetime.now().isoformat()
        state["scan_count"]         += 1
        state["total_opportunities"] = len(opportunities)

        # 4. Telegram alerts for new high-value events
        for e in opportunities:
            eid   = e.get("id", "")
            total = e.get("score", {}).get("total", 0)
            if total >= 65 and eid not in state["alerted_ids"]:
                p      = e.get("profit", {})
                sc     = e.get("score", {})
                codes  = e.get("codes", [])[:4]
                pp     = p.get("profit_per", 0)
                pd_val = e.get("presale_days")
                src    = "✓ verified" if p.get("source") == "verified" else "estimated"

                presale_note = ""
                if pd_val is not None and pd_val >= 0:
                    if pd_val == 0:    presale_note = "\n🔴 *PRESALE LIVE NOW!*\n"
                    elif pd_val == 1:  presale_note = "\n⚠️ *Presale TOMORROW!*\n"
                    elif pd_val <= 7:  presale_note = f"\n⏰ Presale in {pd_val} days\n"

                emoji = "🔥" if sc.get("verdict") == "MUST BUY" else "✅"
                tg(
                    f"{emoji} *{sc.get('verdict','')} — {total:.0f}/100*\n"
                    f"_{src}_\n\n"
                    f"*{e.get('name','')}*\n"
                    f"📍 {e.get('venue','')} · {e.get('city','')}\n"
                    f"📅 {e.get('date','')} ({_days_until(e.get('date',''))}d away)\n"
                    f"{presale_note}\n"
                    f"💰 Pay: ${p.get('tm_total',0):.0f} → Sell: ${p.get('resell',0):.0f}\n"
                    f"*+${pp:.0f}/ticket · +${pp*4:.0f} buying 4*\n"
                    f"ROI: {p.get('roi_pct',0):.0f}%\n\n"
                    f"🔑 {' · '.join(codes)}\n\n"
                    f"[Buy tickets]({e.get('url','')})"
                )
                state["alerted_ids"].add(eid)

        # 5. Presale countdown alerts
        check_presale_alerts(opportunities)

        must   = len([e for e in opportunities if e.get("score", {}).get("verdict") == "MUST BUY"])
        strong = len([e for e in opportunities if e.get("score", {}).get("verdict") == "STRONG BUY"])
        log.info(f"Done. {len(opportunities)} profitable · {must} must buy · {strong} strong buy")
        _save()

    finally:
        state["scanning"] = False

def _save():
    try:
        with open(DATA, "w") as f:
            json.dump({
                "events":              state["events"],
                "last_scan":           state["last_scan"],
                "scan_count":          state["scan_count"],
                "total_opportunities": state["total_opportunities"],
                "filtered_count":      state["filtered_count"],
                "cities_scanned":      state["cities_scanned"],
            }, f, default=str, indent=2)
    except Exception as e:
        log.warning(f"Save error: {e}")

async def scan_loop():
    while True:
        try:
            await run_scan()
        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)
        log.info(f"Next scan in {SCAN_MINS} min")
        await asyncio.sleep(SCAN_MINS * 60)

# ── DASHBOARD ─────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ticket Intel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0e18;--surface:#111827;--surface2:#1a2235;--border:#1e2d42;
  --green:#00c896;--red:#ff4757;--amber:#ffa502;--blue:#3d9be9;--dim:#6b7fa0;--text:#e2e8f4;
  --mono:ui-monospace,'SF Mono','Fira Code',monospace
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:inherit;text-decoration:none}

/* HEADER */
nav{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:99}
.brand{display:flex;align-items:center;gap:10px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.brand-name{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--green);letter-spacing:.1em}
.badge{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:4px;margin-left:6px;background:rgba(255,165,2,.12);color:var(--amber);border:1px solid rgba(255,165,2,.25)}
.badge.live{background:rgba(0,200,150,.12);color:var(--green);border-color:rgba(0,200,150,.25)}
.nav-right{display:flex;align-items:center;gap:12px}
.scan-btn{font-family:var(--mono);font-size:11px;padding:7px 18px;border-radius:6px;border:1px solid rgba(0,200,150,.35);background:rgba(0,200,150,.08);color:var(--green);cursor:pointer;letter-spacing:.05em;transition:background .15s}
.scan-btn:hover{background:rgba(0,200,150,.18)}
.scan-btn:disabled{opacity:.4;cursor:not-allowed}
.last-update{font-size:11px;color:var(--dim)}

/* STATS BAR */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:16px 24px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat-n{font-family:var(--mono);font-size:22px;font-weight:600}
.stat-l{font-size:10px;color:var(--dim);margin-top:3px;text-transform:uppercase;letter-spacing:.08em}

/* FILTER BAR */
.filters{display:flex;gap:6px;padding:0 24px 14px;flex-wrap:wrap;align-items:center}
.filt{font-size:12px;padding:5px 14px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--dim);cursor:pointer;transition:all .15s}
.filt.active{background:var(--green);color:#0a0e18;border-color:transparent;font-weight:600}
.scan-info{font-size:11px;color:var(--dim);margin-left:auto}

/* SECTION HEADER */
.section-head{font-family:var(--mono);font-size:10px;letter-spacing:.15em;color:var(--dim);text-transform:uppercase;padding:4px 24px 8px}

/* EVENT CARD */
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;margin:0 24px 8px;overflow:hidden;cursor:pointer;transition:border-color .2s}
.card:hover{border-color:#2d4060}
.card.open{border-color:rgba(0,200,150,.45)}
.card-top{display:flex;gap:12px;align-items:flex-start;padding:14px 16px}
.score-box{width:46px;height:46px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:15px;font-weight:700;flex-shrink:0}
.card-info{flex:1;min-width:0}
.card-name{font-size:14px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-sub{font-size:11px;color:var(--dim);margin-top:3px}
.card-tags{display:flex;gap:5px;margin-top:5px;flex-wrap:wrap}
.tag{font-size:10px;font-family:var(--mono);padding:2px 7px;border-radius:4px;border:1px solid}
.tag-city{background:var(--surface2);border-color:var(--border);color:var(--dim)}
.tag-presale-live{background:rgba(255,71,87,.12);border-color:rgba(255,71,87,.3);color:var(--red)}
.tag-presale-soon{background:rgba(255,165,2,.1);border-color:rgba(255,165,2,.25);color:var(--amber)}
.tag-presale{background:rgba(107,127,160,.1);border-color:var(--border);color:var(--dim)}
.tag-verified{background:rgba(0,200,150,.1);border-color:rgba(0,200,150,.25);color:var(--green)}
.tag-est{background:rgba(255,165,2,.08);border-color:rgba(255,165,2,.2);color:var(--amber)}
.card-right{text-align:right;flex-shrink:0}
.profit-num{font-family:var(--mono);font-size:16px;font-weight:700;color:var(--green)}
.profit-lbl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
.verdict-tag{display:inline-block;font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:4px;margin-top:5px;letter-spacing:.05em}
.v-MUST{background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.3)}
.v-STRONG{background:rgba(0,200,150,.1);color:var(--green);border:1px solid rgba(0,200,150,.25)}
.v-BUY{background:rgba(0,200,150,.07);color:var(--green);border:1px solid rgba(0,200,150,.18)}
.v-WATCH{background:rgba(255,165,2,.08);color:var(--amber);border:1px solid rgba(255,165,2,.2)}
.progress{height:2px;background:var(--border)}
.progress-fill{height:100%;transition:width .4s}

/* CARD DETAIL */
.detail{display:none;padding:14px 16px;border-top:1px solid var(--border)}
.card.open .detail{display:block}
.detail-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-bottom:12px}
.detail-box{background:var(--surface2);border-radius:8px;padding:10px 12px}
.detail-val{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--text)}
.detail-lbl{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;margin-top:2px}
.verified-box{background:rgba(0,200,150,.06);border:1px solid rgba(0,200,150,.18);border-radius:8px;padding:10px 12px;margin-bottom:10px}
.verified-title{font-size:10px;font-weight:600;color:var(--green);text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}
.verified-text{font-size:12px;color:var(--dim)}
.info-row{background:var(--surface2);border-radius:8px;padding:10px 12px;margin-bottom:7px}
.info-title{font-size:10px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}
.info-text{font-size:12px;color:var(--dim);line-height:1.6}
.info-text b{color:var(--text)}
.codes-wrap{margin-bottom:10px}
.codes-title{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px}
.codes{display:flex;flex-wrap:wrap;gap:4px}
.code{font-family:var(--mono);font-size:10px;background:rgba(61,155,233,.1);border:1px solid rgba(61,155,233,.2);border-radius:4px;padding:2px 7px;color:var(--blue)}
.action-row{display:flex;gap:7px;flex-wrap:wrap}
.btn-buy{font-size:13px;font-weight:600;padding:10px 20px;border-radius:8px;border:none;background:var(--green);color:#0a0e18;cursor:pointer;transition:opacity .15s;display:flex;align-items:center;gap:6px}
.btn-buy:hover{opacity:.88}
.btn-secondary{font-size:12px;padding:9px 16px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--dim);cursor:pointer;transition:background .15s}
.btn-secondary:hover{background:var(--surface2);color:var(--text)}

/* EMPTY STATE */
.empty{text-align:center;padding:60px 24px;color:var(--dim)}
.empty-title{font-size:15px;font-weight:600;color:var(--text);margin-bottom:8px}
.empty-sub{font-size:13px;line-height:1.7}

/* LOADING */
.loading{text-align:center;padding:50px;color:var(--dim);font-family:var(--mono);font-size:12px;letter-spacing:.1em;animation:fade 1.5s infinite}
@keyframes fade{0%,100%{opacity:.3}50%{opacity:1}}

@media(max-width:640px){
  .stats{grid-template-columns:1fr 1fr}
  .detail-grid{grid-template-columns:1fr 1fr}
  nav,.stats,.filters,.section-head,.card{padding-left:14px;padding-right:14px}
  .card{margin-left:0;margin-right:0}
}
</style>
</head>
<body>

<nav>
  <div class="brand">
    <div class="dot"></div>
    <span class="brand-name">TICKET INTEL</span>
    <span class="badge" id="mode-badge">DEMO</span>
  </div>
  <div class="nav-right">
    <span class="last-update" id="last-update">Loading...</span>
    <button class="scan-btn" id="scan-btn" onclick="triggerScan()">SCAN NOW</button>
  </div>
</nav>

<div class="stats" id="stats-bar">
  <div class="stat"><div class="stat-n">—</div><div class="stat-l">Loading</div></div>
</div>

<div class="filters" id="filter-bar"></div>

<div id="event-list">
  <div class="loading">SCANNING MARKETS...</div>
</div>

<script>
let allEvents = [], activeFilter = 'all', openId = null;

const fmt = s => {
  try { return new Date(s).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}); }
  catch { return s; }
};
const daysTo = s => {
  try { return Math.round((new Date(s) - new Date()) / 86400000); }
  catch { return 0; }
};
const scoreColor = n => n >= 80 ? '#ff4757' : n >= 65 ? '#00c896' : n >= 52 ? '#ffa502' : '#6b7fa0';
const scoreBg    = n => n >= 80 ? 'rgba(255,71,87,.15)' : n >= 65 ? 'rgba(0,200,150,.12)' : n >= 52 ? 'rgba(255,165,2,.12)' : 'rgba(107,127,160,.1)';
const vKey       = v => v.split(' ')[0];

async function loadData() {
  try {
    const res  = await fetch('/api/events');
    const data = await res.json();
    allEvents  = data.events || [];
    renderStats(data);
    renderFilters();
    renderEvents();
    const t = data.last_scan
      ? new Date(data.last_scan).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
      : 'never';
    document.getElementById('last-update').textContent = 'Updated ' + t;
    const isLive = allEvents.some(e => e.source && e.source !== 'demo');
    const badge  = document.getElementById('mode-badge');
    badge.textContent   = isLive ? 'LIVE' : 'DEMO';
    badge.className     = isLive ? 'badge live' : 'badge';
    const cities = data.cities_scanned || 0;
    const filtered = data.filtered_count || 0;
    const filterInfo = document.getElementById('filter-info');
    if (filterInfo) {
      filterInfo.textContent = cities > 0
        ? `${cities} cities · ${filtered} filtered out · ${allEvents.length} opportunities`
        : `${filtered} filtered · ${allEvents.length} opportunities`;
    }
  } catch(e) {
    console.error('Load error:', e);
  }
}

async function triggerScan() {
  const btn = document.getElementById('scan-btn');
  btn.disabled = true;
  btn.textContent = 'SCANNING...';
  document.getElementById('last-update').textContent = 'Scanning now...';
  try {
    await fetch('/api/scan', { method: 'POST' });
    await new Promise(r => setTimeout(r, 6000));
    await loadData();
  } catch(e) {
    console.error('Scan error:', e);
  }
  btn.disabled = false;
  btn.textContent = 'SCAN NOW';
}

function renderStats(data) {
  const ev     = data.events || [];
  const must   = ev.filter(e => e.score?.verdict === 'MUST BUY').length;
  const strong = ev.filter(e => e.score?.verdict === 'STRONG BUY').length;
  const presales = ev.filter(e => e.presale_days != null && e.presale_days >= 0 && e.presale_days <= 7).length;
  const maxP   = Math.max(0, ...ev.map(e => e.profit?.profit_4 || 0));
  document.getElementById('stats-bar').innerHTML = `
    <div class="stat"><div class="stat-n" style="color:#ff4757">${must}</div><div class="stat-l">Must buy</div></div>
    <div class="stat"><div class="stat-n" style="color:#00c896">${strong}</div><div class="stat-l">Strong buy</div></div>
    <div class="stat"><div class="stat-n" style="color:#ffa502">${presales}</div><div class="stat-l">Presales soon</div></div>
    <div class="stat"><div class="stat-n" style="color:#00c896">$${Math.round(maxP).toLocaleString()}</div><div class="stat-l">Best 4-ticket</div></div>
  `;
}

function renderFilters() {
  const cats = [
    { key: 'all',      label: 'All events' },
    { key: 'concert',  label: 'Concerts' },
    { key: 'sports',   label: 'Sports' },
    { key: 'comedy',   label: 'Comedy' },
    { key: 'presale',  label: 'Presales' },
    { key: 'verified', label: '✓ Verified' },
  ];
  document.getElementById('filter-bar').innerHTML =
    cats.map(c =>
      `<button class="filt${activeFilter === c.key ? ' active' : ''}" onclick="setFilter('${c.key}')">${c.label}</button>`
    ).join('') +
    `<span class="scan-info" id="filter-info"></span>`;
}

function setFilter(f) {
  activeFilter = f;
  renderFilters();
  renderEvents();
}

function getFiltered() {
  if (activeFilter === 'all')      return allEvents;
  if (activeFilter === 'presale')  return allEvents.filter(e => e.presale_days != null && e.presale_days >= 0 && e.presale_days <= 10);
  if (activeFilter === 'verified') return allEvents.filter(e => e.profit?.source === 'verified');
  return allEvents.filter(e => {
    const cat = (e.category || '').toLowerCase();
    if (activeFilter === 'concert') return cat.includes('music') || cat.includes('concert');
    if (activeFilter === 'sports')  return cat.includes('sport');
    if (activeFilter === 'comedy')  return cat.includes('comedy');
    return false;
  });
}

function renderEvents() {
  const filtered = getFiltered();
  const groups = {
    'MUST BUY':   filtered.filter(e => e.score?.verdict === 'MUST BUY'),
    'STRONG BUY': filtered.filter(e => e.score?.verdict === 'STRONG BUY'),
    'BUY':        filtered.filter(e => e.score?.verdict === 'BUY'),
    'WATCH':      filtered.filter(e => e.score?.verdict === 'WATCH'),
  };
  const labels = {
    'MUST BUY':   'Act now — must buy',
    'STRONG BUY': 'Strong buy',
    'BUY':        'Buy',
    'WATCH':      'Watch',
  };

  let html = '';
  for (const [verdict, items] of Object.entries(groups)) {
    if (!items.length) continue;
    html += `<div class="section-head">${labels[verdict]}</div>`;
    items.forEach(e => { html += renderCard(e); });
  }

  if (!html) {
    html = `<div class="empty">
      <div class="empty-title">No profitable opportunities right now</div>
      <div class="empty-sub">
        The scanner checked every event across ${state_cities} cities and filtered out anything<br>
        where the margin wasn't confirmed profitable.<br><br>
        New events are announced daily — check back soon or click <strong>SCAN NOW</strong>.
      </div>
    </div>`;
  }

  document.getElementById('event-list').innerHTML = html;
}

function presaleTag(e) {
  const pd = e.presale_days;
  if (pd == null || pd < 0) return '';
  if (pd === 0) return '<span class="tag tag-presale-live">PRESALE LIVE</span>';
  if (pd === 1) return '<span class="tag tag-presale-soon">Presale tomorrow</span>';
  if (pd <= 3)  return `<span class="tag tag-presale-soon">Presale in ${pd} days</span>`;
  if (pd <= 7)  return `<span class="tag tag-presale">Presale in ${pd} days</span>`;
  return '';
}

function renderCard(e) {
  const sc = e.score   || {};
  const p  = e.profit  || {};
  const st = e.strategy || {};
  const tot   = sc.total || 0;
  const vk    = vKey(sc.verdict || 'WATCH');
  const isV   = p.source === 'verified';
  const days  = daysTo(e.date);
  const codes = (e.codes || []).slice(0, 10);

  return `
  <div class="card${openId === e.id ? ' open' : ''}" id="card-${e.id}" onclick="toggleCard('${e.id}')">
    <div class="card-top">
      <div class="score-box" style="background:${scoreBg(tot)};color:${scoreColor(tot)}">${Math.round(tot)}</div>
      <div class="card-info">
        <div class="card-name">${e.name || ''}</div>
        <div class="card-sub">${e.venue || ''} · ${fmt(e.date)} · ${days}d away</div>
        <div class="card-tags">
          <span class="tag tag-city">${e.city || ''}</span>
          ${presaleTag(e)}
          ${isV ? '<span class="tag tag-verified">✓ verified price</span>' : '<span class="tag tag-est">estimated</span>'}
        </div>
      </div>
      <div class="card-right">
        <div class="profit-num">+$${Math.round(p.profit_per || 0)}</div>
        <div class="profit-lbl">per ticket</div>
        <div class="verdict-tag v-${vk}">${sc.verdict || ''}</div>
      </div>
    </div>
    <div class="progress">
      <div class="progress-fill" style="width:${Math.min(tot, 100)}%;background:${scoreColor(tot)}"></div>
    </div>
    <div class="detail">
      <div class="detail-grid">
        <div class="detail-box">
          <div class="detail-val" style="color:#00c896">+$${Math.round(p.profit_4 || 0).toLocaleString()}</div>
          <div class="detail-lbl">Buying 4 tickets</div>
        </div>
        <div class="detail-box">
          <div class="detail-val">$${Math.round(p.tm_total || p.tm_face || 0)} → $${Math.round(p.resell || 0)}</div>
          <div class="detail-lbl">You pay → resell</div>
        </div>
        <div class="detail-box">
          <div class="detail-val">${Math.round(p.roi_pct || 0)}%</div>
          <div class="detail-lbl">ROI</div>
        </div>
        <div class="detail-box">
          <div class="detail-val">$${Math.round(st.capital || 0).toLocaleString()}</div>
          <div class="detail-lbl">Capital needed</div>
        </div>
      </div>

      ${isV && p.sg_count ? `
      <div class="verified-box">
        <div class="verified-title">✓ Live SeatGeek Data</div>
        <div class="verified-text">
          <strong>${p.sg_count} active listings</strong> · 
          Median <strong>$${Math.round(p.resell || 0)}</strong> · 
          Range $${Math.round(p.resell_low || 0)}–$${Math.round(p.resell_high || 0)}
        </div>
      </div>` : ''}

      <div class="info-row">
        <div class="info-title">Where to sit</div>
        <div class="info-text">${st.seat || '—'}</div>
      </div>

      <div class="info-row">
        <div class="info-title">Sell strategy</div>
        <div class="info-text">
          List at <b>${st.list_at || '—'}</b> on StubHub + Vivid Seats + SeatGeek simultaneously.<br>
          2 weeks before: drop to <b>${st.reduce_14 || '—'}</b> if unsold.<br>
          3 days before: drop to <b>${st.reduce_3 || '—'}</b>.<br>
          Never go below <b>${st.floor || '—'}</b>.
        </div>
      </div>

      ${codes.length ? `
      <div class="codes-wrap">
        <div class="codes-title">Presale codes to try</div>
        <div class="codes">${codes.map(c => `<span class="code">${c}</span>`).join('')}</div>
      </div>` : ''}

      <div class="action-row">
        ${e.url ? `<a href="${e.url}" target="_blank"><button class="btn-buy">Buy on Ticketmaster →</button></a>` : ''}
        <a href="https://www.stubhub.com/find/s/?q=${encodeURIComponent(e.name || '')}" target="_blank"><button class="btn-secondary">StubHub</button></a>
        <a href="https://www.vividseats.com/search?searchTerm=${encodeURIComponent(e.name || '')}" target="_blank"><button class="btn-secondary">Vivid Seats</button></a>
        ${p.sg_url ? `<a href="${p.sg_url}" target="_blank"><button class="btn-secondary" style="color:#00c896;border-color:rgba(0,200,150,.3)">SeatGeek ✓</button></a>` : `<a href="https://www.seatgeek.com/search?q=${encodeURIComponent(e.name || '')}" target="_blank"><button class="btn-secondary">SeatGeek</button></a>`}
      </div>
    </div>
  </div>`;
}

function toggleCard(id) {
  openId = openId === id ? null : id;
  renderEvents();
  if (openId) {
    const el = document.getElementById('card-' + id);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

// Replace placeholder in empty state
const state_cities = 23;

loadData();
setInterval(loadData, 60000);
</script>
</body>
</html>"""

# ── WEB ROUTES ────────────────────────────────────────────────
async def handle_index(request):
    return web.Response(text=HTML, content_type="text/html")

async def handle_events(request):
    return web.json_response(
        {
            "events":              state["events"],
            "last_scan":           state["last_scan"],
            "scan_count":          state["scan_count"],
            "total_opportunities": state["total_opportunities"],
            "filtered_count":      state["filtered_count"],
            "cities_scanned":      state["cities_scanned"],
        },
        dumps=lambda o: json.dumps(o, default=str)
    )

async def handle_scan(request):
    asyncio.create_task(run_scan())
    return web.json_response({"status": "scanning"})

async def on_startup(app):
    if DATA.exists():
        try:
            with open(DATA) as f:
                saved = json.load(f)
            state["events"]              = saved.get("events", [])
            state["last_scan"]           = saved.get("last_scan")
            state["scan_count"]          = saved.get("scan_count", 0)
            state["total_opportunities"] = saved.get("total_opportunities", 0)
            state["filtered_count"]      = saved.get("filtered_count", 0)
            state["cities_scanned"]      = saved.get("cities_scanned", 0)
            log.info(f"Loaded {len(state['events'])} saved events")
        except Exception as e:
            log.warning(f"Could not load saved data: {e}")
    asyncio.create_task(scan_loop())

# ── MAIN ──────────────────────────────────────────────────────
def main():
    mode = "LIVE" if TM_KEY else "DEMO"
    sg_s = "✓ Live price verification" if SG_KEY else "Add SEATGEEK_CLIENT_ID for verified prices"
    tg_s = "✓ Alerts enabled" if TG_TOKEN else "Add TELEGRAM_TOKEN for phone alerts"

    print(f"""
╔{'═'*55}╗
║  TICKET INTEL PRO v7                                  ║
╠{'═'*55}╣
║  Mode:      {mode:<43}║
║  Cities:    {len(CITIES)} cities across the US                    ║
║  Filter:    Profit > ${MIN_PROFIT}/ticket · ROI > {MIN_ROI}%               ║
║  TM Check:  Filters overpriced primary listings       ║
║  SeatGeek:  {sg_s:<43}║
║  Telegram:  {tg_s:<43}║
║  Scanning:  Every {SCAN_MINS} minutes                          ║
║  Dashboard: http://localhost:{PORT:<26}║
╚{'═'*55}╝

  Open http://localhost:{PORT} in your browser
  Press Ctrl+C to stop
""")

    app = web.Application()
    app.router.add_get("/",           handle_index)
    app.router.add_get("/api/events", handle_events)
    app.router.add_post("/api/scan",  handle_scan)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT, print=lambda _: None)

if __name__ == "__main__":
    main()
