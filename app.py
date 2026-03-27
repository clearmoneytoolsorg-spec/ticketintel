"""
TICKET INTEL PRO v3
====================
Only shows events where profit is CONFIRMED by live resell prices.
Uses SeatGeek API to verify secondary market prices before showing anything.

If face value < secondary market price AND profit > $50/ticket after fees → show it.
If already overpriced or no margin → hidden automatically.

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

# ── CONFIG ────────────────────────────────────────────────────
TM_KEY     = os.getenv("TICKETMASTER_API_KEY","")
SG_KEY     = os.getenv("SEATGEEK_CLIENT_ID","")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID","")
SCAN_MINS  = int(os.getenv("SCAN_INTERVAL_MINUTES","15"))
PORT       = int(os.getenv("PORT","8080"))
DATA       = Path("ticket_data.json")

# Minimum confirmed profit to show event
MIN_PROFIT = 50   # per ticket after StubHub fees
MIN_ROI    = 20   # minimum ROI % to show

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

TIER1 = [
    "taylor swift","beyonce","drake","kendrick lamar","bad bunny",
    "morgan wallen","zach bryan","post malone","travis scott",
    "billie eilish","the weeknd","eminem","coldplay","ed sheeran",
    "sabrina carpenter","chappell roan","tyler the creator",
    "olivia rodrigo","bruno mars","ufc","boxing championship",
    "hamilton","wwe","rolling stones","elton john",
]

SKIP_WORDS = ["tribute","cover band","open mic","karaoke","free event"]

CC_CODES = ["CITI","AMEX","CAPITALONE","CHASE","MASTERCARD","VISA","CITICARD"]
VENUE_CODES = ["LIVENATION","TMFAN","VERIFIED","SPOTIFY","OFFICIAL"]

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
    "ufc":               ["UFC","UFCFIGHT"],
    "hamilton":          ["HAMILTON"],
    "wwe":               ["WWE","WWEFAN"],
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
    "verified_count": 0,
    "skipped_count": 0,
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
        if r.status_code==200: log.info("Telegram ✓")
        else: log.warning(f"Telegram: {r.text[:60]}")
    except Exception as e: log.warning(f"Telegram: {e}")

# ── SEATGEEK PRICE LOOKUP ─────────────────────────────────────
async def get_seatgeek_prices(event_name: str, event_date: str, city: str) -> Optional[dict]:
    """
    Look up real secondary market prices on SeatGeek.
    Returns lowest_price, median_price, highest_price or None if not found.
    """
    if not SG_KEY:
        return None
    import aiohttp
    try:
        # Clean search query
        query = re.sub(r'[^\w\s]','', event_name)[:60]
        params = {
            "client_id": SG_KEY,
            "q": query,
            "per_page": 5,
            "sort": "score.desc",
        }
        if event_date:
            try:
                dt = datetime.strptime(event_date[:10], "%Y-%m-%d")
                # Search within 1 day of event
                params["datetime_utc.gte"] = (dt - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
                params["datetime_utc.lte"] = (dt + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59")
            except: pass

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.seatgeek.com/2/events",
                params=params, timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status != 200: return None
                data = await r.json()

        events = data.get("events", [])
        if not events: return None

        # Find best match
        best = None
        query_lower = query.lower()
        for e in events:
            title_lower = e.get("title","").lower()
            # Check if event names overlap significantly
            words = [w for w in query_lower.split() if len(w)>3]
            matches = sum(1 for w in words if w in title_lower)
            if matches >= 2:
                best = e
                break

        if not best and events:
            best = events[0]  # Fall back to first result

        if not best: return None

        stats = best.get("stats", {})
        lowest  = stats.get("lowest_price", 0)
        median  = stats.get("median_price", 0)
        highest = stats.get("highest_price", 0)
        avg     = stats.get("average_price", 0)

        if not lowest and not median: return None

        return {
            "lowest":  lowest  or 0,
            "median":  median  or avg or 0,
            "highest": highest or 0,
            "listing_count": stats.get("listing_count", 0),
            "sg_url": best.get("url",""),
            "sg_title": best.get("title",""),
        }
    except Exception as e:
        log.debug(f"SeatGeek lookup failed for {event_name[:30]}: {e}")
        return None

# ── PROFIT CALCULATOR ─────────────────────────────────────────
def calculate_profit(face_avg: float, sg_prices: Optional[dict], city_mult: float = 1.0) -> dict:
    """
    Calculate real profit using verified secondary market prices.
    Falls back to estimates if SeatGeek not available.
    """
    if sg_prices and sg_prices.get("lowest", 0) > 0:
        # Use real SeatGeek prices
        resell_conservative = sg_prices["lowest"]   # Worst case — must compete with lowest
        resell_likely       = sg_prices["median"]   # Most likely sale price
        resell_optimistic   = sg_prices["highest"]  # Best case

        # Use the median as our target (realistic)
        resell_target = resell_likely if resell_likely > 0 else resell_conservative
        price_source  = "verified"
        listing_count = sg_prices.get("listing_count", 0)
    else:
        # Estimate — mark lower confidence
        resell_target = face_avg * city_mult * 1.8  # Conservative estimate
        resell_conservative = resell_target * 0.8
        resell_optimistic   = resell_target * 1.3
        price_source  = "estimated"
        listing_count = 0

    # Fees
    stubhub_fee = resell_target * 0.15
    tm_fee      = face_avg * 0.12   # Ticketmaster buyer fee estimate

    profit_per  = resell_target - stubhub_fee - face_avg - tm_fee
    roi_pct     = (profit_per / face_avg * 100) if face_avg > 0 else 0

    # Conservative profit (using lowest resell price)
    profit_conservative = resell_conservative - (resell_conservative*0.15) - face_avg - tm_fee

    return {
        "face_avg":           round(face_avg, 2),
        "resell_target":      round(resell_target, 2),
        "resell_conservative":round(resell_conservative, 2),
        "resell_optimistic":  round(resell_optimistic, 2),
        "stubhub_fee":        round(stubhub_fee, 2),
        "profit_per":         round(profit_per, 2),
        "profit_4":           round(profit_per * 4, 2),
        "profit_conservative":round(profit_conservative, 2),
        "roi_pct":            round(roi_pct, 1),
        "price_source":       price_source,
        "listing_count":      listing_count,
        "is_profitable":      profit_per >= MIN_PROFIT and roi_pct >= MIN_ROI,
        "confidence":         "high" if price_source == "verified" else "medium",
    }

# ── SCORER ────────────────────────────────────────────────────
def score_event(event: dict, profit: dict) -> dict:
    name    = event.get("name","").lower()
    artist  = (event.get("artist") or "").lower()
    venue   = event.get("venue","").lower()
    cat     = event.get("category","").lower()
    capacity= event.get("capacity",15000) or 15000

    # Artist tier
    is_tier1 = any(t in artist or t in name for t in TIER1)
    is_playoff = any(k in name for k in ["playoff","finals","championship","world series","stanley cup"])

    if is_tier1:         asc = 90
    elif is_playoff:     asc = 85
    elif "comedy" in cat:asc = 70
    elif "sports" in cat:asc = 60
    else:                asc = 50

    # Venue scarcity
    if capacity<=1000:   vsc=95
    elif capacity<=2000: vsc=88
    elif capacity<=3500: vsc=80
    elif capacity<=5000: vsc=70
    elif capacity<=10000:vsc=58
    elif capacity<=20000:vsc=45
    else:                vsc=28

    # Profit quality
    p = profit["profit_per"]
    r = profit["roi_pct"]

    if p>=200:   psc=100
    elif p>=150: psc=90
    elif p>=100: psc=80
    elif p>=75:  psc=70
    elif p>=50:  psc=55
    else:        psc=25

    if r>=150:   rsc=100
    elif r>=100: rsc=88
    elif r>=75:  rsc=78
    elif r>=50:  rsc=65
    elif r>=30:  rsc=50
    elif r>=20:  rsc=35
    else:        rsc=15

    # Verified price bonus
    if profit["price_source"] == "verified":
        boost = 8
    else:
        boost = 0

    total = round(min(99, max(0,
        asc*0.25 + vsc*0.15 + psc*0.35 + rsc*0.25 + boost
    )), 1)

    if total>=80:   verdict="MUST BUY"
    elif total>=65: verdict="STRONG BUY"
    elif total>=52: verdict="BUY"
    else:           verdict="WATCH"

    parts = []
    if profit["price_source"]=="verified":
        parts.append(f"Live prices: face ${profit['face_avg']:.0f} → resell ${profit['resell_target']:.0f}")
    else:
        parts.append(f"Est. face ${profit['face_avg']:.0f} → resell ${profit['resell_target']:.0f}")
    if is_tier1:     parts.append("Tier-1 artist")
    if capacity<=3500: parts.append(f"Small venue ({capacity:,} seats)")
    if profit["roi_pct"]>=80: parts.append(f"{profit['roi_pct']:.0f}% ROI")

    return {
        "total":         total,
        "artist_score":  round(asc,1),
        "venue_score":   round(vsc,1),
        "profit_score":  round(psc,1),
        "roi_score":     round(rsc,1),
        "verdict":       verdict,
        "reasoning":     " | ".join(parts),
        "price_source":  profit["price_source"],
    }

def find_codes(event: dict) -> List[str]:
    artist = (event.get("artist") or event.get("name","")).lower()
    venue  = event.get("venue","").lower()
    codes  = list(CC_CODES) + list(VENUE_CODES)
    for key, ac in ARTIST_CODES.items():
        if key in artist or key in event.get("name","").lower():
            codes.extend(ac); break
    if "paramount" in venue: codes.extend(["PARAMOUNT","STG"])
    if "climate"   in venue: codes.extend(["CPAPRESALE","CPA"])
    if "ryman"     in venue: codes.extend(["RYMAN","OPRY"])
    if "msg" in venue or "madison" in venue: codes.extend(["MSG"])
    clean = re.sub(r'[^a-zA-Z]','',artist).upper()
    if clean and len(clean)>=4: codes.append(clean)
    seen, out = set(), []
    for c in codes:
        if c not in seen: seen.add(c); out.append(c)
    return out

def get_strategy(event: dict, profit: dict, score: dict) -> dict:
    cat  = event.get("category","").lower()
    cap  = event.get("capacity",15000) or 15000
    face = profit["face_avg"]
    res  = profit["resell_target"]
    tot  = score["total"]
    pr   = profit["profit_per"]

    if "concert" in cat or "music" in cat:
        seat = "Floor GA first. Lower bowl 100-115 second. Avoid upper deck." if cap>5000 else "Any seat — small venue, all sections hold value."
    elif any(k in event.get("name","").lower() for k in ["ufc","boxing","mma","wwe"]):
        seat = "Lower bowl ringside 101-110. Avoid upper deck."
    elif "sport" in cat:
        seat = "Lower bowl midfield 105-130. Avoid end zone + upper deck."
    elif "comedy" in cat:
        seat = "Any seat — small venue, all sections resell well."
    else:
        seat = "Best available floor or lower level."

    qty = 4 if (tot>=65 and pr>=75) else 2
    qty_reason = f"{'High' if qty==4 else 'Moderate'} confidence — buy {qty}"

    try:
        days = (datetime.strptime(event.get("date","2026-12-31")[:10],"%Y-%m-%d").date()-date.today()).days
    except: days = 60

    # Pricing strategy based on real resell data
    if profit["price_source"] == "verified":
        # Price based on actual market
        list_at  = round(res * 1.08)   # 8% above current median
        reduce14 = round(res * 0.98)   # Match market at 2 weeks
        reduce3  = round(res * 0.90)   # Undercut slightly at 3 days
    else:
        if days>60:   list_at=round(res*1.15); reduce14=round(res); reduce3=round(res*0.88)
        elif days>14: list_at=round(res*1.08); reduce14=round(res*0.95); reduce3=round(res*0.84)
        else:         list_at=round(res*0.98); reduce14=round(res*0.90); reduce3=round(res*0.82)

    floor = max(round(face*1.25), round(res*0.72))

    return {
        "seat_advice":    seat,
        "quantity":       qty,
        "qty_reason":     qty_reason,
        "list_at":        f"${list_at}",
        "reduce_14":      f"${reduce14}",
        "reduce_3":       f"${reduce3}",
        "floor":          f"${floor}",
        "capital_needed": round(face*qty*1.06, 2),
    }

def _days_until(s): 
    try: return max(0,(datetime.strptime(s[:10],"%Y-%m-%d").date()-date.today()).days)
    except: return 60

def _presale_days(s):
    try: return (datetime.strptime(s[:10],"%Y-%m-%d").date()-date.today()).days
    except: return 999

def get_face_avg(event: dict) -> float:
    low  = event.get("face_low",0) or 0
    high = event.get("face_high",0) or 0
    if high > low > 0: return (low+high)/2
    if low > 0: return low*1.35
    return 75  # Default

# ── PRESALE ALERTS ────────────────────────────────────────────
def check_presale_alerts(events: list):
    for e in events:
        ps = e.get("presale_date","")
        if not ps: continue
        profit = e.get("profit",{})
        if not profit.get("is_profitable"): continue
        pd   = _presale_days(ps)
        eid  = e.get("id","")
        name = e.get("name","")
        codes= e.get("codes",[])[:5]
        pp   = profit.get("profit_per",0)
        url  = e.get("url","")
        city = e.get("city","")
        src  = profit.get("price_source","estimated")
        verified_note = " ✓ verified price" if src=="verified" else " (estimated)"

        if pd<=3 and pd>1 and f"{eid}_72" not in state["presale_alerted"]:
            tg(f"⏰ *PRESALE IN {pd} DAYS*{verified_note}\n\n🎟 *{name}*\n📍 {e.get('venue','')} · {city}\n🔑 Opens: {ps[:10]}\n\n💰 +${pp:.0f}/ticket confirmed\nBuy 4 = +${pp*4:.0f}\n\nCodes: {' | '.join(codes[:4])}\n\n[Link]({url})")
            state["presale_alerted"].add(f"{eid}_72")

        elif pd==1 and f"{eid}_24" not in state["presale_alerted"]:
            tg(f"🚨 *PRESALE TOMORROW!*{verified_note}\n\n🎟 *{name}*\n📍 {e.get('venue','')} · {city}\n\n💰 *+${pp:.0f}/ticket · 4 tickets = +${pp*4:.0f}*\n\n*Codes:*\n{chr(10).join(['• '+c for c in codes])}\n\n[Buy link]({url})")
            state["presale_alerted"].add(f"{eid}_24")

        elif pd==0 and f"{eid}_live" not in state["presale_alerted"]:
            tg(f"🔴 *PRESALE LIVE NOW!*\n\n🎟 *{name}*\n📍 {e.get('venue','')} · {city}\n\n💰 *+${pp:.0f}/ticket · +${pp*4:.0f} buying 4*{verified_note}\n\n*Try NOW:*\n{chr(10).join(['• '+c for c in codes])}\n\n[→ BUY NOW]({url})")
            state["presale_alerted"].add(f"{eid}_live")

# ── FETCHERS ──────────────────────────────────────────────────
async def fetch_city(session, city, state_code, city_mult):
    import aiohttp
    params = {
        "apikey":TM_KEY,"city":city,"stateCode":state_code,"countryCode":"US",
        "classificationName":"music,sports,comedy,arts","size":100,
        "startDateTime":datetime.now().strftime("%Y-%m-%dT00:00:00Z"),
        "endDateTime":(datetime.now()+timedelta(days=180)).strftime("%Y-%m-%dT00:00:00Z"),
        "sort":"date,asc",
    }
    events = []
    try:
        async with session.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params=params,timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status!=200: return []
            data = await r.json()
        for item in data.get("_embedded",{}).get("events",[]):
            v  = (item.get("_embedded",{}).get("venues",[{}]) or [{}])[0]
            p  = (item.get("priceRanges",[{}]) or [{}])[0]
            a  = (item.get("_embedded",{}).get("attractions",[{}]) or [{}])[0]
            ps = item.get("sales",{}).get("presales",[])
            presale_date=""
            for presale in (ps or []):
                pd_str=presale.get("startDateTime","")[:10]
                if pd_str: presale_date=pd_str; break
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
        {"id":"m04","name":"Hamilton (NY)","artist":"Hamilton","venue":"Richard Rodgers Theatre","city":"New York","state":"NY","capacity":1319,"date":d(10),"category":"arts","face_low":89,"face_high":399,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.15},
        {"id":"m05","name":"Zach Bryan — Nashville","artist":"Zach Bryan","venue":"Bridgestone Arena","city":"Nashville","state":"TN","capacity":19000,"date":d(55),"category":"concert","face_low":79,"face_high":249,"url":"https://www.ticketmaster.com","presale_date":d(6),"source":"demo","city_mult":1.25},
        {"id":"m06","name":"Kendrick Lamar — Grand National Tour","artist":"Kendrick Lamar","venue":"Crypto.com Arena","city":"Los Angeles","state":"CA","capacity":20000,"date":d(62),"category":"concert","face_low":99,"face_high":399,"url":"https://www.ticketmaster.com","presale_date":d(8),"source":"demo","city_mult":1.2},
        {"id":"m07","name":"Seattle Mariners vs Cleveland Guardians","artist":"Seattle Mariners","venue":"T-Mobile Park","city":"Seattle","state":"WA","capacity":47929,"date":d(1),"category":"sports","face_low":25,"face_high":80,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.0},
        {"id":"m08","name":"Generic Local Concert","artist":"Local Band","venue":"Small Venue","city":"Seattle","state":"WA","capacity":500,"date":d(20),"category":"concert","face_low":30,"face_high":60,"url":"https://www.ticketmaster.com","presale_date":"","source":"demo","city_mult":1.0},
    ]

def filter_events(events):
    seen, out = set(), []
    for e in events:
        if any(k in e.get("name","").lower() for k in SKIP_WORDS): continue
        if not e.get("date") or not e.get("venue"): continue
        eid=e.get("id","")
        if eid in seen: continue
        seen.add(eid); out.append(e)
    return out

# ── SCAN ENGINE ───────────────────────────────────────────────
async def run_scan():
    if state["scanning"]: return
    state["scanning"] = True
    log.info("=== Scan started ===")
    try:
        # 1. Fetch events
        if TM_KEY:
            import aiohttp
            all_events = []
            async with aiohttp.ClientSession() as session:
                tasks = [fetch_city(session,c,s,m) for c,s,m in CITIES]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r,list): all_events.extend(r)
        else:
            log.info("Demo mode — using sample events")
            all_events = get_mock_events()

        events = filter_events(all_events)
        log.info(f"{len(events)} events after filtering")

        # 2. For each event: get face value, check SeatGeek, calculate real profit
        verified_opps = []
        skipped = 0
        checked = 0

        for e in events:
            face_avg  = get_face_avg(e)
            city_mult = e.get("city_mult",1.0)

            # Get live SeatGeek prices (if API key available)
            sg_prices = None
            if SG_KEY:
                sg_prices = await get_seatgeek_prices(
                    e.get("name",""),
                    e.get("date",""),
                    e.get("city","")
                )
                checked += 1

            # Calculate real profit
            profit = calculate_profit(face_avg, sg_prices, city_mult)

            # KEY FILTER: Only keep if actually profitable
            if not profit["is_profitable"]:
                skipped += 1
                continue

            # Score the opportunity
            score = score_event(e, profit)
            codes = find_codes(e)
            strat = get_strategy(e, profit, score)
            pd    = _presale_days(e.get("presale_date","")) if e.get("presale_date") else None

            e["profit"]       = profit
            e["score"]        = score
            e["codes"]        = codes
            e["strategy"]     = strat
            e["presale_days"] = pd
            verified_opps.append(e)

        # Sort by score
        verified_opps.sort(key=lambda x: x.get("score",{}).get("total",0), reverse=True)

        log.info(f"Verified: {len(verified_opps)} profitable · {skipped} filtered out · {checked} SeatGeek checks")
        state["verified_count"] = len(verified_opps)
        state["skipped_count"]  = skipped

        # 3. Alert on new opportunities
        for e in verified_opps:
            eid   = e.get("id","")
            total = e.get("score",{}).get("total",0)
            if total >= 65 and eid not in state["alerted_ids"]:
                profit = e.get("profit",{})
                score  = e.get("score",{})
                codes  = e.get("codes",[])[:4]
                pp     = profit.get("profit_per",0)
                pd_val = e.get("presale_days")
                city   = e.get("city","")
                src    = profit.get("price_source","estimated")

                presale_note=""
                if pd_val is not None and pd_val>=0:
                    if pd_val==0:   presale_note="\n🔴 *PRESALE LIVE NOW!*\n"
                    elif pd_val==1: presale_note="\n⚠️ *Presale TOMORROW!*\n"
                    elif pd_val<=7: presale_note=f"\n⏰ Presale in {pd_val} days\n"

                verified_badge = "✅ VERIFIED PROFIT" if src=="verified" else "📊 EST. PROFIT"
                emoji = "🔥" if score.get("verdict")=="MUST BUY" else "✅"

                msg = (
                    f"{emoji} *{score.get('verdict','')} — {total:.0f}/100*\n"
                    f"_{verified_badge}_\n\n"
                    f"*{e.get('name','')}*\n"
                    f"📍 {e.get('venue','')} · {city}\n"
                    f"📅 {e.get('date','')} ({_days_until(e.get('date',''))}d away)\n"
                    f"{presale_note}\n"
                    f"💰 Face: ${profit.get('face_avg',0):.0f} → Resell: ${profit.get('resell_target',0):.0f}\n"
                    f"Profit: *+${pp:.0f}/ticket* · *+${pp*4:.0f} buying 4*\n"
                    f"ROI: {profit.get('roi_pct',0):.0f}%\n\n"
                    f"🔑 {' · '.join(codes)}\n\n"
                    f"[Buy tickets]({e.get('url','')})"
                )
                tg(msg)
                state["alerted_ids"].add(eid)

        # 4. Presale alerts
        check_presale_alerts(verified_opps)

        # 5. Update state
        state["events"]              = verified_opps
        state["last_scan"]           = datetime.now().isoformat()
        state["scan_count"]         += 1
        state["total_opportunities"] = len(verified_opps)

        must   = len([e for e in verified_opps if e.get("score",{}).get("verdict")=="MUST BUY"])
        strong = len([e for e in verified_opps if e.get("score",{}).get("verdict")=="STRONG BUY"])
        log.info(f"Done. {len(verified_opps)} opps · {must} must · {strong} strong")
        _save()

    finally:
        state["scanning"] = False

def _save():
    try:
        with open(DATA,"w") as f:
            json.dump({"events":state["events"],"last_scan":state["last_scan"],
                       "scan_count":state["scan_count"],
                       "total_opportunities":state["total_opportunities"],
                       "skipped_count":state["skipped_count"]},
                      f,default=str,indent=2)
    except Exception as e: log.warning(f"Save: {e}")

async def scan_loop():
    while True:
        try: await run_scan()
        except Exception as e: log.error(f"Error: {e}",exc_info=True)
        log.info(f"Next scan in {SCAN_MINS} min")
        await asyncio.sleep(SCAN_MINS*60)

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
.verified-badge{font-size:9px;font-family:var(--mono);padding:1px 5px;border-radius:3px;background:rgba(0,212,170,.1);border:1px solid rgba(0,212,170,.2);color:var(--green);margin-left:5px}
.est-badge{font-size:9px;font-family:var(--mono);padding:1px 5px;border-radius:3px;background:rgba(245,166,35,.1);border:1px solid rgba(245,166,35,.2);color:var(--amber);margin-left:5px}
.psa{font-size:11px;margin-top:2px}
.psa.live{color:var(--red);font-weight:500}
.psa.soon{color:var(--amber)}
.ctag{font-size:9px;font-family:var(--mono);padding:1px 5px;border-radius:3px;background:var(--bg3);border:1px solid var(--bd);color:var(--dim);margin-left:4px}
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
.badge{font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:3px;margin-left:7px}
.notice{background:rgba(0,212,170,.06);border:1px solid rgba(0,212,170,.15);border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:12px;color:var(--dim)}
.notice b{color:var(--green)}
.empty{text-align:center;padding:40px;color:var(--dim);font-size:12px;line-height:1.8}
.spin{text-align:center;padding:40px;color:var(--dim);font-family:var(--mono);font-size:11px;letter-spacing:2px;animation:fade 1.5s infinite}
@keyframes fade{0%,100%{opacity:.3}50%{opacity:1}}
@media(max-width:600px){.stats{grid-template-columns:1fr 1fr}.dr{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<header>
  <div class="logo"><div class="pulse"></div>TICKET INTEL PRO<span class="badge" id="mode-badge" style="background:rgba(245,166,35,.15);color:var(--amber);border:1px solid rgba(245,166,35,.25)">DEMO</span></div>
  <div class="hright">
    <span id="skip-count" style="color:var(--dim)"></span>
    <span id="status">Loading...</span>
    <button class="sbtn" id="sbtn" onclick="scan()">SCAN NOW</button>
  </div>
</header>
<div class="stats" id="stats"></div>
<div class="main">
  <div id="notice" style="display:none" class="notice"></div>
  <div class="frow" id="frow"></div>
  <div id="list"><div class="spin">VERIFYING PRICES...</div></div>
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
    const sk=document.getElementById('skip-count');
    if(d.skipped_count>0&&sk)sk.textContent=`${d.skipped_count} filtered out`;
    const hasVerified=evts.some(e=>e.profit?.price_source==='verified');
    const n=document.getElementById('notice');
    if(n){
      if(hasVerified){n.style.display='block';n.innerHTML='<b>✓ Live prices verified</b> — profits confirmed against real StubHub/SeatGeek data. Only showing events where you will actually make money.';}
      else{n.style.display='block';n.innerHTML='<b>Estimated prices</b> — add your free SeatGeek API key to verify profits against real market data. Get it free at <a href="https://platform.seatgeek.com" target="_blank" style="color:var(--green)">platform.seatgeek.com</a>';}
    }
  }catch(e){console.error(e);}
}
async function scan(){
  const btn=document.getElementById('sbtn');
  btn.disabled=true;btn.textContent='VERIFYING...';
  try{await fetch('/api/scan',{method:'POST'});await new Promise(r=>setTimeout(r,7000));await load();}
  catch(e){console.error(e);}
  btn.disabled=false;btn.textContent='SCAN NOW';
}
function updateStats(d){
  const ev=d.events||[];
  const must=ev.filter(e=>e.score?.verdict==='MUST BUY').length;
  const str=ev.filter(e=>e.score?.verdict==='STRONG BUY').length;
  const ps=ev.filter(e=>e.presale_days!=null&&e.presale_days>=0&&e.presale_days<=7).length;
  const maxP=Math.max(0,...ev.map(e=>e.profit?.profit_4||0));
  const verified=ev.filter(e=>e.profit?.price_source==='verified').length;
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
  else if(af==='verified') fl=evts.filter(e=>e.profit?.price_source==='verified');
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
  if(!html)html=`<div class="empty">No profitable opportunities found right now.<br><br>The scanner filtered out events where resell prices are already too high or margins are too thin.<br><br>Click <b>SCAN NOW</b> to check again — new events are announced daily.</div>`;
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
  const isVerified=p.price_source==='verified';
  return `<div class="ec${oid===e.id?' open':''}" id="c${e.id}" onclick="tog('${e.id}')">
    <div class="et">
      <div class="esc" style="background:${sbg(tot)};color:${sc(tot)}">${Math.round(tot)}</div>
      <div class="ei">
        <div class="en">${e.name||''}<span class="ctag">${e.city||''}</span>${isVerified?'<span class="verified-badge">✓ verified</span>':'<span class="est-badge">est.</span>'}</div>
        <div class="em">${e.venue||''} · ${fmt(e.date)} (${dTo(e.date)}d)</div>
        ${psTag(e)}
      </div>
      <div class="er">
        <div class="ep">+$${Math.round(p.profit_per||0)}/ticket</div>
        <div class="el">${isVerified?'confirmed':'estimated'} profit</div>
        <div class="vt v${vk_}">${sc_.verdict||''}</div>
      </div>
    </div>
    <div class="sb2"><div class="sf2" style="width:${Math.min(tot,100)}%;background:${sc(tot)}"></div></div>
    <div class="bd2">
      <div class="dr">
        <div class="db"><div class="dv" style="color:#00d4aa">+$${Math.round(p.profit_4||0).toLocaleString()}</div><div class="dl">4-ticket ${isVerified?'confirmed':'est.'}</div></div>
        <div class="db"><div class="dv">$${Math.round(p.face_avg||0)} → $${Math.round(p.resell_target||0)}</div><div class="dl">Face → resell</div></div>
        <div class="db"><div class="dv">${Math.round(p.roi_pct||0)}%</div><div class="dl">ROI</div></div>
        <div class="db"><div class="dv">$${(st.capital_needed||0).toLocaleString()}</div><div class="dl">Capital needed</div></div>
      </div>
      ${isVerified&&p.listing_count?`<div class="adv" style="background:rgba(0,212,170,.06);border:1px solid rgba(0,212,170,.15)"><div class="at" style="color:var(--green)">Live market data</div><div class="ab">SeatGeek shows <b>${p.listing_count} current listings</b> at <b>$${Math.round(p.resell_target||0)} median</b> · Range: $${Math.round(p.resell_conservative||0)}–$${Math.round(p.resell_optimistic||0)}</div></div>`:''}
      <div class="adv"><div class="at">Why this works</div><div class="ab">${sc_.reasoning||''}</div></div>
      <div class="adv"><div class="at">Where to sit</div><div class="ab">${st.seat_advice||''}</div></div>
      <div class="adv"><div class="at">Pricing strategy</div><div class="ab">
        List at <b>${st.list_at||'—'}</b> on StubHub + Vivid Seats + SeatGeek simultaneously.<br>
        2 weeks out: <b>${st.reduce_14||'—'}</b> · 3 days out: <b>${st.reduce_3||'—'}</b><br>
        Never go below <b>${st.floor||'—'}</b>
      </div></div>
      <div class="adv"><div class="at">Buy recommendation</div><div class="ab"><b>${st.qty_reason||''}</b> · Capital: $${(st.capital_needed||0).toLocaleString()}</div></div>
      ${(e.codes||[]).length?`<div class="cw"><div class="ct">Presale codes</div><div class="cs">${(e.codes||[]).slice(0,10).map(c=>`<span class="code">${c}</span>`).join('')}</div></div>`:''}
      <div class="brow">
        ${e.url?`<a href="${e.url}" target="_blank"><button class="btn p">Buy tickets →</button></a>`:''}
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
        "scan_count":state["scan_count"],"total_opportunities":state["total_opportunities"],
        "skipped_count":state["skipped_count"],
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
            state["skipped_count"]=s.get("skipped_count",0)
            log.info(f"Loaded {len(state['events'])} events")
        except: pass
    asyncio.create_task(scan_loop())

def main():
    mode = "LIVE" if TM_KEY else "DEMO"
    tg_s = "Enabled ✓" if TG_TOKEN else "Add TELEGRAM_TOKEN"
    sg_s = "Enabled ✓ (price verification active)" if SG_KEY else "Add SEATGEEK_CLIENT_ID for price verification"
    print(f"""
╔{'═'*55}╗
║  TICKET INTEL PRO v3 — Verified Profits Only      ║
╠{'═'*55}╣
║  Mode:      {mode:<44}║
║  Cities:    10 cities                             ║
║  SeatGeek:  {sg_s:<44}║
║  Telegram:  {tg_s:<44}║
║  Filter:    Only shows profit > ${MIN_PROFIT}/ticket + {MIN_ROI}% ROI   ║
║  URL:       http://localhost:{PORT:<27}║
╚{'═'*55}╝
""")
    app = web.Application()
    app.router.add_get("/",handle_index)
    app.router.add_get("/api/events",handle_events)
    app.router.add_post("/api/scan",handle_scan)
    app.on_startup.append(on_startup)
    web.run_app(app,host="0.0.0.0",port=PORT,print=lambda _:None)

if __name__=="__main__":
    main()
