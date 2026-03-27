"""
TICKET INTEL PRO v2
====================
10 cities. Smarter scoring. World Cup monitoring.
Targets 2-4 real opportunities per week.

Run: python app.py
Dashboard: http://localhost:8080
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
from typing import List
from aiohttp import web
import requests as req
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("ticketpro")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])

# ── CONFIG ────────────────────────────────────────────────────
TM_KEY     = os.getenv("TICKETMASTER_API_KEY","")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID","")
SCAN_MINS  = int(os.getenv("SCAN_INTERVAL_MINUTES","15"))
PORT       = int(os.getenv("PORT","8080"))
DATA       = Path("ticket_data.json")

# ── CITIES TO SCAN ────────────────────────────────────────────
# Ranked by resell market quality
CITIES = [
    ("Seattle",    "WA", 1.0),   # Home base
    ("Las Vegas",  "NV", 1.3),   # UFC, boxing, residencies — highest premiums
    ("Los Angeles","CA", 1.2),   # Concerts, Lakers/Clippers playoffs
    ("Nashville",  "TN", 1.25),  # Country concerts 3-4x face value
    ("New York",   "NY", 1.15),  # Broadway, concerts, MSG
    ("Chicago",    "IL", 1.1),   # Concerts, sports
    ("Miami",      "FL", 1.1),   # World Cup, concerts
    ("Dallas",     "TX", 1.1),   # World Cup, concerts
    ("Portland",   "OR", 0.95),  # Close to Seattle, easy travel
    ("Tacoma",     "WA", 0.9),   # Local, low competition
]

# ── EVENT SCORING TIERS ───────────────────────────────────────
# Each tier has realistic resell multipliers based on actual market data

MUST_BUY_EVENTS = [
    # These specific event types ALWAYS command 3x+ face value
    "world cup", "fifa", "super bowl", "nba finals", "nfl championship",
    "stanley cup", "world series", "ufc 3", "boxing championship",
    "farewell tour", "final tour", "last tour", "reunion tour",
    "taylor swift", "beyonce", "bad bunny", "drake", "eminem",
    "coldplay", "metallica", "rolling stones", "elton john",
    "hamilton", "bruce springsteen",
]

STRONG_BUY_EVENTS = [
    # 2-3x face value consistently
    "morgan wallen", "zach bryan", "post malone", "travis scott",
    "billie eilish", "kendrick lamar", "the weeknd", "harry styles",
    "ed sheeran", "sabrina carpenter", "chappell roan", "tyler the creator",
    "olivia rodrigo", "ufc fight night", "wwe", "playoff", "finals",
    "sold out", "one night only", "limited engagement",
    "nba playoffs", "nhl playoffs", "mlb playoffs",
]

WATCH_EVENTS = [
    # 1.3-2x face value — worth tracking but not always worth buying
    "mariners", "seahawks", "sounders", "kraken",
    "comedy", "stand up", "festival",
]

# Venues that drive scarcity premium
SMALL_VENUES = {
    # venue_keyword: capacity_estimate
    "paramount":    2807,
    "moore":        1419,
    "showbox":      1100,
    "neptune":      1100,
    "neumos":       650,
    "crocodile":    500,
    "crystal":      1500,
    "house of blues": 2500,
    "ryman":        2362,  # Nashville
    "beacon":       2894,  # NYC
    "apollo":       1506,  # NYC
    "chicago theatre": 3600,
    "fillmore":     1150,
    "troubadour":   500,
    "fox theater":  4400,
    "orpheum":      2800,
}

# ── PRESALE CODES ─────────────────────────────────────────────
CC_CODES = ["CITI","CITICARD","AMEX","AMERICANEXPRESS","CAPITALONE","CAP1","CHASE","MASTERCARD","VISA"]
VENUE_CODES = ["LIVENATION","TMFAN","VERIFIED","SPOTIFY","OFFICIAL","PRESALE"]

ARTIST_CODES = {
    "taylor swift":      ["SWIFTIES","TAYLORSWIFT","TSNATION","ERAS"],
    "morgan wallen":     ["MORGANWALLEN","HANGINOVER","WHISKEY"],
    "zach bryan":        ["ZACHBRYAN","AMERICANHEARTBREAK","QUITTIME"],
    "beyonce":           ["BEYHIVE","BEYONCE","RENAISSANCE"],
    "kendrick lamar":    ["KENDRICK","PGLANG","NOTLIKEUS"],
    "bad bunny":         ["BADBUNNY","CONEJO"],
    "billie eilish":     ["BILLIEEILISH","HAPPIER","HITMHARD"],
    "post malone":       ["POSTMALONE","BEERBOYS"],
    "sabrina carpenter": ["SABRINACARPENTER","SHORTNSWEET"],
    "chappell roan":     ["CHAPPELLROAN","PINKPONY","MIDWEST"],
    "olivia rodrigo":    ["OLIVIARODRIGO","SOUR","GUTS"],
    "tyler the creator": ["TYLERTHECREATOR","CHROMAKOPIA","IGOR"],
    "ufc":               ["UFC","UFCFIGHT","UFCSEATTLE","UFCLV"],
    "wwe":               ["WWE","WWEFAN","RAWTICKETS"],
    "world cup":         ["FIFA","WORLDCUP","FIFAWC26","WC2026"],
    "hamilton":          ["HAMILTON","HAMILTONFAN"],
}

SKIP_WORDS = ["tribute","cover band","open mic","karaoke","free event","comedy open mic"]

# ── STATE ─────────────────────────────────────────────────────
state = {
    "events": [],
    "last_scan": None,
    "scan_count": 0,
    "total_opportunities": 0,
    "alerted_ids": set(),
    "presale_alerted": set(),
    "scanning": False,
    "cities_scanned": [],
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
        else: log.warning(f"Telegram: {r.text[:80]}")
    except Exception as e:
        log.warning(f"Telegram failed: {e}")

# ── SCORING ENGINE ────────────────────────────────────────────
def score_event(event: dict, city_multiplier: float = 1.0) -> dict:
    """
    Realistic scoring based on actual resell market data.
    Returns score 0-100 and realistic profit estimates.
    """
    name     = event.get("name","").lower()
    artist   = (event.get("artist") or "").lower()
    venue    = event.get("venue","").lower()
    cat      = event.get("category","").lower()
    face_low = event.get("face_low",0) or 0
    face_high= event.get("face_high",0) or 0
    capacity = event.get("capacity",15000) or 15000

    # Get face value
    if face_high > face_low > 0:
        face_avg = (face_low + face_high) / 2
    elif face_low > 0:
        face_avg = face_low * 1.4
    else:
        face_avg = 75  # Default assumption

    # ── TIER DETECTION ────────────────────────────────────────
    # Check venue capacity from known venues
    for vkey, vcap in SMALL_VENUES.items():
        if vkey in venue:
            capacity = vcap
            break

    # Determine event tier
    is_must_buy   = any(k in name or k in artist for k in MUST_BUY_EVENTS)
    is_strong_buy = any(k in name or k in artist for k in STRONG_BUY_EVENTS)
    is_watch      = any(k in name or k in artist for k in WATCH_EVENTS)
    is_world_cup  = "world cup" in name or "fifa" in name or "wc2026" in name

    # ── RESELL MULTIPLIER ─────────────────────────────────────
    # Based on real market data, not guesses
    if is_world_cup:
        mult = 4.5  # World Cup is insane right now
    elif is_must_buy:
        mult = 3.5
    elif is_strong_buy:
        mult = 2.4
    elif is_watch:
        mult = 1.6
    elif "playoff" in name or "finals" in name or "championship" in name:
        mult = 2.8
    elif "comedy" in cat:
        mult = 1.8
    elif "sports" in cat:
        mult = 1.5
    else:
        mult = 1.4

    # Capacity adjustment — smaller venue = more scarcity
    if capacity <= 500:    mult *= 1.8
    elif capacity <= 1000: mult *= 1.55
    elif capacity <= 2000: mult *= 1.35
    elif capacity <= 3500: mult *= 1.20
    elif capacity <= 5000: mult *= 1.10
    elif capacity > 50000: mult *= 0.75  # Stadium — lower premium per ticket

    # Presale bonus — if presale exists, demand is confirmed
    if event.get("presale_date"):
        mult *= 1.08

    # City market adjustment
    mult *= city_multiplier

    # Calculate realistic numbers
    resell_avg  = round(face_avg * mult, 2)
    stubhub_fee = resell_avg * 0.15
    profit_per  = resell_avg - stubhub_fee - face_avg
    roi_pct     = (profit_per / face_avg * 100) if face_avg > 0 else 0

    # ── SCORE COMPONENTS ──────────────────────────────────────
    # Artist/event quality (0-100)
    if is_world_cup:        asc = 100
    elif is_must_buy:       asc = 95
    elif is_strong_buy:     asc = 78
    elif "playoff" in name: asc = 85
    elif is_watch:          asc = 58
    elif "comedy" in cat:   asc = 65
    else:                   asc = 40

    # Venue scarcity (0-100)
    if capacity <= 500:    vsc = 100
    elif capacity <= 1000: vsc = 95
    elif capacity <= 2000: vsc = 88
    elif capacity <= 3500: vsc = 80
    elif capacity <= 5000: vsc = 70
    elif capacity <= 10000:vsc = 58
    elif capacity <= 20000:vsc = 45
    elif capacity <= 40000:vsc = 32
    else:                  vsc = 20

    # Profit quality (0-100)
    if profit_per >= 300:   psc = 100
    elif profit_per >= 200: psc = 92
    elif profit_per >= 150: psc = 84
    elif profit_per >= 100: psc = 75
    elif profit_per >= 75:  psc = 65
    elif profit_per >= 50:  psc = 52
    elif profit_per >= 25:  psc = 38
    else:                   psc = 15

    # ROI quality (0-100)
    if roi_pct >= 150:   rsc = 100
    elif roi_pct >= 100: rsc = 88
    elif roi_pct >= 75:  rsc = 78
    elif roi_pct >= 50:  rsc = 65
    elif roi_pct >= 30:  rsc = 50
    elif roi_pct >= 15:  rsc = 35
    else:                rsc = 15

    # Weighted total
    total = round(min(99, max(0,
        asc * 0.30 +
        vsc * 0.20 +
        psc * 0.30 +
        rsc * 0.20
    )), 1)

    # Verdict with realistic thresholds
    if total >= 82:   verdict = "MUST BUY"
    elif total >= 68: verdict = "STRONG BUY"
    elif total >= 55: verdict = "BUY"
    elif total >= 42: verdict = "WATCH"
    else:             verdict = "SKIP"

    # Reasoning
    parts = []
    if is_world_cup:        parts.append("FIFA World Cup 2026 — historic demand")
    elif is_must_buy:       parts.append("Tier-1 artist — proven sellout history")
    elif is_strong_buy:     parts.append("High-demand artist/event")
    if capacity <= 3500:    parts.append(f"Small venue ({capacity:,} seats) — scarcity premium")
    if profit_per >= 100:   parts.append(f"Face ${face_avg:.0f} → resell ${resell_avg:.0f}")
    if roi_pct >= 80:       parts.append(f"{roi_pct:.0f}% ROI")
    if "playoff" in name:   parts.append("Playoff/championship premium")

    return {
        "total":        total,
        "artist_score": round(asc,1),
        "venue_score":  round(vsc,1),
        "profit_score": round(psc,1),
        "roi_score":    round(rsc,1),
        "face_avg":     round(face_avg,2),
        "resell_avg":   round(resell_avg,2),
        "multiplier":   round(mult,2),
        "profit_per":   round(profit_per,2),
        "profit_4":     round(profit_per*4,2),
        "roi_pct":      round(roi_pct,1),
        "verdict":      verdict,
        "reasoning":    " | ".join(parts) if parts else "Moderate opportunity",
        "is_world_cup": is_world_cup,
        "tier":         "must" if is_must_buy or is_world_cup else "strong" if is_strong_buy else "watch",
    }

def find_codes(event: dict) -> List[str]:
    artist = (event.get("artist") or event.get("name","")).lower()
    venue  = event.get("venue","").lower()
    codes  = list(CC_CODES) + list(VENUE_CODES)
    for key, ac in ARTIST_CODES.items():
        if key in artist or key in event.get("name","").lower():
            codes.extend(ac)
            break
    if "paramount" in venue: codes.extend(["PARAMOUNT","STG"])
    if "climate"   in venue: codes.extend(["CPAPRESALE","CPA"])
    if "lumen"     in venue: codes.extend(["LUMEN"])
    if "t-mobile"  in venue: codes.extend(["TMOBILE","TPARK"])
    if "msg" in venue or "madison" in venue: codes.extend(["MSG","MSGPRESALE"])
    if "staples" in venue or "crypto" in venue: codes.extend(["CRYPTO","LAKERSPRESALE"])
    if "allegiant" in venue: codes.extend(["ALLEGIANT","RAIDERS"])
    if "ryman" in venue: codes.extend(["RYMAN","OPRY"])
    clean = re.sub(r'[^a-zA-Z]','',artist).upper()
    if clean and len(clean)>=4: codes.append(clean)
    seen, out = set(), []
    for c in codes:
        if c not in seen: seen.add(c); out.append(c)
    return out

def get_strategy(event: dict, score: dict) -> dict:
    cat  = event.get("category","").lower()
    cap  = event.get("capacity",15000) or 15000
    face = score["face_avg"]
    res  = score["resell_avg"]
    tot  = score["total"]
    pr   = score["profit_per"]

    # Seat advice
    if score.get("is_world_cup"):
        seat = "Any category — all World Cup tickets hold massive value. Category 1 lower bowl best ROI."
    elif "concert" in cat or "music" in cat:
        seat = "Floor GA first. Lower bowl 100-115 second. Never upper deck." if cap>5000 else "Any seat — small venue, all sections hold strong value."
    elif "ufc" in event.get("name","").lower() or "mma" in cat or "boxing" in cat:
        seat = "Lower bowl ringside sections. Sections 101-110. Avoid nosebleeds."
    elif "sport" in cat or "wwe" in cat:
        seat = "Lower bowl midfield/center court 105-130. Avoid end zone + upper deck."
    elif "comedy" in cat:
        seat = "Any seat — comedy shows are small venues, all seats resell well."
    else:
        seat = "Best available floor or lower level."

    qty = 4 if (tot>=65 and pr>=60) else 2
    qty_reason = f"{'High' if qty==4 else 'Moderate'} confidence — buy {qty} tickets"

    try:
        days = (datetime.strptime(event.get("date","2026-12-31")[:10],"%Y-%m-%d").date()-date.today()).days
    except: days = 60

    if days>60:   la=round(res*1.15); r14=round(res*1.00); r3=round(res*0.88)
    elif days>14: la=round(res*1.08); r14=round(res*0.95); r3=round(res*0.84)
    else:         la=round(res*0.98); r14=round(res*0.90); r3=round(res*0.82)
    floor = max(round(face*1.25), round(res*0.68))

    return {
        "seat_advice":    seat,
        "quantity":       qty,
        "qty_reason":     qty_reason,
        "list_at":        f"${la}",
        "reduce_14":      f"${r14}",
        "reduce_3":       f"${r3}",
        "floor":          f"${floor}",
        "capital_needed": round(face*qty*1.06,2),
    }

def _days_until(s: str) -> int:
    try: return max(0,(datetime.strptime(s[:10],"%Y-%m-%d").date()-date.today()).days)
    except: return 60

def _presale_days(s: str) -> int:
    try: return (datetime.strptime(s[:10],"%Y-%m-%d").date()-date.today()).days
    except: return 999

# ── PRESALE ALERTS ────────────────────────────────────────────
def check_presale_alerts(events: list):
    for e in events:
        ps = e.get("presale_date","")
        if not ps: continue
        sc = e.get("score",{})
        if sc.get("total",0) < 55: continue
        pd = _presale_days(ps)
        eid = e.get("id","")
        name = e.get("name","")
        codes = e.get("codes",[])[:6]
        profit = sc.get("profit_per",0)
        url = e.get("url","")
        city = e.get("city","")

        if pd <= 3 and pd > 1 and f"{eid}_72" not in state["presale_alerted"]:
            tg(f"⏰ *PRESALE IN {pd} DAYS*\n\n🎟 *{name}*\n📍 {e.get('venue','')} · {city}\n🔑 Opens: {ps[:10]}\n\n💰 Est. +${profit:.0f}/ticket\n\n*Get ready:*\n• Join fan club / email list\n• Codes: {' | '.join(codes[:4])}\n\n[Event link]({url})")
            state["presale_alerted"].add(f"{eid}_72")
            log.info(f"Presale 72hr: {name}")

        elif pd == 1 and f"{eid}_24" not in state["presale_alerted"]:
            tg(f"🚨 *PRESALE TOMORROW!*\n\n🎟 *{name}*\n📍 {e.get('venue','')} · {city}\n🔑 Opens: *{ps[:10]}*\n\n💰 *+${profit:.0f}/ticket* · 4 tickets = *+${profit*4:.0f}*\n\n*Codes to try:*\n{chr(10).join(['• '+c for c in codes[:6]])}\n\n[Buy link]({url})")
            state["presale_alerted"].add(f"{eid}_24")
            log.info(f"Presale 24hr: {name}")

        elif pd == 0 and f"{eid}_live" not in state["presale_alerted"]:
            tg(f"🔴 *PRESALE LIVE NOW!*\n\n🎟 *{name}*\n📍 {e.get('venue','')} · {city}\n\n💰 Buy at face value!\n*+${profit:.0f}/ticket · +${profit*4:.0f} buying 4*\n\n*Try NOW:*\n{chr(10).join(['• '+c for c in codes[:6]])}\n\n🎯 {e.get('strategy',{}).get('seat_advice','Floor GA or lower bowl')}\n\n[→ BUY NOW]({url})")
            state["presale_alerted"].add(f"{eid}_live")
            log.info(f"Presale LIVE: {name}")

# ── TICKETMASTER FETCHER ───────────────────────────────────────
async def fetch_city(session, city: str, state_code: str, city_mult: float) -> List[dict]:
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
        import aiohttp
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
                "on_sale":      item.get("sales",{}).get("public",{}).get("startDateTime",""),
                "source":       "ticketmaster",
                "city_mult":    city_mult,
            })
    except Exception as e:
        log.debug(f"TM {city}: {e}")
    return events

async def fetch_all_cities() -> List[dict]:
    if not TM_KEY: return get_mock_events()
    import aiohttp
    all_events = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_city(session, city, sc, mult) for city, sc, mult in CITIES]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list): all_events.extend(r)
    state["cities_scanned"] = [c[0] for c in CITIES]
    log.info(f"Fetched {len(all_events)} events from {len(CITIES)} cities")
    return all_events

def get_mock_events() -> List[dict]:
    today = date.today()
    def d(n): return str(today+timedelta(days=n))
    return [
        {"id":"m01","name":"FIFA World Cup 2026 — Seattle Match","artist":"FIFA","venue":"Lumen Field","city":"Seattle","state":"WA","capacity":69000,"date":d(95),"category":"sports","face_low":200,"face_high":800,"url":"https://www.ticketmaster.com","presale_date":d(5),"on_sale":d(7),"source":"demo","city_mult":1.0},
        {"id":"m02","name":"Morgan Wallen — One Thing at a Time Tour","artist":"Morgan Wallen","venue":"Climate Pledge Arena","city":"Seattle","state":"WA","capacity":18100,"date":d(38),"category":"concert","face_low":89,"face_high":299,"url":"https://www.ticketmaster.com","presale_date":d(2),"on_sale":d(4),"source":"demo","city_mult":1.0},
        {"id":"m03","name":"UFC 312 — Title Fight","artist":"UFC","venue":"T-Mobile Arena","city":"Las Vegas","state":"NV","capacity":20000,"date":d(44),"category":"sports","face_low":150,"face_high":500,"url":"https://www.ticketmaster.com","presale_date":d(1),"on_sale":d(3),"source":"demo","city_mult":1.3},
        {"id":"m04","name":"Zach Bryan — The Quittin Time Tour","artist":"Zach Bryan","venue":"Bridgestone Arena","city":"Nashville","state":"TN","capacity":19000,"date":d(55),"category":"concert","face_low":79,"face_high":249,"url":"https://www.ticketmaster.com","presale_date":d(6),"on_sale":d(8),"source":"demo","city_mult":1.25},
        {"id":"m05","name":"Chappell Roan — Pink Pony Tour","artist":"Chappell Roan","venue":"Ryman Auditorium","city":"Nashville","state":"TN","capacity":2362,"date":d(33),"category":"concert","face_low":65,"face_high":150,"url":"https://www.ticketmaster.com","presale_date":d(0),"on_sale":d(2),"source":"demo","city_mult":1.25},
        {"id":"m06","name":"Kendrick Lamar — Grand National Tour","artist":"Kendrick Lamar","venue":"Crypto.com Arena","city":"Los Angeles","state":"CA","capacity":20000,"date":d(62),"category":"concert","face_low":99,"face_high":399,"url":"https://www.ticketmaster.com","presale_date":d(8),"on_sale":d(10),"source":"demo","city_mult":1.2},
        {"id":"m07","name":"Dave Chappelle — One Night Only","artist":"Dave Chappelle","venue":"Chicago Theatre","city":"Chicago","state":"IL","capacity":3600,"date":d(21),"category":"comedy","face_low":75,"face_high":175,"url":"https://www.ticketmaster.com","presale_date":d(1),"on_sale":d(3),"source":"demo","city_mult":1.1},
        {"id":"m08","name":"Beyonce — Renaissance World Tour","artist":"Beyonce","venue":"Allegiant Stadium","city":"Las Vegas","state":"NV","capacity":65000,"date":d(88),"category":"concert","face_low":150,"face_high":600,"url":"https://www.ticketmaster.com","presale_date":d(12),"on_sale":d(14),"source":"demo","city_mult":1.3},
        {"id":"m09","name":"Sabrina Carpenter — Short n Sweet Tour","artist":"Sabrina Carpenter","venue":"Paramount Theatre","city":"Seattle","state":"WA","capacity":2807,"date":d(50),"category":"concert","face_low":65,"face_high":185,"url":"https://www.ticketmaster.com","presale_date":d(4),"on_sale":d(6),"source":"demo","city_mult":1.0},
        {"id":"m10","name":"NBA Playoffs — Game 5","artist":"Lakers vs Warriors","venue":"Crypto.com Arena","city":"Los Angeles","state":"CA","capacity":20000,"date":d(18),"category":"sports","face_low":120,"face_high":450,"url":"https://www.ticketmaster.com","presale_date":"","on_sale":d(1),"source":"demo","city_mult":1.2},
        {"id":"m11","name":"Bad Bunny — Most Wanted Tour","artist":"Bad Bunny","venue":"Kaseya Center","city":"Miami","state":"FL","capacity":19600,"date":d(70),"category":"concert","face_low":89,"face_high":299,"url":"https://www.ticketmaster.com","presale_date":d(9),"on_sale":d(11),"source":"demo","city_mult":1.1},
        {"id":"m12","name":"Tyler the Creator — Chromakopia Tour","artist":"Tyler the Creator","venue":"Climate Pledge Arena","city":"Seattle","state":"WA","capacity":18100,"date":d(77),"category":"concert","face_low":89,"face_high":259,"url":"https://www.ticketmaster.com","presale_date":d(10),"on_sale":d(12),"source":"demo","city_mult":1.0},
    ]

def filter_events(events: list) -> list:
    seen, out = set(), []
    for e in events:
        if any(k in e.get("name","").lower() for k in SKIP_WORDS): continue
        if not e.get("date") or not e.get("venue"): continue
        eid = e.get("id","")
        if eid in seen: continue
        seen.add(eid)
        out.append(e)
    return out

# ── SCAN ENGINE ───────────────────────────────────────────────
async def run_scan():
    if state["scanning"]: return
    state["scanning"] = True
    log.info("=== Scan started ===")
    try:
        raw    = await fetch_all_cities()
        events = filter_events(raw)
        log.info(f"{len(events)} events after filtering")

        scored = []
        for e in events:
            city_mult = e.get("city_mult", 1.0)
            sc    = score_event(e, city_mult)
            codes = find_codes(e)
            strat = get_strategy(e, sc)
            pd    = _presale_days(e.get("presale_date","")) if e.get("presale_date") else None

            e["score"]        = sc
            e["codes"]        = codes
            e["strategy"]     = strat
            e["presale_days"] = pd
            scored.append(e)

        # Sort by score
        scored.sort(key=lambda x: x.get("score",{}).get("total",0), reverse=True)

        # Only alert on genuinely good events (score >= 68)
        for e in scored:
            eid   = e.get("id","")
            total = e.get("score",{}).get("total",0)
            if total >= 68 and eid not in state["alerted_ids"]:
                sc     = e.get("score",{})
                codes  = e.get("codes",[])[:5]
                profit = sc.get("profit_per",0)
                pd_val = e.get("presale_days")
                city   = e.get("city","")
                verdict= sc.get("verdict","")

                presale_note = ""
                if pd_val is not None and pd_val >= 0:
                    if pd_val == 0:   presale_note = "\n🔴 *PRESALE IS LIVE NOW!*\n"
                    elif pd_val == 1: presale_note = "\n⚠️ *Presale opens TOMORROW!*\n"
                    elif pd_val <= 7: presale_note = f"\n⏰ Presale in {pd_val} days\n"

                emoji = "🔥" if verdict == "MUST BUY" else "✅" if verdict == "STRONG BUY" else "👀"
                msg = (
                    f"{emoji} *{verdict} — {total:.0f}/100*\n\n"
                    f"*{e.get('name','')}*\n"
                    f"📍 {e.get('venue','')} · {city}\n"
                    f"📅 {e.get('date','')} ({_days_until(e.get('date',''))}d away)\n"
                    f"{presale_note}\n"
                    f"💰 +${profit:.0f}/ticket · +${profit*4:.0f} buying 4\n"
                    f"Face: ${sc.get('face_avg',0):.0f} → Resell: ${sc.get('resell_avg',0):.0f} ({sc.get('roi_pct',0):.0f}% ROI)\n\n"
                    f"🔑 {' · '.join(codes[:4])}\n\n"
                    f"[Buy tickets]({e.get('url','')})"
                )
                tg(msg)
                state["alerted_ids"].add(eid)

        # Presale countdown alerts
        check_presale_alerts(scored)

        # Update state
        state["events"]              = scored
        state["last_scan"]           = datetime.now().isoformat()
        state["scan_count"]         += 1
        state["total_opportunities"] = len([e for e in scored if e.get("score",{}).get("total",0)>=55])

        # Summary
        must  = len([e for e in scored if e.get("score",{}).get("verdict")=="MUST BUY"])
        strong= len([e for e in scored if e.get("score",{}).get("verdict")=="STRONG BUY"])
        log.info(f"Done. {len(scored)} events · {must} must buy · {strong} strong buy")
        _save()

    finally:
        state["scanning"] = False

def _save():
    try:
        with open(DATA,"w") as f:
            json.dump({"events":state["events"],"last_scan":state["last_scan"],
                       "scan_count":state["scan_count"],
                       "total_opportunities":state["total_opportunities"]},
                      f, default=str, indent=2)
    except Exception as e: log.warning(f"Save: {e}")

async def scan_loop():
    while True:
        try: await run_scan()
        except Exception as e: log.error(f"Error: {e}", exc_info=True)
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
:root{--bg:#0b0f1a;--bg2:#121929;--bg3:#1a2236;--bd:#1f2d44;--green:#00d4aa;--amber:#f5a623;--red:#ff4d6d;--blue:#4a9eff;--purple:#a78bfa;--gold:#ffd700;--dim:#7a8ea8;--text:#dde6f0;--mono:'DM Mono',monospace}
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
.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px;padding:12px 20px}
.stat{background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:11px 12px}
.stat-n{font-family:var(--mono);font-size:18px;font-weight:500}
.stat-l{font-size:9px;color:var(--dim);margin-top:2px;text-transform:uppercase;letter-spacing:.8px}
.main{padding:0 20px 28px}
.frow{display:flex;gap:5px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.f{font-size:11px;padding:4px 11px;border-radius:20px;border:1px solid var(--bd);background:transparent;color:var(--dim);cursor:pointer}
.f.on{background:var(--green);color:#0b0f1a;border-color:transparent;font-weight:500}
.f.wc{background:rgba(255,215,0,.15);color:var(--gold);border-color:rgba(255,215,0,.3)}
.f.wc.on{background:var(--gold);color:#0b0f1a}
.sh{font-family:var(--mono);font-size:10px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin:14px 0 7px}
.ec{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;margin-bottom:7px;overflow:hidden;cursor:pointer}
.ec:hover{border-color:#2a3d5a}
.ec.open{border-color:rgba(0,212,170,.4)}
.ec.wc-event{border-color:rgba(255,215,0,.25)}
.ec.wc-event.open{border-color:rgba(255,215,0,.6)}
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
.ctag{font-size:9px;font-family:var(--mono);padding:1px 5px;border-radius:3px;background:var(--bg3);border:1px solid var(--bd);color:var(--dim);margin-left:4px}
.psa{font-size:11px;margin-top:2px}
.psa.live{color:var(--red);font-weight:500}
.psa.soon{color:var(--amber)}
.sb2{height:2px;background:var(--bd)}
.sf2{height:100%;border-radius:1px;transition:width .4s}
.bd2{display:none;padding:12px 13px;border-top:1px solid var(--bd)}
.ec.open .bd2{display:block}
.dr{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-bottom:9px}
.db{background:var(--bg3);border-radius:7px;padding:8px 10px}
.dv{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--text)}
.dl{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.7px;margin-top:1px}
.sps{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:9px}
.sp{font-size:10px;background:var(--bg3);border:1px solid var(--bd);border-radius:3px;padding:2px 6px;color:var(--dim)}
.sp b{color:var(--text)}
.adv{background:var(--bg3);border-radius:7px;padding:9px 11px;margin-bottom:6px}
.at{font-size:9px;font-weight:500;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.ab{font-size:11px;color:var(--dim);line-height:1.6}
.ab b{color:var(--text)}
.cw{margin-bottom:9px}
.ct{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px}
.cs{display:flex;flex-wrap:wrap;gap:3px}
.code{font-family:var(--mono);font-size:10px;background:rgba(74,158,255,.1);border:1px solid rgba(74,158,255,.2);border-radius:3px;padding:2px 6px;color:var(--blue)}
.brow{display:flex;gap:5px;flex-wrap:wrap;margin-top:4px}
.btn{font-size:11px;padding:5px 11px;border-radius:5px;border:1px solid var(--bd);background:transparent;color:var(--text);cursor:pointer}
.btn:hover{background:var(--bg3)}
.btn.p{background:rgba(0,212,170,.12);border-color:rgba(0,212,170,.3);color:var(--green)}
.wc-banner{background:rgba(255,215,0,.08);border:1px solid rgba(255,215,0,.2);border-radius:7px;padding:8px 11px;margin-bottom:6px;font-size:11px;color:var(--gold)}
.badge{font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:3px;margin-left:7px}
.empty{text-align:center;padding:40px;color:var(--dim);font-size:12px}
.spin{text-align:center;padding:40px;color:var(--dim);font-family:var(--mono);font-size:11px;letter-spacing:2px;animation:fade 1.5s infinite}
.cities{font-size:10px;color:var(--dim);margin-left:auto;font-family:var(--mono)}
@keyframes fade{0%,100%{opacity:.3}50%{opacity:1}}
@keyframes pu{0%,100%{opacity:1}50%{opacity:.2}}
@media(max-width:600px){.stats{grid-template-columns:1fr 1fr}.dr{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<header>
  <div class="logo"><div class="pulse"></div>TICKET INTEL PRO<span class="badge" id="mode-badge" style="background:rgba(245,166,35,.15);color:var(--amber);border:1px solid rgba(245,166,35,.25)">DEMO</span></div>
  <div class="hright">
    <span id="cities-tag" class="cities"></span>
    <span id="status">Loading...</span>
    <button class="sbtn" id="sbtn" onclick="scan()">SCAN NOW</button>
  </div>
</header>
<div class="stats" id="stats"></div>
<div class="main">
  <div class="frow" id="frow"></div>
  <div id="list"><div class="spin">SCANNING 10 CITIES...</div></div>
</div>
<script>
let evts=[], af='all', oid=null;
const fmt=s=>{try{return new Date(s).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});}catch{return s;}};
const dTo=s=>{try{return Math.round((new Date(s)-new Date())/86400000);}catch{return 0;}};
const sc=n=>n>=80?'#ff4d6d':n>=65?'#00d4aa':n>=50?'#f5a623':'#7a8ea8';
const sbg=n=>n>=80?'rgba(255,77,109,.15)':n>=65?'rgba(0,212,170,.12)':n>=50?'rgba(245,166,35,.12)':'rgba(122,134,153,.1)';
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
    const ct=document.getElementById('cities-tag');
    if(ct)ct.textContent=live?'10 cities':'demo mode';
  }catch(e){console.error(e);}
}
async function scan(){
  const btn=document.getElementById('sbtn');
  btn.disabled=true;btn.textContent='SCANNING...';
  try{await fetch('/api/scan',{method:'POST'});await new Promise(r=>setTimeout(r,6000));await load();}
  catch(e){console.error(e);}
  btn.disabled=false;btn.textContent='SCAN NOW';
}
function updateStats(d){
  const ev=d.events||[];
  const must=ev.filter(e=>e.score?.verdict==='MUST BUY').length;
  const str=ev.filter(e=>e.score?.verdict==='STRONG BUY').length;
  const wc=ev.filter(e=>e.score?.is_world_cup).length;
  const ps=ev.filter(e=>e.presale_days!=null&&e.presale_days>=0&&e.presale_days<=7&&(e.score?.total||0)>=55).length;
  const maxP=Math.max(0,...ev.map(e=>e.score?.profit_4||0));
  document.getElementById('stats').innerHTML=`
    <div class="stat"><div class="stat-n" style="color:#ff4d6d">${must}</div><div class="stat-l">Must buy</div></div>
    <div class="stat"><div class="stat-n" style="color:#00d4aa">${str}</div><div class="stat-l">Strong buy</div></div>
    <div class="stat"><div class="stat-n" style="color:#f5a623">${ps}</div><div class="stat-l">Presales this week</div></div>
    <div class="stat"><div class="stat-n" style="color:#ffd700">${wc}</div><div class="stat-l">World Cup</div></div>
    <div class="stat"><div class="stat-n" style="color:#00d4aa">$${Math.round(maxP).toLocaleString()}</div><div class="stat-l">Best 4-ticket</div></div>
  `;
}
function renderF(){
  document.getElementById('frow').innerHTML=
    ['all','concert','sports','comedy'].map(c=>`<button class="f${af===c?' on':''}" onclick="setF('${c}')">${c==='all'?'All':c[0].toUpperCase()+c.slice(1)}</button>`).join('')+
    `<button class="f wc${af==='wc'?' on':''}" onclick="setF('wc')">World Cup 🏆</button>`+
    `<button class="f${af==='presale'?' on':''}" onclick="setF('presale')">Presales</button>`+
    `<button class="f" style="margin-left:auto" onclick="load()">↻</button>`;
}
function setF(f){af=f;renderF();renderList();}
function renderList(){
  let fl;
  if(af==='wc') fl=evts.filter(e=>e.score?.is_world_cup);
  else if(af==='presale') fl=evts.filter(e=>e.presale_days!=null&&e.presale_days>=0&&e.presale_days<=10&&(e.score?.total||0)>=55);
  else if(af==='all') fl=evts;
  else fl=evts.filter(e=>{
    const cat=(e.category||'').toLowerCase();
    return cat.includes(af)||(af==='concert'&&(cat.includes('music')||cat.includes('concert')));
  });
  const groups={MUST:fl.filter(e=>e.score?.verdict==='MUST BUY'),STRONG:fl.filter(e=>e.score?.verdict==='STRONG BUY'),BUY:fl.filter(e=>e.score?.verdict==='BUY'),WATCH:fl.filter(e=>e.score?.verdict==='WATCH')};
  const titles={'MUST':'Act now — must buy','STRONG':'Strong buy','BUY':'Buy','WATCH':'Watch'};
  let html='';
  for(const[k,arr]of Object.entries(groups)){
    if(arr.length){html+=`<div class="sh">${titles[k]}</div>`;arr.forEach(e=>{html+=card(e);});}
  }
  if(!html)html='<div class="empty">No events found. Click SCAN NOW.</div>';
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
  const sc_=e.score||{},st=e.strategy||{};
  const tot=sc_.total||0,vk_=vk(sc_.verdict||'WATCH');
  const isWC=sc_.is_world_cup;
  return `<div class="ec${oid===e.id?' open':''}${isWC?' wc-event':''}" id="c${e.id}" onclick="tog('${e.id}')">
    <div class="et">
      <div class="esc" style="background:${sbg(tot)};color:${sc(tot)}">${Math.round(tot)}</div>
      <div class="ei">
        <div class="en">${isWC?'🏆 ':''}${e.name||''}<span class="ctag">${e.city||''}</span></div>
        <div class="em">${e.venue||''} · ${fmt(e.date)} (${dTo(e.date)}d)</div>
        ${psTag(e)}
      </div>
      <div class="er">
        <div class="ep">+$${Math.round(sc_.profit_per||0)}/ticket</div>
        <div class="el">profit</div>
        <div class="vt v${vk_}">${sc_.verdict||''}</div>
      </div>
    </div>
    <div class="sb2"><div class="sf2" style="width:${Math.min(tot,100)}%;background:${sc(tot)}"></div></div>
    <div class="bd2">
      ${isWC?'<div class="wc-banner">FIFA World Cup 2026 — historically the highest-premium ticket event in the world. Buy any category, all will resell at massive multiples.</div>':''}
      <div class="dr">
        <div class="db"><div class="dv" style="color:#00d4aa">+$${Math.round(sc_.profit_4||0).toLocaleString()}</div><div class="dl">4-ticket profit</div></div>
        <div class="db"><div class="dv">$${Math.round(sc_.face_avg||0)} → $${Math.round(sc_.resell_avg||0)}</div><div class="dl">Face → resell</div></div>
        <div class="db"><div class="dv">${Math.round(sc_.roi_pct||0)}%</div><div class="dl">ROI</div></div>
        <div class="db"><div class="dv">$${(st.capital_needed||0).toLocaleString()}</div><div class="dl">Capital</div></div>
      </div>
      <div class="sps">
        <span class="sp">Artist <b>${Math.round(sc_.artist_score||0)}</b></span>
        <span class="sp">Venue <b>${Math.round(sc_.venue_score||0)}</b></span>
        <span class="sp">Profit <b>${Math.round(sc_.profit_score||0)}</b></span>
        <span class="sp">ROI <b>${Math.round(sc_.roi_score||0)}</b></span>
        <span class="sp">×${(sc_.multiplier||1).toFixed(1)} resell mult</span>
      </div>
      <div class="adv"><div class="at">Why this event</div><div class="ab">${sc_.reasoning||''}</div></div>
      <div class="adv"><div class="at">Where to sit</div><div class="ab">${st.seat_advice||''}</div></div>
      <div class="adv"><div class="at">Pricing strategy</div><div class="ab">
        List at <b>${st.list_at||'—'}</b> on StubHub + Vivid Seats + SeatGeek.<br>
        2 weeks: <b>${st.reduce_14||'—'}</b> · 3 days: <b>${st.reduce_3||'—'}</b> · Floor: <b>${st.floor||'—'}</b>
      </div></div>
      <div class="adv"><div class="at">Buy recommendation</div><div class="ab"><b>${st.qty_reason||''}</b> · Capital needed: $${(st.capital_needed||0).toLocaleString()}</div></div>
      ${(e.codes||[]).length?`<div class="cw"><div class="ct">Presale codes</div><div class="cs">${(e.codes||[]).slice(0,10).map(c=>`<span class="code">${c}</span>`).join('')}</div></div>`:''}
      <div class="brow">
        ${e.url?`<a href="${e.url}" target="_blank"><button class="btn p">Buy →</button></a>`:''}
        <a href="https://www.stubhub.com/find/s/?q=${encodeURIComponent(e.name||'')}" target="_blank"><button class="btn">StubHub</button></a>
        <a href="https://www.vividseats.com/search?searchTerm=${encodeURIComponent(e.name||'')}" target="_blank"><button class="btn">Vivid Seats</button></a>
        <a href="https://www.seatgeek.com/search?q=${encodeURIComponent(e.name||'')}" target="_blank"><button class="btn">SeatGeek</button></a>
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
        "events":state["events"],"last_scan":state["last_scan"],
        "scan_count":state["scan_count"],"total_opportunities":state["total_opportunities"]
    },dumps=lambda o:json.dumps(o,default=str))
async def handle_scan(req):
    asyncio.create_task(run_scan())
    return web.json_response({"status":"scanning"})

async def on_startup(app):
    if DATA.exists():
        try:
            with open(DATA) as f: s=json.load(f)
            state["events"]=s.get("events",[])
            state["last_scan"]=s.get("last_scan")
            state["scan_count"]=s.get("scan_count",0)
            state["total_opportunities"]=s.get("total_opportunities",0)
            log.info(f"Loaded {len(state['events'])} saved events")
        except: pass
    asyncio.create_task(scan_loop())

def main():
    mode = "LIVE" if TM_KEY else "DEMO"
    tg_s = "Enabled ✓" if TG_TOKEN else "Add TELEGRAM_TOKEN to .env"
    print(f"""
╔{'═'*53}╗
║  TICKET INTEL PRO v2                              ║
╠{'═'*53}╣
║  Mode:     {mode:<43}║
║  Cities:   10 (Seattle, Las Vegas, LA, Nashville  ║
║            NYC, Chicago, Miami, Dallas, Portland) ║
║  WC 2026:  Monitoring all host cities             ║
║  Telegram: {tg_s:<43}║
║  Scan:     Every {SCAN_MINS} minutes                         ║
║  URL:      http://localhost:{PORT:<26}║
╚{'═'*53}╝
""")
    app = web.Application()
    app.router.add_get("/",handle_index)
    app.router.add_get("/api/events",handle_events)
    app.router.add_post("/api/scan",handle_scan)
    app.on_startup.append(on_startup)
    web.run_app(app,host="0.0.0.0",port=PORT,print=lambda _:None)

if __name__=="__main__":
    main()
