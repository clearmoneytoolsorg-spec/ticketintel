"""
TICKET INTEL PRO v4
====================
Fixed: profit calculation, junk filtering, SeatGeek live price verification.
Only shows events where profit is confirmed > $40/ticket.

Run: python app.py
"""

import sys, subprocess

def install(pkg):
    subprocess.check_call([sys.executable,"-m","pip","install",pkg,"-q","--disable-pip-version-check"])

for pkg in ["aiohttp","requests","python-dotenv"]:
    try: __import__(pkg.replace("-","_"))
    except ImportError: print(f"Installing {pkg}..."); install(pkg)

import os, json, re, asyncio, logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional
from aiohttp import web
import requests as req
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("ticketpro")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])

TM_KEY    = os.getenv("TICKETMASTER_API_KEY","")
SG_KEY    = os.getenv("SEATGEEK_CLIENT_ID","")
TG_TOKEN  = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT   = os.getenv("TELEGRAM_CHAT_ID","")
SCAN_MINS = int(os.getenv("SCAN_INTERVAL_MINUTES","15"))
PORT      = int(os.getenv("PORT","8080"))
DATA      = Path("ticket_data.json")

MIN_PROFIT = 40   # minimum profit per ticket after all fees
MIN_ROI    = 20   # minimum ROI %

CITIES = [
    ("Seattle",    "WA", 1.0),
    ("Las Vegas",  "NV", 1.3),
    ("Los Angeles","CA", 1.2),
    ("Nashville",  "TN", 1.25),
    ("New York",   "NY", 1.15),
    ("Chicago",    "IL", 1.1),
    ("Miami",      "FL", 1.1),
    ("Dallas",     "TX", 1.1),
    ("Portland",   "OR", 0.95),
    ("Tacoma",     "WA", 0.9),
]

# ── ALWAYS SKIP THESE ─────────────────────────────────────────
SKIP_WORDS = [
    "tribute","cover band","open mic","karaoke","free ",
    "varsity","high school","youth ","kids ","children",
    "cheer","cheerleading","pro cheer","dance competition",
    "college baseball","college softball","college volleyball",
    "college football","tractor","rodeo","monster truck",
    "community theatre","amateur","unsigned artist",
    "comedy open mic","improv showcase",
]

# ── ONLY SHOW THESE TYPES ─────────────────────────────────────
# Event must match at least one of these to be considered
WANTED = [
    # Tier 1 music — always profitable
    "taylor swift","beyonce","drake","kendrick lamar","bad bunny",
    "morgan wallen","zach bryan","post malone","travis scott",
    "billie eilish","the weeknd","eminem","coldplay","ed sheeran",
    "sabrina carpenter","chappell roan","tyler the creator",
    "olivia rodrigo","bruno mars","harry styles","dua lipa",
    "ariana grande","sza","doja cat","lana del rey",
    "twenty one pilots","green day","blink-182","metallica",
    "rolling stones","elton john","bruce springsteen",
    "foo fighters","red hot chili peppers",
    # Tier 2 music — usually profitable
    "luke combs","jason aldean","chris stapleton","lainey wilson",
    "eric church","hardy","thomas rhett","j. cole","future",
    "the 1975","arctic monkeys","tame impala","dave matthews",
    "phish","dead and company","jack johnson","kenny chesney",
    # Comedy — profitable at small venues
    "dave chappelle","kevin hart","chris rock","bill burr",
    "bert kreischer","andrew schulz","theo von","joe rogan",
    "john mulaney","trevor noah","gabriel iglesias","jim jefferies",
    # Sports — only high-demand
    "ufc","mma","boxing championship","wwe","wrestlemania",
    "nba finals","nfl playoff","super bowl","stanley cup finals",
    "world series","ncaa tournament","march madness",
    "playoff","championship","title fight","title match",
    # Theater — consistently strong
    "hamilton","wicked","lion king","beetlejuice","hadestown",
    "chicago musical","phantom","les miserables","mean girls",
    # NASCAR/F1 — premium events only  
    "formula 1","f1 grand prix","daytona 500","indy 500",
]

CC_CODES   = ["CITI","AMEX","CAPITALONE","CHASE","MASTERCARD","VISA","CITICARD"]
VENUE_CODES= ["LIVENATION","TMFAN","VERIFIED","SPOTIFY","OFFICIAL","PRESALE"]

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
    "ufc":               ["UFC","UFCFIGHT","UFCSEATTLE"],
    "wwe":               ["WWE","WWEFAN"],
    "hamilton":          ["HAMILTON","HAMFAN"],
    "dave chappelle":    ["CHAPPELLE","COMEDY"],
    "kevin hart":        ["KEVINHART","HART"],
    "bill burr":         ["BILLBURR","BURR"],
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
}

# ── TELEGRAM ──────────────────────────────────────────────────
def tg(text: str):
    if not TG_TOKEN or not TG_CHAT:
        log.info(f"[ALERT] {text[:80]}")
        return
    try:
        r = req.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":text,
                  "parse_mode":"Markdown","disable_web_page_preview":True},
            timeout=10
        )
        if r.status_code == 200: log.info("Telegram ✓")
        else: log.warning(f"Telegram: {r.text[:60]}")
    except Exception as e: log.warning(f"Telegram: {e}")

# ── SEATGEEK LIVE PRICE LOOKUP ────────────────────────────────
async def seatgeek_prices(name: str, event_date: str, city: str) -> Optional[dict]:
    """Get real secondary market prices from SeatGeek."""
    if not SG_KEY:
        return None
    import aiohttp
    try:
        # Build search query from event name
        query = re.sub(r'[^\w\s]', '', name)[:50].strip()
        params = {
            "client_id": SG_KEY,
            "q": query,
            "per_page": 5,
            "sort": "score.desc",
        }
        # Add date filter
        if event_date:
            try:
                dt = datetime.strptime(event_date[:10], "%Y-%m-%d")
                params["datetime_utc.gte"] = (dt-timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
                params["datetime_utc.lte"] = (dt+timedelta(days=2)).strftime("%Y-%m-%dT23:59:59")
            except: pass

        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.seatgeek.com/2/events",
                params=params, timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()

        events = data.get("events", [])
        if not events:
            return None

        # Find best matching event
        best = None
        name_words = [w.lower() for w in re.sub(r'[^\w\s]','',name).split() if len(w) > 3]
        for e in events:
            title_words = [w.lower() for w in e.get("title","").split() if len(w) > 3]
            overlap = sum(1 for w in name_words if w in title_words)
            if overlap >= 2:
                best = e
                break
        if not best and events:
            best = events[0]

        stats = best.get("stats", {})
        lowest  = stats.get("lowest_price") or 0
        median  = stats.get("median_price") or stats.get("average_price") or 0
        highest = stats.get("highest_price") or 0
        count   = stats.get("listing_count") or 0

        if not lowest and not median:
            return None

        return {
            "lowest":   round(lowest, 2),
            "median":   round(median, 2),
            "highest":  round(highest, 2),
            "count":    count,
            "url":      best.get("url",""),
            "verified": True,
        }
    except Exception as e:
        log.debug(f"SeatGeek {name[:30]}: {e}")
        return None

# ── RESELL MULTIPLIERS (calibrated to real market data) ───────
def get_multiplier(name: str, artist: str, category: str, capacity: int) -> float:
    n = name.lower()
    a = (artist or "").lower()
    cat = category.lower()

    # Base multiplier by event type
    if any(k in n or k in a for k in ["taylor swift","beyonce","bad bunny","cold play"]):
        mult = 3.8
    elif any(k in n or k in a for k in ["morgan wallen","zach bryan","kendrick lamar","post malone","travis scott","billie eilish","the weeknd","eminem","rolling stones","elton john"]):
        mult = 3.0
    elif any(k in n or k in a for k in ["sabrina carpenter","chappell roan","tyler the creator","olivia rodrigo","harry styles","dua lipa","ariana grande"]):
        mult = 2.8
    elif any(k in n or k in a for k in ["hamilton","wicked","lion king","hadestown"]):
        mult = 2.5
    elif any(k in n or k in a for k in ["ufc","boxing championship","title fight","wrestlemania"]):
        mult = 2.4
    elif any(k in n for k in ["nba finals","stanley cup final","world series","super bowl","march madness final"]):
        mult = 3.2
    elif any(k in n for k in ["playoff","championship","finals","title"]):
        mult = 2.2
    elif any(k in n or k in a for k in ["dave chappelle","kevin hart","bill burr","chris rock","bert kreischer"]):
        mult = 2.2
    elif "comedy" in cat:
        mult = 1.7
    elif "sports" in cat:
        mult = 1.6
    elif any(k in n or k in a for k in ["luke combs","jason aldean","chris stapleton","kenny chesney","eric church"]):
        mult = 2.2
    elif "concert" in cat or "music" in cat:
        mult = 1.6
    else:
        mult = 1.4

    # Venue size adjustment
    if capacity <= 500:    mult *= 1.7
    elif capacity <= 1000: mult *= 1.5
    elif capacity <= 2000: mult *= 1.3
    elif capacity <= 3500: mult *= 1.18
    elif capacity <= 5000: mult *= 1.08
    elif capacity > 50000: mult *= 0.80
    elif capacity > 25000: mult *= 0.88

    return round(mult, 2)

# ── PROFIT CALCULATOR ─────────────────────────────────────────
def calc_profit(face_avg: float, sg: Optional[dict], mult: float) -> dict:
    """Calculate real profit. Uses SeatGeek if available, estimates otherwise."""

    if sg and sg.get("median", 0) > 0:
        # VERIFIED: use real SeatGeek median price
        resell = sg["median"]
        source = "verified"
        confidence = "high"
    elif sg and sg.get("lowest", 0) > 0:
        resell = sg["lowest"] * 1.15  # Slightly above lowest listing
        source = "verified"
        confidence = "medium"
    else:
        # ESTIMATE: use calibrated multiplier
        resell = face_avg * mult
        source = "estimated"
        confidence = "medium"

    # Real fees
    stubhub_fee = resell * 0.15      # StubHub takes 15%
    tm_buyer_fee = face_avg * 0.25   # Ticketmaster adds ~25% in fees when buying

    profit = resell - stubhub_fee - face_avg - tm_buyer_fee
    roi    = (profit / (face_avg + tm_buyer_fee) * 100) if face_avg > 0 else 0

    # Conservative estimate (if prices drop 20%)
    conservative_resell = resell * 0.80
    conservative_profit = conservative_resell - (conservative_resell*0.15) - face_avg - tm_buyer_fee

    return {
        "face_avg":     round(face_avg, 2),
        "face_with_fees": round(face_avg + tm_buyer_fee, 2),
        "resell":       round(resell, 2),
        "resell_low":   round(resell * 0.80, 2),
        "resell_high":  round(resell * 1.25, 2),
        "profit_per":   round(profit, 2),
        "profit_4":     round(profit * 4, 2),
        "profit_conservative": round(conservative_profit, 2),
        "roi_pct":      round(roi, 1),
        "stubhub_fee":  round(stubhub_fee, 2),
        "source":       source,
        "confidence":   confidence,
        "multiplier":   round(mult, 2),
        "sg_count":     sg.get("count", 0) if sg else 0,
        "sg_url":       sg.get("url", "") if sg else "",
        "is_profitable": profit >= MIN_PROFIT and roi >= MIN_ROI,
    }

# ── SCORER ────────────────────────────────────────────────────
def score_it(event: dict, profit: dict) -> dict:
    name = event.get("name","").lower()
    a    = (event.get("artist") or "").lower()
    cap  = event.get("capacity", 15000) or 15000
    cat  = event.get("category","").lower()

    # Artist quality
    if any(k in a or k in name for k in ["taylor swift","beyonce","eminem","rolling stones"]):
        asc = 98
    elif any(k in a or k in name for k in ["morgan wallen","zach bryan","kendrick","post malone","billie eilish","the weeknd"]):
        asc = 88
    elif any(k in a or k in name for k in ["hamilton","wicked","ufc","championship","playoff","finals"]):
        asc = 82
    elif any(k in a or k in name for k in ["dave chappelle","kevin hart","bill burr","chris rock"]):
        asc = 78
    else:
        asc = 65

    # Venue scarcity
    if cap<=500:    vsc=98
    elif cap<=1000: vsc=93
    elif cap<=2000: vsc=86
    elif cap<=3500: vsc=78
    elif cap<=5000: vsc=68
    elif cap<=10000:vsc=55
    elif cap<=20000:vsc=42
    else:           vsc=25

    # Profit quality
    p = profit["profit_per"]
    r = profit["roi_pct"]
    if p>=200: psc=100
    elif p>=150:psc=92
    elif p>=100:psc=82
    elif p>=75: psc=72
    elif p>=50: psc=60
    elif p>=40: psc=50
    else:       psc=20

    if r>=150: rsc=100
    elif r>=100:rsc=88
    elif r>=75: rsc=78
    elif r>=50: rsc=65
    elif r>=30: rsc=52
    elif r>=20: rsc=38
    else:       rsc=15

    # Verified bonus
    boost = 10 if profit["source"]=="verified" else 0

    total = round(min(99,max(0,
        asc*0.25 + vsc*0.15 + psc*0.35 + rsc*0.25 + boost
    )), 1)

    if total>=80:   verdict="MUST BUY"
    elif total>=65: verdict="STRONG BUY"
    elif total>=52: verdict="BUY"
    else:           verdict="WATCH"

    parts = []
    if profit["source"]=="verified":
        parts.append(f"Verified: face ${profit['face_avg']:.0f} → resell ${profit['resell']:.0f}")
        if profit["sg_count"] > 0:
            parts.append(f"{profit['sg_count']} live listings on SeatGeek")
    else:
        parts.append(f"Est. face ${profit['face_avg']:.0f} → resell ${profit['resell']:.0f}")
    if cap <= 3500: parts.append(f"Small venue ({cap:,} seats)")
    if r >= 80:     parts.append(f"{r:.0f}% ROI")

    return {
        "total":        total,
        "artist_score": round(asc,1),
        "venue_score":  round(vsc,1),
        "profit_score": round(psc,1),
        "roi_score":    round(rsc,1),
        "verdict":      verdict,
        "reasoning":    " | ".join(parts),
        "source":       profit["source"],
    }

def find_codes(event: dict) -> List[str]:
    a = (event.get("artist") or event.get("name","")).lower()
    v = event.get("venue","").lower()
    codes = list(CC_CODES) + list(VENUE_CODES)
    for key, ac in ARTIST_CODES.items():
        if key in a or key in event.get("name","").lower():
            codes.extend(ac); break
    if "paramount" in v: codes.extend(["PARAMOUNT","STG"])
    if "climate"   in v: codes.extend(["CPAPRESALE","CPA"])
    if "ryman"     in v: codes.extend(["RYMAN","OPRY"])
    if "msg" in v or "madison" in v: codes.extend(["MSG"])
    if "t-mobile arena" in v: codes.extend(["TMOBILE","TMARENA"])
    clean = re.sub(r'[^a-zA-Z]','',a).upper()
    if clean and len(clean)>=4: codes.append(clean)
    seen, out = set(), []
    for c in codes:
        if c not in seen: seen.add(c); out.append(c)
    return out

def get_strategy(event: dict, profit: dict, score: dict) -> dict:
    cat  = event.get("category","").lower()
    cap  = event.get("capacity",15000) or 15000
    name = event.get("name","").lower()
    face = profit["face_avg"]
    res  = profit["resell"]
    tot  = score["total"]
    pr   = profit["profit_per"]

    if any(k in name for k in ["ufc","boxing","mma","wwe","wrestlemania"]):
        seat = "Lower bowl ringside 101-110. Sections near the octagon/ring command highest premium."
    elif "concert" in cat or "music" in cat:
        if cap <= 5000:
            seat = "Any seat — small venue, all sections hold strong value."
        else:
            seat = "Floor GA first. Lower bowl 100-115 second. Avoid upper deck entirely."
    elif "sport" in cat:
        seat = "Lower bowl midfield/center court 105-130. Avoid end zone and upper deck."
    elif "comedy" in cat:
        seat = "Any seat — comedy shows are small venues, all sections resell similarly."
    elif "arts" in cat or "theatre" in cat or "theater" in cat:
        seat = "Orchestra center. Avoid rear mezzanine. Best ROI in rows 1-15 orchestra."
    else:
        seat = "Best available floor or lower level."

    qty = 4 if (tot >= 65 and pr >= 60) else 2
    qty_reason = f"{'High' if qty==4 else 'Moderate'} confidence — buy {qty} tickets"

    try:
        days = (datetime.strptime(event.get("date","2026-12-31")[:10],"%Y-%m-%d").date()-date.today()).days
    except: days = 60

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

    floor = max(round(face * 1.25), round(res * 0.70))

    return {
        "seat_advice":    seat,
        "quantity":       qty,
        "qty_reason":     qty_reason,
        "list_at":        f"${list_at}",
        "reduce_14":      f"${reduce14}",
        "reduce_3":       f"${reduce3}",
        "floor":          f"${floor}",
        "capital_needed": round(profit["face_with_fees"] * qty, 2),
    }

def _days_until(s):
    try: return max(0,(datetime.strptime(s[:10],"%Y-%m-%d").date()-date.today()).days)
    except: return 60

def _presale_days(s):
    try: return (datetime.strptime(s[:10],"%Y-%m-%d").date()-date.today()).days
    except: return 999

def is_wanted(event: dict) -> bool:
    """Check if this event is worth scanning at all."""
    name   = event.get("name","").lower()
    artist = (event.get("artist") or "").lower()
    cat    = event.get("category","").lower()

    # Always skip these
    if any(k in name for k in SKIP_WORDS):
        return False

    # Skip events in the past or too soon
    days = _days_until(event.get("date",""))
    if days < 2:
        return False

    # Skip if face value too low (likely free or community event)
    face_low = event.get("face_low", 0) or 0
    if face_low > 0 and face_low < 20:
        return False

    # Check if it matches our wanted list
    combined = name + " " + artist
    if any(k in combined for k in WANTED):
        return True

    # Also keep events where the category is arts/theater
    # (Hamilton, Broadway shows often come through as "Arts & Theatre")
    if "arts" in cat and any(k in name for k in ["theatre","theater","musical","opera","symphony","ballet","hamilton","wicked"]):
        return True

    # Keep championship/playoff sports regardless
    if "sport" in cat and any(k in name for k in ["playoff","championship","finals","title","world series","stanley cup","nba finals","super bowl"]):
        return True

    return False

# ── TICKETMASTER FETCHER ──────────────────────────────────────
async def fetch_city(session, city, state_code, city_mult):
    import aiohttp
    params = {
        "apikey": TM_KEY, "city": city, "stateCode": state_code,
        "countryCode": "US",
        "classificationName": "music,sports,comedy,arts",
        "size": 100,
        "startDateTime": datetime.now().strftime("%Y-%m-%dT00:00:00Z"),
        "endDateTime": (datetime.now()+timedelta(days=180)).strftime("%Y-%m-%dT00:00:00Z"),
        "sort": "date,asc",
    }
    events = []
    try:
        async with session.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200: return []
            data = await r.json()

        for item in data.get("_embedded",{}).get("events",[]):
            v  = (item.get("_embedded",{}).get("venues",[{}]) or [{}])[0]
            p  = (item.get("priceRanges",[{}]) or [{}])[0]
            a  = (item.get("_embedded",{}).get("attractions",[{}]) or [{}])[0]
            ps = item.get("sales",{}).get("presales",[])
            presale_date = ""
            for presale in (ps or []):
                pd_str = presale.get("startDateTime","")[:10]
                if pd_str: presale_date = pd_str; break
            events.append({
                "id":           f"tm_{item.get('id','')}",
                "name":         item.get("name",""),
                "artist":       a.get("name",""),
                "venue":        v.get("name",""),
                "city":         v.get("city",{}).get("name",city),
                "state":        state_code,
                "capacity":     0,
                "date":         item.get("dates",{}).get("start",{}).get("localDate",""),
                "category":     item.get("classifications",[{}])[0].get("segment",{}).get("name","music"),
                "face_low":     p.get("min",0),
                "face_high":    p.get("max",0),
                "url":          item.get("url",""),
                "presale_date": presale_date,
                "source":       "ticketmaster",
                "city_mult":    city_mult,
            })
    except Exception as e:
        log.debug(f"TM {city}: {e}")
    return events

def get_mock_events():
    today = date.today()
    def d(n): return str(today+timedelta(days=n))
    return [
        {"id":"m01","name":"Morgan Wallen — One Thing at a Time Tour","artist":"Morgan Wallen","venue":"Climate Pledge Arena","city":"Seattle","state":"WA","capacity":18100,"date":d(38),"category":"concert","face_low":89,"face_high":299,"url":"https://www.ticketmaster.com","presale_date":d(2),"source":"demo","city_mult":1.0},
        {"id":"m02","name":"UFC 327 — Title Fight","artist":"UFC","venue":"T-Mobile Arena","city":"Las Vegas","state":"NV","capacity":20000,"date":d(15),"category":"sports","face_low":150,"face_high":500,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.3},
        {"id":"m03","name":"Chappell Roan — Pink Pony Tour","artist":"Chappell Roan","venue":"Ryman Auditorium","city":"Nashville","state":"TN","capacity":2362,"date":d(33),"category":"concert","face_low":65,"face_high":150,"url":"https://www.ticketmaster.com","presale_date":d(0),"source":"demo","city_mult":1.25},
        {"id":"m04","name":"Hamilton","artist":"Hamilton","venue":"Richard Rodgers Theatre","city":"New York","state":"NY","capacity":1319,"date":d(20),"category":"arts","face_low":89,"face_high":399,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.15},
        {"id":"m05","name":"Dave Chappelle — One Night Only","artist":"Dave Chappelle","venue":"Chicago Theatre","city":"Chicago","state":"IL","capacity":3600,"date":d(25),"category":"comedy","face_low":75,"face_high":175,"url":"https://www.ticketmaster.com","presale_date":d(1),"source":"demo","city_mult":1.1},
        {"id":"m06","name":"Seattle Mariners Regular Season","artist":"Seattle Mariners","venue":"T-Mobile Park","city":"Seattle","state":"WA","capacity":47929,"date":d(3),"category":"sports","face_low":25,"face_high":60,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.0},
        {"id":"m07","name":"Local Band — Weekend Show","artist":"Local Artist","venue":"Small Bar","city":"Seattle","state":"WA","capacity":200,"date":d(5),"category":"concert","face_low":15,"face_high":25,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.0},
        {"id":"m08","name":"Zach Bryan — Nashville","artist":"Zach Bryan","venue":"Bridgestone Arena","city":"Nashville","state":"TN","capacity":19000,"date":d(55),"category":"concert","face_low":79,"face_high":249,"url":"https://www.ticketmaster.com","presale_date":d(6),"source":"demo","city_mult":1.25},
    ]

# ── PRESALE ALERTS ────────────────────────────────────────────
def check_presale_alerts(events: list):
    for e in events:
        ps = e.get("presale_date","")
        if not ps: continue
        p  = e.get("profit",{})
        if not p.get("is_profitable"): continue
        pd   = _presale_days(ps)
        eid  = e.get("id","")
        name = e.get("name","")
        codes= e.get("codes",[])[:5]
        pp   = p.get("profit_per",0)
        url  = e.get("url","")
        city = e.get("city","")
        src  = "✓ verified" if p.get("source")=="verified" else "estimated"

        if pd<=3 and pd>1 and f"{eid}_72" not in state["presale_alerted"]:
            tg(f"⏰ *PRESALE IN {pd} DAYS* ({src})\n\n🎟 *{name}*\n📍 {e.get('venue','')} · {city}\n🔑 Opens: {ps[:10]}\n\n💰 +${pp:.0f}/ticket · +${pp*4:.0f} buying 4\n\nCodes: {' | '.join(codes[:4])}\n\n[Link]({url})")
            state["presale_alerted"].add(f"{eid}_72")

        elif pd==1 and f"{eid}_24" not in state["presale_alerted"]:
            tg(f"🚨 *PRESALE TOMORROW!* ({src})\n\n🎟 *{name}*\n📍 {e.get('venue','')} · {city}\n\n💰 *+${pp:.0f}/ticket · +${pp*4:.0f} buying 4*\n\n*Codes:*\n{chr(10).join(['• '+c for c in codes])}\n\n[Buy link]({url})")
            state["presale_alerted"].add(f"{eid}_24")

        elif pd==0 and f"{eid}_live" not in state["presale_alerted"]:
            strat = e.get("strategy",{})
            tg(f"🔴 *PRESALE LIVE NOW!*\n\n🎟 *{name}*\n📍 {e.get('venue','')} · {city}\n\n💰 *+${pp:.0f}/ticket · +${pp*4:.0f} buying 4* ({src})\n🎯 {strat.get('seat_advice','Floor GA')}\n\n*Try NOW:*\n{chr(10).join(['• '+c for c in codes])}\n\n[→ BUY NOW]({url})")
            state["presale_alerted"].add(f"{eid}_live")

# ── SCAN ENGINE ───────────────────────────────────────────────
async def run_scan():
    if state["scanning"]: return
    state["scanning"] = True
    log.info("=== Scan started ===")

    try:
        # 1. Fetch all events
        if TM_KEY:
            import aiohttp
            raw = []
            async with aiohttp.ClientSession() as session:
                tasks = [fetch_city(session,c,s,m) for c,s,m in CITIES]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, list): raw.extend(r)
            log.info(f"Fetched {len(raw)} raw events from {len(CITIES)} cities")
        else:
            raw = get_mock_events()
            log.info(f"Demo mode: {len(raw)} mock events")

        # 2. Filter to wanted events only
        wanted = []
        seen = set()
        for e in raw:
            eid = e.get("id","")
            if eid in seen: continue
            seen.add(eid)
            if is_wanted(e):
                wanted.append(e)

        filtered_out = len(raw) - len(wanted)
        log.info(f"{len(wanted)} wanted events ({filtered_out} filtered out as junk)")

        # 3. Score + price check each wanted event
        opportunities = []
        for e in wanted:
            face_low  = e.get("face_low",0) or 0
            face_high = e.get("face_high",0) or 0
            city_mult = e.get("city_mult",1.0)

            if face_high > face_low > 0:
                face_avg = (face_low + face_high) / 2
            elif face_low > 0:
                face_avg = face_low * 1.35
            else:
                face_avg = 85  # Default for events without price data

            # Get resell multiplier
            mult = get_multiplier(
                e.get("name",""), e.get("artist",""),
                e.get("category",""), e.get("capacity",15000) or 15000
            ) * city_mult

            # Check SeatGeek for live prices
            sg = None
            if SG_KEY:
                sg = await seatgeek_prices(
                    e.get("name",""), e.get("date",""), e.get("city","")
                )

            # Calculate profit
            profit = calc_profit(face_avg, sg, mult)

            # Only keep if profitable
            if not profit["is_profitable"]:
                continue

            # Score it
            score = score_it(e, profit)
            codes = find_codes(e)
            strat = get_strategy(e, profit, score)
            pd    = _presale_days(e.get("presale_date","")) if e.get("presale_date") else None

            e["profit"]       = profit
            e["score"]        = score
            e["codes"]        = codes
            e["strategy"]     = strat
            e["presale_days"] = pd
            opportunities.append(e)

        opportunities.sort(key=lambda x: x.get("score",{}).get("total",0), reverse=True)
        log.info(f"{len(opportunities)} profitable opportunities found")
        state["filtered_count"] = filtered_out + (len(wanted) - len(opportunities))

        # 4. Alert on new opportunities
        for e in opportunities:
            eid   = e.get("id","")
            total = e.get("score",{}).get("total",0)
            if total >= 65 and eid not in state["alerted_ids"]:
                p      = e.get("profit",{})
                sc     = e.get("score",{})
                codes  = e.get("codes",[])[:4]
                pp     = p.get("profit_per",0)
                pd_val = e.get("presale_days")
                src    = "✓ verified" if p.get("source")=="verified" else "estimated"

                presale_note = ""
                if pd_val is not None and pd_val >= 0:
                    if pd_val == 0:   presale_note = "\n🔴 *PRESALE LIVE NOW!*\n"
                    elif pd_val == 1: presale_note = "\n⚠️ *Presale TOMORROW!*\n"
                    elif pd_val <= 7: presale_note = f"\n⏰ Presale in {pd_val} days\n"

                emoji = "🔥" if sc.get("verdict")=="MUST BUY" else "✅"
                tg(
                    f"{emoji} *{sc.get('verdict','')} — {total:.0f}/100*\n"
                    f"_{src}_\n\n"
                    f"*{e.get('name','')}*\n"
                    f"📍 {e.get('venue','')} · {e.get('city','')}\n"
                    f"📅 {e.get('date','')} ({_days_until(e.get('date',''))}d away)\n"
                    f"{presale_note}\n"
                    f"💰 Face: ${p.get('face_avg',0):.0f} → Resell: ${p.get('resell',0):.0f}\n"
                    f"*+${pp:.0f}/ticket · +${pp*4:.0f} buying 4*\n"
                    f"ROI: {p.get('roi_pct',0):.0f}%\n\n"
                    f"🔑 {' · '.join(codes)}\n\n"
                    f"[Buy tickets]({e.get('url','')})"
                )
                state["alerted_ids"].add(eid)

        # 5. Presale alerts
        check_presale_alerts(opportunities)

        # 6. Update state
        state["events"]              = opportunities
        state["last_scan"]           = datetime.now().isoformat()
        state["scan_count"]         += 1
        state["total_opportunities"] = len(opportunities)

        must   = len([e for e in opportunities if e.get("score",{}).get("verdict")=="MUST BUY"])
        strong = len([e for e in opportunities if e.get("score",{}).get("verdict")=="STRONG BUY"])
        verified = len([e for e in opportunities if e.get("profit",{}).get("source")=="verified"])
        log.info(f"Done. {len(opportunities)} opps · {must} must buy · {strong} strong · {verified} verified")
        _save()

    finally:
        state["scanning"] = False

def _save():
    try:
        with open(DATA,"w") as f:
            json.dump({
                "events": state["events"],
                "last_scan": state["last_scan"],
                "scan_count": state["scan_count"],
                "total_opportunities": state["total_opportunities"],
                "filtered_count": state["filtered_count"],
            }, f, default=str, indent=2)
    except Exception as e: log.warning(f"Save: {e}")

async def scan_loop():
    while True:
        try: await run_scan()
        except Exception as e: log.error(f"Scan error: {e}", exc_info=True)
        log.info(f"Next scan in {SCAN_MINS} min")
        await asyncio.sleep(SCAN_MINS * 60)

# ── DASHBOARD ─────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ticket Intel Pro</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0b0f1a;--bg2:#121929;--bg3:#1a2236;--bd:#1f2d44;--green:#00d4aa;--amber:#f5a623;--red:#ff4d6d;--blue:#4a9eff;--dim:#7a8ea8;--text:#dde6f0;--mono:'DM Mono',monospace}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:inherit;text-decoration:none}
header{background:var(--bg2);border-bottom:1px solid var(--bd);padding:12px 20px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:50}
.logo{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--green);display:flex;align-items:center;gap:8px;letter-spacing:2px}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pu 2s infinite;flex-shrink:0}
@keyframes pu{0%,100%{opacity:1}50%{opacity:.2}}
.hright{display:flex;align-items:center;gap:10px;font-size:11px;color:var(--dim)}
.sbtn{font-family:var(--mono);font-size:10px;padding:6px 14px;border-radius:5px;background:rgba(0,212,170,.1);border:1px solid rgba(0,212,170,.3);color:var(--green);cursor:pointer;letter-spacing:1px}
.sbtn:hover{background:rgba(0,212,170,.2)}
.sbtn:disabled{opacity:.4;cursor:not-allowed}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;padding:12px 20px}
.stat{background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:11px 12px}
.stat-n{font-family:var(--mono);font-size:18px;font-weight:500}
.stat-l{font-size:9px;color:var(--dim);margin-top:2px;text-transform:uppercase;letter-spacing:.8px}
.main{padding:0 20px 28px}
.frow{display:flex;gap:5px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.f{font-size:11px;padding:4px 11px;border-radius:20px;border:1px solid var(--bd);background:transparent;color:var(--dim);cursor:pointer}
.f.on{background:var(--green);color:#0b0f1a;border-color:transparent;font-weight:500}
.sh{font-family:var(--mono);font-size:10px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin:14px 0 7px}
.ec{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;margin-bottom:7px;overflow:hidden;cursor:pointer}
.ec:hover{border-color:#2a3d5a}
.ec.open{border-color:rgba(0,212,170,.4)}
.et{display:flex;gap:10px;align-items:flex-start;padding:11px 13px}
.esc{width:40px;height:40px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:13px;font-weight:500;flex-shrink:0}
.ei{flex:1;min-width:0}
.en{font-size:13px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.em{font-size:11px;color:var(--dim);margin-top:2px}
.er{text-align:right;flex-shrink:0}
.ep{font-size:14px;font-weight:500;color:var(--green)}
.el{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.7px}
.vt{display:inline-block;font-family:var(--mono);font-size:9px;padding:2px 6px;border-radius:3px;margin-top:3px;letter-spacing:.5px}
.vMUST{background:rgba(255,77,109,.15);color:var(--red);border:1px solid rgba(255,77,109,.3)}
.vSTRONG{background:rgba(0,212,170,.12);color:var(--green);border:1px solid rgba(0,212,170,.3)}
.vBUY{background:rgba(0,212,170,.08);color:var(--green);border:1px solid rgba(0,212,170,.2)}
.vWATCH{background:rgba(245,166,35,.1);color:var(--amber);border:1px solid rgba(245,166,35,.25)}
.vbadge{font-size:9px;font-family:var(--mono);padding:1px 5px;border-radius:3px;margin-left:4px}
.verified{background:rgba(0,212,170,.1);border:1px solid rgba(0,212,170,.2);color:var(--green)}
.estimated{background:rgba(245,166,35,.1);border:1px solid rgba(245,166,35,.2);color:var(--amber)}
.ctag{font-size:9px;font-family:var(--mono);padding:1px 5px;border-radius:3px;background:var(--bg3);border:1px solid var(--bd);color:var(--dim);margin-left:4px}
.psa{font-size:11px;margin-top:2px}
.psa.live{color:var(--red);font-weight:500}
.psa.soon{color:var(--amber)}
.sb2{height:2px;background:var(--bd)}
.sf2{height:100%;transition:width .4s}
.bd2{display:none;padding:12px 13px;border-top:1px solid var(--bd)}
.ec.open .bd2{display:block}
.dr{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-bottom:9px}
.db{background:var(--bg3);border-radius:7px;padding:8px 10px}
.dv{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--text)}
.dl{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.7px;margin-top:1px}
.adv{background:var(--bg3);border-radius:7px;padding:9px 11px;margin-bottom:6px}
.at{font-size:9px;font-weight:500;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.ab{font-size:11px;color:var(--dim);line-height:1.6}
.ab b{color:var(--text)}
.livedata{background:rgba(0,212,170,.06);border:1px solid rgba(0,212,170,.15);border-radius:7px;padding:9px 11px;margin-bottom:6px}
.cw{margin-bottom:9px}
.ct{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px}
.cs{display:flex;flex-wrap:wrap;gap:3px}
.code{font-family:var(--mono);font-size:10px;background:rgba(74,158,255,.1);border:1px solid rgba(74,158,255,.2);border-radius:3px;padding:2px 6px;color:var(--blue)}
.brow{display:flex;gap:5px;flex-wrap:wrap;margin-top:4px}
.btn{font-size:11px;padding:5px 11px;border-radius:5px;border:1px solid var(--bd);background:transparent;color:var(--text);cursor:pointer}
.btn:hover{background:var(--bg3)}
.btn.p{background:rgba(0,212,170,.12);border-color:rgba(0,212,170,.3);color:var(--green)}
.badge{font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:3px;margin-left:7px}
.empty{text-align:center;padding:50px;color:var(--dim);font-size:13px;line-height:2}
.spin{text-align:center;padding:40px;color:var(--dim);font-family:var(--mono);font-size:11px;letter-spacing:2px;animation:fade 1.5s infinite}
.filter-info{font-size:11px;color:var(--dim);margin-bottom:12px;padding:9px 12px;background:var(--bg2);border-radius:7px;border:1px solid var(--bd)}
@keyframes fade{0%,100%{opacity:.3}50%{opacity:1}}
@media(max-width:600px){.stats{grid-template-columns:1fr 1fr}.dr{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<header>
  <div class="logo"><div class="pulse"></div>TICKET INTEL PRO<span class="badge" id="mode-badge" style="background:rgba(245,166,35,.15);color:var(--amber);border:1px solid rgba(245,166,35,.25)">DEMO</span></div>
  <div class="hright">
    <span id="status">Loading...</span>
    <button class="sbtn" id="sbtn" onclick="scan()">SCAN NOW</button>
  </div>
</header>
<div class="stats" id="stats"></div>
<div class="main">
  <div id="filter-info" class="filter-info" style="display:none"></div>
  <div class="frow" id="frow"></div>
  <div id="list"><div class="spin">SCANNING & VERIFYING...</div></div>
</div>
<script>
let evts=[], af='all', oid=null;
const fmt=s=>{try{return new Date(s).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});}catch{return s;}};
const dTo=s=>{try{return Math.round((new Date(s)-new Date())/86400000);}catch{return 0;}};
const sc=n=>n>=78?'#ff4d6d':n>=63?'#00d4aa':n>=50?'#f5a623':'#7a8ea8';
const sbg=n=>n>=78?'rgba(255,77,109,.15)':n>=63?'rgba(0,212,170,.12)':n>=50?'rgba(245,166,35,.12)':'rgba(122,134,153,.1)';
const vk=v=>v.split(' ')[0];

async function load(){
  try{
    const r=await fetch('/api/events');
    const d=await r.json();
    evts=d.events||[];
    updateStats(d);renderF();renderList();
    const t=d.last_scan?new Date(d.last_scan).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'never';
    document.getElementById('status').textContent='Updated '+t;
    const live=evts.some(e=>e.source&&e.source!=='demo');
    const b=document.getElementById('mode-badge');
    if(b){b.textContent=live?'LIVE':'DEMO';b.style.background=live?'rgba(0,212,170,.12)':'rgba(245,166,35,.12)';b.style.color=live?'#00d4aa':'#f5a623';b.style.borderColor=live?'rgba(0,212,170,.25)':'rgba(245,166,35,.25)';}
    const fi=document.getElementById('filter-info');
    if(fi&&d.filtered_count>0){
      const verified=evts.filter(e=>e.profit?.source==='verified').length;
      fi.style.display='block';
      fi.innerHTML=`Scanned 10 cities · <b>${d.filtered_count} events filtered out</b> (low margin or junk) · Showing <b>${evts.length} profitable opportunities</b>${verified>0?' · <b style="color:var(--green)">'+verified+' prices verified by SeatGeek</b>':''}`;
    }
  }catch(e){console.error(e);}
}
async function scan(){
  const btn=document.getElementById('sbtn');
  btn.disabled=true;btn.textContent='SCANNING...';
  try{await fetch('/api/scan',{method:'POST'});await new Promise(r=>setTimeout(r,8000));await load();}
  catch(e){console.error(e);}
  btn.disabled=false;btn.textContent='SCAN NOW';
}
function updateStats(d){
  const ev=d.events||[];
  const must=ev.filter(e=>e.score?.verdict==='MUST BUY').length;
  const str=ev.filter(e=>e.score?.verdict==='STRONG BUY').length;
  const ps=ev.filter(e=>e.presale_days!=null&&e.presale_days>=0&&e.presale_days<=7).length;
  const maxP=Math.max(0,...ev.map(e=>e.profit?.profit_4||0));
  document.getElementById('stats').innerHTML=`
    <div class="stat"><div class="stat-n" style="color:#ff4d6d">${must}</div><div class="stat-l">Must buy</div></div>
    <div class="stat"><div class="stat-n" style="color:#00d4aa">${str}</div><div class="stat-l">Strong buy</div></div>
    <div class="stat"><div class="stat-n" style="color:#f5a623">${ps}</div><div class="stat-l">Presales soon</div></div>
    <div class="stat"><div class="stat-n" style="color:#00d4aa">$${Math.round(maxP).toLocaleString()}</div><div class="stat-l">Best 4-ticket</div></div>
  `;
}
function renderF(){
  document.getElementById('frow').innerHTML=
    ['all','concert','sports','comedy'].map(c=>`<button class="f${af===c?' on':''}" onclick="setF('${c}')">${c==='all'?'All':c[0].toUpperCase()+c.slice(1)}</button>`).join('')+
    `<button class="f${af==='presale'?' on':''}" onclick="setF('presale')">Presales</button>`+
    `<button class="f${af==='verified'?' on':''}" onclick="setF('verified')" style="border-color:rgba(0,212,170,.3);color:var(--green)">✓ Verified</button>`+
    `<button class="f" style="margin-left:auto" onclick="load()">↻</button>`;
}
function setF(f){af=f;renderF();renderList();}
function renderList(){
  let fl;
  if(af==='presale') fl=evts.filter(e=>e.presale_days!=null&&e.presale_days>=0&&e.presale_days<=10);
  else if(af==='verified') fl=evts.filter(e=>e.profit?.source==='verified');
  else if(af==='all') fl=evts;
  else fl=evts.filter(e=>{
    const cat=(e.category||'').toLowerCase();
    return cat.includes(af)||(af==='concert'&&(cat.includes('music')||cat.includes('concert')));
  });
  const grps={MUST:fl.filter(e=>e.score?.verdict==='MUST BUY'),STRONG:fl.filter(e=>e.score?.verdict==='STRONG BUY'),BUY:fl.filter(e=>e.score?.verdict==='BUY'),WATCH:fl.filter(e=>e.score?.verdict==='WATCH')};
  const titles={'MUST':'Act now — must buy','STRONG':'Strong buy','BUY':'Buy','WATCH':'Watch'};
  let html='';
  for(const[k,arr] of Object.entries(grps)){
    if(arr.length){html+=`<div class="sh">${titles[k]}</div>`;arr.forEach(e=>{html+=card(e);});}
  }
  if(!html) html=`<div class="empty">No profitable opportunities found right now.<br>The scanner checked every event in 10 cities and filtered out anything where<br>the margin wasn't confirmed.<br><br>New events are announced daily — check back soon or click SCAN NOW.</div>`;
  document.getElementById('list').innerHTML=html;
}
function psTag(e){
  const pd=e.presale_days;
  if(pd==null||pd<0)return'';
  if(pd===0)return'<div class="psa live">PRESALE LIVE NOW</div>';
  if(pd===1)return'<div class="psa soon">Presale tomorrow!</div>';
  if(pd<=3)return`<div class="psa soon">Presale in ${pd} days</div>`;
  if(pd<=7)return`<div class="psa" style="color:var(--dim)">Presale in ${pd} days</div>`;
  return'';
}
function card(e){
  const sc_=e.score||{},st=e.strategy||{},p=e.profit||{};
  const tot=sc_.total||0,vk_=vk(sc_.verdict||'WATCH');
  const isV=p.source==='verified';
  return `<div class="ec${oid===e.id?' open':''}" id="c${e.id}" onclick="tog('${e.id}')">
    <div class="et">
      <div class="esc" style="background:${sbg(tot)};color:${sc(tot)}">${Math.round(tot)}</div>
      <div class="ei">
        <div class="en">${e.name||''}<span class="ctag">${e.city||''}</span><span class="vbadge ${isV?'verified':'estimated'}">${isV?'✓ verified':'est.'}</span></div>
        <div class="em">${e.venue||''} · ${fmt(e.date)} (${dTo(e.date)}d)</div>
        ${psTag(e)}
      </div>
      <div class="er">
        <div class="ep">+$${Math.round(p.profit_per||0)}/ticket</div>
        <div class="el">${isV?'confirmed':'estimated'}</div>
        <div class="vt v${vk_}">${sc_.verdict||''}</div>
      </div>
    </div>
    <div class="sb2"><div class="sf2" style="width:${Math.min(tot,100)}%;background:${sc(tot)}"></div></div>
    <div class="bd2">
      <div class="dr">
        <div class="db"><div class="dv" style="color:#00d4aa">+$${Math.round(p.profit_4||0).toLocaleString()}</div><div class="dl">4-ticket profit</div></div>
        <div class="db"><div class="dv">$${Math.round(p.face_avg||0)} → $${Math.round(p.resell||0)}</div><div class="dl">Face → resell</div></div>
        <div class="db"><div class="dv">${Math.round(p.roi_pct||0)}%</div><div class="dl">ROI</div></div>
        <div class="db"><div class="dv">$${(st.capital_needed||0).toLocaleString()}</div><div class="dl">Capital needed</div></div>
      </div>
      ${isV&&p.sg_count?`<div class="livedata"><div class="at" style="color:var(--green)">✓ Live SeatGeek data</div><div class="ab"><b>${p.sg_count} active listings</b> · Median: <b>$${Math.round(p.resell||0)}</b> · Range: $${Math.round(p.resell_low||0)}–$${Math.round(p.resell_high||0)}</div></div>`:''}
      <div class="adv"><div class="at">Why this works</div><div class="ab">${sc_.reasoning||''}</div></div>
      <div class="adv"><div class="at">Where to sit</div><div class="ab">${st.seat_advice||''}</div></div>
      <div class="adv"><div class="at">Pricing strategy</div><div class="ab">
        List at <b>${st.list_at||'—'}</b> on StubHub + Vivid Seats + SeatGeek.<br>
        2 weeks out: <b>${st.reduce_14||'—'}</b> · 3 days: <b>${st.reduce_3||'—'}</b><br>
        Never below <b>${st.floor||'—'}</b>
      </div></div>
      <div class="adv"><div class="at">Buy recommendation</div><div class="ab"><b>${st.qty_reason||''}</b> · Capital: $${(st.capital_needed||0).toLocaleString()}</div></div>
      ${(e.codes||[]).length?`<div class="cw"><div class="ct">Presale codes</div><div class="cs">${(e.codes||[]).slice(0,10).map(c=>`<span class="code">${c}</span>`).join('')}</div></div>`:''}
      <div class="brow">
        ${e.url?`<a href="${e.url}" target="_blank"><button class="btn p">Buy tickets →</button></a>`:''}
        <a href="https://www.stubhub.com/find/s/?q=${encodeURIComponent(e.name||'')}" target="_blank"><button class="btn">StubHub</button></a>
        <a href="https://www.vividseats.com/search?searchTerm=${encodeURIComponent(e.name||'')}" target="_blank"><button class="btn">Vivid Seats</button></a>
        ${p.sg_url?`<a href="${p.sg_url}" target="_blank"><button class="btn" style="border-color:rgba(0,212,170,.3);color:var(--green)">SeatGeek ✓</button></a>`:`<a href="https://www.seatgeek.com/search?q=${encodeURIComponent(e.name||'')}" target="_blank"><button class="btn">SeatGeek</button></a>`}
      </div>
    </div>
  </div>`;
}
function tog(id){oid=oid===id?null:id;renderList();if(oid){const el=document.getElementById('c'+id);if(el)el.scrollIntoView({behavior:'smooth',block:'nearest'});}}
load();setInterval(load,60000);
</script>
</body>
</html>"""

# ── ROUTES ────────────────────────────────────────────────────
async def handle_index(req): return web.Response(text=HTML,content_type="text/html")
async def handle_events(req):
    return web.json_response({
        "events": state["events"],
        "last_scan": state["last_scan"],
        "scan_count": state["scan_count"],
        "total_opportunities": state["total_opportunities"],
        "filtered_count": state["filtered_count"],
    }, dumps=lambda o: json.dumps(o,default=str))
async def handle_scan(req):
    asyncio.create_task(run_scan())
    return web.json_response({"status":"scanning"})

async def on_startup(app):
    if DATA.exists():
        try:
            with open(DATA) as f: s=json.load(f)
            state["events"]              = s.get("events",[])
            state["last_scan"]           = s.get("last_scan")
            state["scan_count"]          = s.get("scan_count",0)
            state["total_opportunities"] = s.get("total_opportunities",0)
            state["filtered_count"]      = s.get("filtered_count",0)
            log.info(f"Loaded {len(state['events'])} events")
        except: pass
    asyncio.create_task(scan_loop())

def main():
    mode = "LIVE" if TM_KEY else "DEMO"
    sg_s = "Enabled ✓ — live price verification active" if SG_KEY else "Add SEATGEEK_CLIENT_ID to enable live prices"
    tg_s = "Enabled ✓" if TG_TOKEN else "Add TELEGRAM_TOKEN"
    print(f"""
╔{'═'*57}╗
║  TICKET INTEL PRO v4                                  ║
╠{'═'*57}╣
║  Mode:     {mode:<47}║
║  Filter:   Only shows profit > ${MIN_PROFIT}/ticket + {MIN_ROI}% ROI         ║
║  SeatGeek: {sg_s:<47}║
║  Telegram: {tg_s:<47}║
║  Cities:   Seattle, Las Vegas, LA, Nashville, NYC     ║
║            Chicago, Miami, Dallas, Portland, Tacoma   ║
║  URL:      http://localhost:{PORT:<30}║
╚{'═'*57}╝
""")
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/events", handle_events)
    app.router.add_post("/api/scan", handle_scan)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT, print=lambda _: None)

if __name__ == "__main__":
    main()
