"""
TICKET INTEL PRO
=================
Complete ticket resell intelligence system.
- Real events from Ticketmaster API
- Presale countdown alerts (72hr, 24hr, LIVE NOW)
- Claude AI analysis on every opportunity
- Fixed Telegram alerts using requests library
- Live web dashboard
- Runs 24/7 on Railway (free)

Run: python app.py
Dashboard: http://localhost:8080
"""

import sys
import subprocess

def install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q",
                          "--disable-pip-version-check"])

# Auto-install dependencies
for pkg in ["aiohttp", "requests", "python-dotenv", "anthropic"]:
    try:
        __import__(pkg.replace("-","_"))
    except ImportError:
        print(f"Installing {pkg}...")
        install(pkg)

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
TM_KEY        = os.getenv("TICKETMASTER_API_KEY","")
TG_TOKEN      = os.getenv("TELEGRAM_TOKEN","")
TG_CHAT       = os.getenv("TELEGRAM_CHAT_ID","")
CLAUDE_KEY    = os.getenv("ANTHROPIC_API_KEY","")
SCAN_MINS     = int(os.getenv("SCAN_INTERVAL_MINUTES","15"))
PORT          = int(os.getenv("PORT","8080"))
DATA          = Path("ticket_data.json")

TIER1 = ["taylor swift","beyonce","drake","kendrick lamar","bad bunny",
          "morgan wallen","zach bryan","post malone","travis scott",
          "billie eilish","the weeknd","eminem","coldplay","ed sheeran",
          "sabrina carpenter","chappell roan","tyler the creator",
          "olivia rodrigo","bruno mars","ufc","mma","boxing","wwe",
          "seahawks","sounders","storm","kraken","mariners"]

SKIP = ["tribute","cover band","open mic","karaoke"]

CC_CODES = ["CITI","CITICARD","AMEX","AMERICANEXPRESS",
            "CAPITALONE","CAP1","CHASE","MASTERCARD","VISA"]
VENUE_CODES = ["LIVENATION","TMFAN","VERIFIED","SPOTIFY","OFFICIAL"]

ARTIST_CODES = {
    "taylor swift":      ["SWIFTIES","TAYLORSWIFT","TSNATION"],
    "morgan wallen":     ["MORGANWALLEN","HANGINOVER"],
    "zach bryan":        ["ZACHBRYAN","AMERICANHEARTBREAK"],
    "beyonce":           ["BEYHIVE","BEYONCE"],
    "kendrick lamar":    ["KENDRICK","PGLANG"],
    "bad bunny":         ["BADBUNNY","CONEJO"],
    "billie eilish":     ["BILLIEEILISH","HAPPIER"],
    "post malone":       ["POSTMALONE","BEERBOYS"],
    "sabrina carpenter": ["SABRINACARPENTER","SHORTNSWEET"],
    "chappell roan":     ["CHAPPELLROAN","PINKPONY"],
    "ufc":               ["UFC","UFCSEATTLE","UFCFIGHT"],
    "seahawks":          ["SEAHAWKS","GOHAWKS"],
    "sounders":          ["SOUNDERS","RAVE GREEN"],
    "kraken":            ["KRAKEN","SEAKRAKEN"],
    "mariners":          ["MARINERS","MARINERSFAN"],
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
}

# ── TELEGRAM (using requests — no SSL issues) ─────────────────
def tg(text: str):
    """Send Telegram message — simple, reliable, no SSL issues."""
    if not TG_TOKEN or not TG_CHAT:
        log.info(f"[ALERT] {text[:100]}")
        return
    try:
        r = req.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text,
                  "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=10
        )
        if r.status_code == 200:
            log.info("Telegram sent ✓")
        else:
            log.warning(f"Telegram error: {r.text[:100]}")
    except Exception as e:
        log.warning(f"Telegram failed: {e}")

# ── CLAUDE AI ANALYSIS ────────────────────────────────────────
def claude_analyze(event: dict, score: dict) -> str:
    """Use Claude to write a smart analysis of the opportunity."""
    if not CLAUDE_KEY:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=CLAUDE_KEY)
        prompt = f"""You are a ticket resell expert. Analyze this opportunity in 3-4 sentences max. Be direct and practical.

Event: {event.get('name')}
Venue: {event.get('venue')} ({event.get('capacity', 'unknown')} capacity)
Date: {event.get('date')} ({_days_until(event.get('date',''))} days away)
Face value: ${score.get('face_avg',0):.0f}
Estimated resell: ${score.get('resell_avg',0):.0f}
Profit per ticket: ${score.get('profit_per',0):.0f}
Score: {score.get('total',0):.0f}/100

Write a 3-sentence analysis:
1. Why this event will sell out and command a premium
2. Best seats to target and why
3. When to list and at what price strategy

Be specific. No fluff."""

        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        log.debug(f"Claude analysis error: {e}")
        return ""

# ── SCORING ───────────────────────────────────────────────────
def estimate_resell(event: dict) -> dict:
    face_low  = event.get("face_low", 0) or 50
    face_high = event.get("face_high", 0) or 150
    face_avg  = (face_low + face_high) / 2 if face_high > face_low else face_low * 1.5
    name      = event.get("name", "").lower()
    artist    = (event.get("artist") or "").lower()
    capacity  = event.get("capacity", 10000) or 10000

    mult = 1.6
    if any(t in artist or t in name for t in TIER1):
        mult = 3.0
    elif any(k in name for k in ["playoff","finals","championship","world series","super bowl"]):
        mult = 2.8
    elif "farewell" in name or "one night" in name or "only" in name:
        mult = 3.0
    elif event.get("category","").lower() == "comedy":
        mult = 2.2
    elif event.get("category","").lower() == "sports":
        mult = 2.0

    if capacity <= 1000:    mult *= 1.65
    elif capacity <= 2000:  mult *= 1.45
    elif capacity <= 3500:  mult *= 1.28
    elif capacity <= 5000:  mult *= 1.15
    elif capacity > 50000:  mult *= 0.82

    resell = round(face_avg * mult, 2)
    return {"face_avg": round(face_avg,2), "resell_avg": resell,
            "resell_low": round(resell*0.72,2), "resell_high": round(resell*1.5,2),
            "multiplier": round(mult,2)}

def score_event(event: dict, resell: dict) -> dict:
    face    = resell["face_avg"]
    res     = resell["resell_avg"]
    cap     = event.get("capacity", 10000) or 10000
    name    = event.get("name","").lower()
    artist  = (event.get("artist") or "").lower()
    cat     = event.get("category","").lower()

    # Artist score
    if any(t in artist or t in name for t in TIER1): asc = 95
    elif any(k in name for k in ["playoff","finals","championship","farewell"]): asc = 85
    elif cat == "comedy": asc = 74
    elif any(k in name for k in ["tour","arena","stadium"]): asc = 68
    else: asc = 44

    # Venue score
    if cap<=1000: vsc=98
    elif cap<=2000: vsc=93
    elif cap<=3500: vsc=87
    elif cap<=5000: vsc=80
    elif cap<=10000: vsc=67
    elif cap<=20000: vsc=52
    else: vsc=34

    # Price gap
    gap = ((res-face)/face*100) if face>0 else 0
    if gap>=200: gsc=100
    elif gap>=150: gsc=93
    elif gap>=100: gsc=84
    elif gap>=75: gsc=74
    elif gap>=50: gsc=63
    elif gap>=30: gsc=49
    elif gap>=10: gsc=36
    else: gsc=15

    # Demand
    dem = 50
    if any(t in artist or t in name for t in TIER1): dem += 28
    if resell.get("multiplier",1) >= 3.0: dem += 16
    elif resell.get("multiplier",1) >= 2.0: dem += 8
    if any(k in name for k in ["sold out","one night","farewell","final"]): dem += 14
    if event.get("presale_date"): dem += 6
    dem = min(100, dem)

    total = round(min(99, max(0,
        dem*0.35 + asc*0.25 + vsc*0.20 + gsc*0.20)), 1)

    fee    = res * 0.15
    profit = res - fee - face
    roi    = (profit/face*100) if face>0 else 0

    if total>=88: verdict="MUST BUY"
    elif total>=75: verdict="STRONG BUY"
    elif total>=65: verdict="BUY"
    elif total>=50: verdict="WATCH"
    else: verdict="SKIP"

    parts = []
    if asc>=88: parts.append("Tier-1 artist/team")
    if vsc>=80: parts.append(f"Small venue ({event.get('capacity','?')} seats)")
    if gsc>=70: parts.append(f"Face ${face:.0f} → resell ${res:.0f}")
    if profit>=80: parts.append(f"+${profit:.0f}/ticket est.")

    return {"total":total,"demand":round(dem,1),"artist_score":round(asc,1),
            "venue_score":round(vsc,1),"price_gap":round(gsc,1),
            "face_avg":round(face,2),"resell_avg":round(res,2),
            "profit_per":round(profit,2),"profit_4":round(profit*4,2),
            "roi_pct":round(roi,1),"verdict":verdict,
            "reasoning":" | ".join(parts) if parts else "Moderate opportunity"}

def find_codes(event: dict) -> List[str]:
    artist = (event.get("artist") or event.get("name","")).lower()
    venue  = event.get("venue","").lower()
    codes  = list(CC_CODES) + list(VENUE_CODES)
    for key, ac in ARTIST_CODES.items():
        if key in artist:
            codes.extend(ac)
            break
    if "paramount" in venue: codes.extend(["PARAMOUNT","STG"])
    if "climate"   in venue: codes.extend(["CPAPRESALE","CPA"])
    if "lumen"     in venue: codes.extend(["LUMEN","LUMENPRESALE"])
    if "t-mobile"  in venue: codes.extend(["TMOBILE","TPARK"])
    clean = re.sub(r'[^a-zA-Z]','',artist).upper()
    if clean and len(clean)>=4: codes.append(clean)
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

def get_strategy(event: dict, score: dict) -> dict:
    cat  = event.get("category","concert").lower()
    cap  = event.get("capacity",10000) or 10000
    face = score["face_avg"]
    res  = score["resell_avg"]
    tot  = score["total"]
    pr   = score["profit_per"]

    if "concert" in cat or "music" in cat:
        seat = "Floor GA first. Lower bowl 100-115 second. Never upper deck." if cap>5000 else "Any seat — small venue, all sections hold value."
    elif "sport" in cat or "ufc" in cat or "mma" in cat or "wwe" in cat:
        seat = "Lower bowl midfield/center court sections 105-130. Avoid end zone + upper deck."
    elif "comedy" in cat:
        seat = "Any seat — comedy shows are small venues, all seats resell well."
    else:
        seat = "Best available floor or lower level."

    qty = 4 if (tot>=70 and pr>=75) else 2
    qty_reason = f"{'High' if qty==4 else 'Moderate'} confidence — buy {qty} tickets"

    days = _days_until(event.get("date",""))
    if days>60:   la=round(res*1.15); r14=round(res); r3=round(res*0.90)
    elif days>14: la=round(res*1.08); r14=round(res*0.95); r3=round(res*0.85)
    else:         la=round(res*0.98); r14=round(res*0.92); r3=round(res*0.83)
    floor = max(round(face*1.20), round(res*0.70))

    return {"seat_advice":seat,"quantity":qty,"qty_reason":qty_reason,
            "list_at":f"${la}","reduce_14":f"${r14}","reduce_3":f"${r3}",
            "floor":f"${floor}","capital_needed":round(face*qty*1.05,2)}

def _days_until(date_str: str) -> int:
    try:
        return max(0,(datetime.strptime(date_str[:10],"%Y-%m-%d").date()-date.today()).days)
    except: return 60

def _presale_days(ps: str) -> int:
    try:
        return (datetime.strptime(ps[:10],"%Y-%m-%d").date()-date.today()).days
    except: return 999

# ── PRESALE ALERTS ────────────────────────────────────────────
def check_presale_alerts(events: list):
    """Fire alerts at 72hr, 24hr, and when presale goes live."""
    for e in events:
        ps = e.get("presale_date","")
        if not ps: continue
        sc = e.get("score",{})
        if sc.get("total",0) < 65: continue

        pd = _presale_days(ps)
        eid = e.get("id","")
        name = e.get("name","")
        codes = e.get("codes",[])[:5]
        profit = sc.get("profit_per",0)
        url = e.get("url","")

        alert_key_72 = f"{eid}_72"
        alert_key_24 = f"{eid}_24"
        alert_key_live = f"{eid}_live"

        if pd <= 3 and pd > 1 and alert_key_72 not in state["presale_alerted"]:
            tg(f"⏰ *PRESALE IN {pd} DAYS*\n\n"
               f"🎟 *{name}*\n"
               f"📍 {e.get('venue','')} · {e.get('city','')}\n"
               f"📅 Event: {e.get('date','')}\n"
               f"🔑 Presale opens: {ps[:10]}\n\n"
               f"💰 Est. profit: +${profit:.0f}/ticket\n\n"
               f"*Action now:*\n"
               f"• Join artist fan club / email list\n"
               f"• Make sure your Ticketmaster account is ready\n"
               f"• Codes to try: {' | '.join(codes[:4])}\n\n"
               f"[Buy link]({url})")
            state["presale_alerted"].add(alert_key_72)
            log.info(f"Presale 72hr alert: {name}")

        elif pd == 1 and alert_key_24 not in state["presale_alerted"]:
            tg(f"🚨 *PRESALE TOMORROW!*\n\n"
               f"🎟 *{name}*\n"
               f"📍 {e.get('venue','')} · {e.get('city','')}\n"
               f"🔑 Presale opens: *{ps[:10]}*\n\n"
               f"💰 Est. profit: *+${profit:.0f}/ticket* (+${profit*4:.0f} for 4)\n\n"
               f"*Codes to try first:*\n"
               f"{chr(10).join(['• '+c for c in codes[:6]])}\n\n"
               f"*Set an alarm for presale time!*\n"
               f"[Buy link]({url})")
            state["presale_alerted"].add(alert_key_24)
            log.info(f"Presale 24hr alert: {name}")

        elif pd == 0 and alert_key_live not in state["presale_alerted"]:
            tg(f"🔴 *PRESALE IS LIVE RIGHT NOW!*\n\n"
               f"🎟 *{name}*\n"
               f"📍 {e.get('venue','')} · {e.get('city','')}\n\n"
               f"💰 Buy now at face value!\n"
               f"Est. profit: *+${profit:.0f}/ticket*\n"
               f"Buy 4 tickets = *+${profit*4:.0f} profit*\n\n"
               f"*Try these codes NOW:*\n"
               f"{chr(10).join(['• '+c for c in codes[:6]])}\n\n"
               f"🎯 Target: Floor GA or lower bowl 100-115\n\n"
               f"[→ BUY TICKETS NOW]({url})")
            state["presale_alerted"].add(alert_key_live)
            log.info(f"Presale LIVE alert: {name}")

# ── EVENT FETCHER ─────────────────────────────────────────────
async def fetch_ticketmaster() -> List[dict]:
    if not TM_KEY: return []
    import aiohttp
    events = []
    cities = [("Seattle","WA"),("Tacoma","WA"),("Bellevue","WA"),("Portland","OR")]
    async with aiohttp.ClientSession() as session:
        for city, state_code in cities:
            params = {
                "apikey": TM_KEY, "city": city,
                "stateCode": state_code, "countryCode": "US",
                "classificationName": "music,sports,comedy,arts",
                "size": 100,
                "startDateTime": datetime.now().strftime("%Y-%m-%dT00:00:00Z"),
                "endDateTime": (datetime.now()+timedelta(days=180)).strftime("%Y-%m-%dT00:00:00Z"),
                "sort": "date,asc",
            }
            try:
                async with session.get(
                    "https://app.ticketmaster.com/discovery/v2/events.json",
                    params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200: continue
                    data = await r.json()
                for item in data.get("_embedded",{}).get("events",[]):
                    v  = (item.get("_embedded",{}).get("venues",[{}]) or [{}])[0]
                    p  = (item.get("priceRanges",[{}]) or [{}])[0]
                    a  = (item.get("_embedded",{}).get("attractions",[{}]) or [{}])[0]
                    ps = item.get("sales",{}).get("presales",[])
                    # Get presale date
                    presale_date = ""
                    if ps:
                        for presale in ps:
                            pd_str = presale.get("startDateTime","")[:10]
                            if pd_str:
                                presale_date = pd_str
                                break
                    events.append({
                        "id":           f"tm_{item.get('id','')}",
                        "name":         item.get("name",""),
                        "artist":       a.get("name",""),
                        "venue":        v.get("name",""),
                        "city":         v.get("city",{}).get("name",city),
                        "capacity":     0,
                        "date":         item.get("dates",{}).get("start",{}).get("localDate",""),
                        "category":     item.get("classifications",[{}])[0].get("segment",{}).get("name","music"),
                        "face_low":     p.get("min",0),
                        "face_high":    p.get("max",0),
                        "url":          item.get("url",""),
                        "presale_date": presale_date,
                        "on_sale":      item.get("sales",{}).get("public",{}).get("startDateTime",""),
                        "source":       "ticketmaster",
                    })
            except Exception as e:
                log.debug(f"TM {city}: {e}")
    log.info(f"Ticketmaster: {len(events)} events")
    return events

def get_mock_events() -> List[dict]:
    today = date.today()
    def d(n): return str(today+timedelta(days=n))
    return [
        {"id":"m01","name":"Morgan Wallen — One Thing at a Time Tour","artist":"Morgan Wallen","venue":"Climate Pledge Arena","city":"Seattle","capacity":18100,"date":d(38),"category":"concert","face_low":89,"face_high":299,"url":"https://www.ticketmaster.com","presale_date":d(2),"on_sale":d(4),"source":"demo"},
        {"id":"m02","name":"Dave Chappelle — One Night Only","artist":"Dave Chappelle","venue":"Paramount Theatre","city":"Seattle","capacity":2807,"date":d(12),"category":"comedy","face_low":75,"face_high":150,"url":"https://www.ticketmaster.com","presale_date":d(0),"on_sale":d(1),"source":"demo"},
        {"id":"m03","name":"Kendrick Lamar — Grand National Tour","artist":"Kendrick Lamar","venue":"Lumen Field","city":"Seattle","capacity":69000,"date":d(55),"category":"concert","face_low":99,"face_high":399,"url":"https://www.ticketmaster.com","presale_date":d(5),"on_sale":d(7),"source":"demo"},
        {"id":"m04","name":"UFC Fight Night — Climate Pledge Arena","artist":"UFC","venue":"Climate Pledge Arena","city":"Seattle","capacity":18100,"date":d(22),"category":"sports","face_low":75,"face_high":200,"url":"https://www.ticketmaster.com","presale_date":d(1),"on_sale":d(3),"source":"demo"},
        {"id":"m05","name":"Chappell Roan — Pink Pony Tour","artist":"Chappell Roan","venue":"Moore Theatre","city":"Seattle","capacity":1419,"date":d(33),"category":"concert","face_low":55,"face_high":120,"url":"https://www.ticketmaster.com","presale_date":d(0),"on_sale":d(2),"source":"demo"},
        {"id":"m06","name":"Zach Bryan — The Quittin Time Tour","artist":"Zach Bryan","venue":"Climate Pledge Arena","city":"Seattle","capacity":18100,"date":d(71),"category":"concert","face_low":79,"face_high":249,"url":"https://www.ticketmaster.com","presale_date":d(7),"on_sale":d(9),"source":"demo"},
        {"id":"m07","name":"Sabrina Carpenter — Short n Sweet Tour","artist":"Sabrina Carpenter","venue":"Paramount Theatre","city":"Seattle","capacity":2807,"date":d(62),"category":"concert","face_low":65,"face_high":185,"url":"https://www.ticketmaster.com","presale_date":d(6),"on_sale":d(8),"source":"demo"},
        {"id":"m08","name":"Seattle Sounders FC — MLS Playoffs","artist":"Seattle Sounders","venue":"Lumen Field","city":"Seattle","capacity":69000,"date":d(22),"category":"sports","face_low":55,"face_high":180,"url":"https://www.axs.com","presale_date":"","on_sale":d(1),"source":"demo"},
    ]

def filter_events(events):
    seen, out = set(), []
    for e in events:
        if any(k in e.get("name","").lower() for k in SKIP): continue
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
        raw = await fetch_ticketmaster() if TM_KEY else []
        if not raw:
            log.info("No API key — using demo events")
            raw = get_mock_events()

        events = filter_events(raw)
        log.info(f"{len(events)} events after filtering")

        scored = []
        for e in events:
            res   = estimate_resell(e)
            sc    = score_event(e, res)
            codes = find_codes(e)
            strat = get_strategy(e, sc)
            pd    = _presale_days(e.get("presale_date","")) if e.get("presale_date") else None

            e["score"]        = sc
            e["codes"]        = codes
            e["strategy"]     = strat
            e["presale_days"] = pd
            scored.append(e)

        scored.sort(key=lambda x: x.get("score",{}).get("total",0), reverse=True)

        # Alert on new high-value events
        for e in scored:
            eid   = e.get("id","")
            total = e.get("score",{}).get("total",0)
            if total >= 72 and eid not in state["alerted_ids"]:
                sc     = e.get("score",{})
                codes  = e.get("codes",[])[:5]
                profit = sc.get("profit_per",0)
                pd_val = e.get("presale_days")

                # Get Claude AI analysis
                ai_analysis = claude_analyze(e, sc)

                presale_note = ""
                if pd_val is not None and pd_val >= 0:
                    if pd_val == 0:
                        presale_note = "\n🔴 *PRESALE IS LIVE NOW!*\n"
                    elif pd_val == 1:
                        presale_note = "\n⚠️ *Presale opens TOMORROW!*\n"
                    elif pd_val <= 7:
                        presale_note = f"\n⏰ Presale in {pd_val} days\n"

                msg = (
                    f"🎟 *{sc.get('verdict','')} — Score {total:.0f}/100*\n\n"
                    f"*{e.get('name','')}*\n"
                    f"📍 {e.get('venue','')} · {e.get('city','')}\n"
                    f"📅 {e.get('date','')} ({_days_until(e.get('date',''))}d away)\n"
                    f"{presale_note}\n"
                    f"💰 +${profit:.0f}/ticket · +${profit*4:.0f} buying 4\n"
                    f"Face: ${sc.get('face_avg',0):.0f} → Resell: ${sc.get('resell_avg',0):.0f}\n"
                    f"ROI: {sc.get('roi_pct',0):.0f}%\n\n"
                )
                if ai_analysis:
                    msg += f"🤖 *AI Analysis:*\n{ai_analysis}\n\n"
                msg += (
                    f"🔑 Codes: {' · '.join(codes[:5])}\n\n"
                    f"[Buy tickets]({e.get('url','')})"
                )
                tg(msg)
                state["alerted_ids"].add(eid)

        # Check presale countdown alerts
        check_presale_alerts(scored)

        state["events"]              = scored
        state["last_scan"]           = datetime.now().isoformat()
        state["scan_count"]         += 1
        state["total_opportunities"] = len([e for e in scored if e.get("score",{}).get("total",0)>=65])

        log.info(f"Done. {len(scored)} events · {state['total_opportunities']} opportunities")
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
    except Exception as e:
        log.warning(f"Save error: {e}")

async def scan_loop():
    while True:
        try: await run_scan()
        except Exception as e: log.error(f"Scan error: {e}", exc_info=True)
        log.info(f"Next scan in {SCAN_MINS} min")
        await asyncio.sleep(SCAN_MINS * 60)

# ── DASHBOARD HTML ────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ticket Intel Pro</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0b0f1a;--bg2:#121929;--bg3:#1a2236;--bd:#1f2d44;--green:#00d4aa;--amber:#f5a623;--red:#ff4d6d;--blue:#4a9eff;--purple:#a78bfa;--dim:#7a8ea8;--text:#dde6f0;--mono:'DM Mono',monospace}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:inherit;text-decoration:none}
header{background:var(--bg2);border-bottom:1px solid var(--bd);padding:12px 20px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:50}
.logo{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--green);display:flex;align-items:center;gap:8px;letter-spacing:2px}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pu 2s infinite;flex-shrink:0}
@keyframes pu{0%,100%{opacity:1}50%{opacity:.2}}
.hright{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--dim)}
.sbtn{font-family:var(--mono);font-size:10px;padding:6px 14px;border-radius:5px;background:rgba(0,212,170,.1);border:1px solid rgba(0,212,170,.3);color:var(--green);cursor:pointer;letter-spacing:1px;transition:all .2s}
.sbtn:hover{background:rgba(0,212,170,.22)}
.sbtn:disabled{opacity:.4;cursor:not-allowed}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;padding:14px 20px}
.stat{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:12px}
.stat-n{font-family:var(--mono);font-size:20px;font-weight:500}
.stat-l{font-size:10px;color:var(--dim);margin-top:2px;text-transform:uppercase;letter-spacing:.8px}
.main{padding:0 20px 28px}
.frow{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.f{font-size:12px;padding:4px 12px;border-radius:20px;border:1px solid var(--bd);background:transparent;color:var(--dim);cursor:pointer;transition:all .15s}
.f.on{background:var(--green);color:#0b0f1a;border-color:transparent;font-weight:500}
.sh{font-family:var(--mono);font-size:10px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin:14px 0 8px}
.ec{background:var(--bg2);border:1px solid var(--bd);border-radius:12px;margin-bottom:8px;overflow:hidden;cursor:pointer}
.ec:hover{border-color:#2a3d5a}
.ec.open{border-color:rgba(0,212,170,.4)}
.et{display:flex;gap:10px;align-items:flex-start;padding:12px 14px}
.esc{width:42px;height:42px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:14px;font-weight:500;flex-shrink:0}
.ei{flex:1;min-width:0}
.en{font-size:13px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.em{font-size:11px;color:var(--dim);margin-top:2px}
.er{text-align:right;flex-shrink:0}
.ep{font-size:14px;font-weight:500;color:var(--green)}
.el{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.7px}
.vt{display:inline-block;font-family:var(--mono);font-size:9px;padding:2px 6px;border-radius:3px;margin-top:4px;letter-spacing:.5px}
.vMUST{background:rgba(255,77,109,.15);color:var(--red);border:1px solid rgba(255,77,109,.3)}
.vSTRONG{background:rgba(0,212,170,.12);color:var(--green);border:1px solid rgba(0,212,170,.3)}
.vBUY{background:rgba(0,212,170,.08);color:var(--green);border:1px solid rgba(0,212,170,.2)}
.vWATCH{background:rgba(245,166,35,.1);color:var(--amber);border:1px solid rgba(245,166,35,.25)}
.psa{font-size:11px;margin-top:3px}
.psa.live{color:var(--red);font-weight:500}
.psa.soon{color:var(--amber)}
.psa.upcoming{color:var(--dim)}
.sb2{height:2px;background:var(--bd);margin:0 14px}
.sf2{height:100%;border-radius:1px;transition:width .4s}
.bd2{display:none;padding:12px 14px;border-top:1px solid var(--bd)}
.ec.open .bd2{display:block}
.dr{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-bottom:10px}
.db{background:var(--bg3);border-radius:8px;padding:9px 10px}
.dv{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--text)}
.dl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.7px;margin-top:2px}
.sps{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:10px}
.sp{font-size:10px;background:var(--bg3);border:1px solid var(--bd);border-radius:4px;padding:2px 7px;color:var(--dim)}
.sp b{color:var(--text)}
.adv{background:var(--bg3);border-radius:8px;padding:10px 12px;margin-bottom:7px}
.at{font-size:10px;font-weight:500;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px}
.ab{font-size:12px;color:var(--dim);line-height:1.6}
.ab b{color:var(--text)}
.ai-analysis{background:rgba(139,92,246,.1);border:1px solid rgba(139,92,246,.2);border-radius:8px;padding:10px 12px;margin-bottom:7px}
.ai-label{font-size:10px;color:var(--purple);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px;font-weight:500}
.ai-text{font-size:12px;color:var(--dim);line-height:1.6}
.cw{margin-bottom:10px}
.ct{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px}
.cs{display:flex;flex-wrap:wrap;gap:4px}
.code{font-family:var(--mono);font-size:10px;background:rgba(74,158,255,.1);border:1px solid rgba(74,158,255,.22);border-radius:4px;padding:2px 7px;color:var(--blue)}
.brow{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.btn{font-size:11px;padding:6px 12px;border-radius:6px;border:1px solid var(--bd);background:transparent;color:var(--text);cursor:pointer;transition:all .15s}
.btn:hover{background:var(--bg3)}
.btn.p{background:rgba(0,212,170,.12);border-color:rgba(0,212,170,.35);color:var(--green)}
.btn.p:hover{background:rgba(0,212,170,.22)}
.badge{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:4px;margin-left:8px}
.empty{text-align:center;padding:50px;color:var(--dim);font-size:13px}
.spin{text-align:center;padding:40px;color:var(--dim);font-family:var(--mono);font-size:11px;letter-spacing:2px;animation:fade 1.5s infinite}
@keyframes fade{0%,100%{opacity:.3}50%{opacity:1}}
@media(max-width:600px){.stats{grid-template-columns:1fr 1fr}.dr{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<header>
  <div class="logo">
    <div class="pulse"></div>TICKET INTEL PRO
    <span class="badge" id="mode-badge" style="background:rgba(245,166,35,.15);color:var(--amber);border:1px solid rgba(245,166,35,.25)">DEMO</span>
  </div>
  <div class="hright">
    <span id="status">Loading...</span>
    <button class="sbtn" id="sbtn" onclick="scan()">SCAN NOW</button>
  </div>
</header>
<div class="stats" id="stats"></div>
<div class="main">
  <div class="frow" id="frow"></div>
  <div id="list"><div class="spin">SCANNING...</div></div>
</div>
<script>
let evts=[], af='all', oid=null;
const fmt=s=>{try{return new Date(s).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});}catch{return s;}};
const dTo=s=>{try{return Math.round((new Date(s)-new Date())/86400000);}catch{return 0;}};
const sc=n=>n>=85?'#ff4d6d':n>=70?'#00d4aa':n>=50?'#f5a623':'#7a8ea8';
const sbg=n=>n>=85?'rgba(255,77,109,.15)':n>=70?'rgba(0,212,170,.12)':n>=50?'rgba(245,166,35,.12)':'rgba(122,134,153,.1)';
const vk=v=>v.split(' ')[0];

async function load(){
  try{
    const r=await fetch('/api/events');
    const d=await r.json();
    evts=d.events||[];
    updateStats(d);
    renderF();
    renderList();
    const t=d.last_scan?new Date(d.last_scan).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'never';
    document.getElementById('status').textContent='Updated '+t;
    const live=evts.some(e=>e.source&&e.source!=='demo'&&e.source!=='mock');
    const b=document.getElementById('mode-badge');
    if(b){b.textContent=live?'LIVE':'DEMO';b.style.background=live?'rgba(0,212,170,.12)':'rgba(245,166,35,.12)';b.style.color=live?'#00d4aa':'#f5a623';b.style.borderColor=live?'rgba(0,212,170,.25)':'rgba(245,166,35,.25)';}
  }catch(e){console.error(e);}
}

async function scan(){
  const btn=document.getElementById('sbtn');
  btn.disabled=true;btn.textContent='SCANNING...';
  try{await fetch('/api/scan',{method:'POST'});await new Promise(r=>setTimeout(r,4000));await load();}
  catch(e){console.error(e);}
  btn.disabled=false;btn.textContent='SCAN NOW';
}

function updateStats(d){
  const ev=d.events||[];
  const must=ev.filter(e=>e.score?.verdict==='MUST BUY').length;
  const str=ev.filter(e=>e.score?.verdict==='STRONG BUY').length;
  const opp=d.total_opportunities||0;
  const presales=ev.filter(e=>e.presale_days!=null&&e.presale_days>=0&&e.presale_days<=7&&(e.score?.total||0)>=65).length;
  const maxP=Math.max(0,...ev.map(e=>e.score?.profit_4||0));
  document.getElementById('stats').innerHTML=`
    <div class="stat"><div class="stat-n" style="color:#ff4d6d">${must}</div><div class="stat-l">Must buy</div></div>
    <div class="stat"><div class="stat-n" style="color:#00d4aa">${str}</div><div class="stat-l">Strong buy</div></div>
    <div class="stat"><div class="stat-n" style="color:#f5a623">${presales}</div><div class="stat-l">Presales this week</div></div>
    <div class="stat"><div class="stat-n" style="color:#00d4aa">$${Math.round(maxP).toLocaleString()}</div><div class="stat-l">Best 4-ticket profit</div></div>
  `;
}

function renderF(){
  document.getElementById('frow').innerHTML=
    ['all','concert','sports','comedy'].map(c=>`<button class="f${af===c?' on':''}" onclick="setF('${c}')">${c==='all'?'All events':c[0].toUpperCase()+c.slice(1)}</button>`).join('')+
    `<button class="f" onclick="filterPresale()" id="f-presale">Presales this week</button>`+
    `<button class="f" style="margin-left:auto" onclick="load()">↻</button>`;
}
function setF(f){af=f;renderF();renderList();}
function filterPresale(){
  const filtered=evts.filter(e=>e.presale_days!=null&&e.presale_days>=0&&e.presale_days<=7&&(e.score?.total||0)>=65);
  renderListItems(filtered,'Presales opening this week');
}

function renderList(){
  const fl=af==='all'?evts:evts.filter(e=>{
    const cat=(e.category||'').toLowerCase();
    return cat.includes(af)||(af==='concert'&&(cat.includes('music')||cat.includes('concert')));
  });
  const groups={MUST:fl.filter(e=>e.score?.verdict==='MUST BUY'),STRONG:fl.filter(e=>e.score?.verdict==='STRONG BUY'),BUY:fl.filter(e=>e.score?.verdict==='BUY'),WATCH:fl.filter(e=>e.score?.verdict==='WATCH')};
  const titles={'MUST':'Act now — must buy','STRONG':'Strong buy','BUY':'Buy','WATCH':'Watching'};
  let html='';
  for(const[k,arr]of Object.entries(groups)){
    if(arr.length){html+=`<div class="sh">${titles[k]}</div>`;arr.forEach(e=>{html+=card(e);});}
  }
  if(!html)html='<div class="empty">No events. Click SCAN NOW to load.</div>';
  document.getElementById('list').innerHTML=html;
}

function renderListItems(arr, title){
  let html=`<div class="sh">${title}</div>`;
  if(!arr.length) html='<div class="empty">No presales found this week.</div>';
  arr.forEach(e=>{html+=card(e);});
  document.getElementById('list').innerHTML=html;
}

function presaleTag(e){
  const pd=e.presale_days;
  if(pd==null||pd<0) return '';
  if(pd===0) return '<div class="psa live">PRESALE LIVE NOW — BUY NOW!</div>';
  if(pd===1) return '<div class="psa soon">Presale opens TOMORROW</div>';
  if(pd<=3)  return `<div class="psa soon">Presale in ${pd} days</div>`;
  if(pd<=7)  return `<div class="psa upcoming">Presale in ${pd} days</div>`;
  return '';
}

function card(e){
  const sc_=e.score||{},st=e.strategy||{};
  const tot=sc_.total||0,vk_=vk(sc_.verdict||'WATCH');
  const days=dTo(e.date);
  return `<div class="ec${oid===e.id?' open':''}" id="c${e.id}" onclick="tog('${e.id}')">
    <div class="et">
      <div class="esc" style="background:${sbg(tot)};color:${sc(tot)}">${Math.round(tot)}</div>
      <div class="ei">
        <div class="en">${e.name||''}</div>
        <div class="em">${e.venue||''} · ${e.city||''} · ${fmt(e.date)} (${days}d)</div>
        ${presaleTag(e)}
      </div>
      <div class="er">
        <div class="ep">+$${Math.round(sc_.profit_per||0)}/ticket</div>
        <div class="el">profit</div>
        <div class="vt v${vk_}">${sc_.verdict||''}</div>
      </div>
    </div>
    <div class="sb2"><div class="sf2" style="width:${Math.min(tot,100)}%;background:${sc(tot)}"></div></div>
    <div class="bd2">
      <div class="dr">
        <div class="db"><div class="dv" style="color:#00d4aa">+$${Math.round(sc_.profit_4||0).toLocaleString()}</div><div class="dl">4-ticket profit</div></div>
        <div class="db"><div class="dv">$${Math.round(sc_.face_avg||0)} → $${Math.round(sc_.resell_avg||0)}</div><div class="dl">Face → resell</div></div>
        <div class="db"><div class="dv">${Math.round(sc_.roi_pct||0)}%</div><div class="dl">ROI</div></div>
        <div class="db"><div class="dv">$${(st.capital_needed||0).toLocaleString()}</div><div class="dl">Capital</div></div>
      </div>
      <div class="sps">
        <span class="sp">Demand <b>${Math.round(sc_.demand||0)}</b></span>
        <span class="sp">Artist <b>${Math.round(sc_.artist_score||0)}</b></span>
        <span class="sp">Venue <b>${Math.round(sc_.venue_score||0)}</b></span>
        <span class="sp">Gap <b>${Math.round(sc_.price_gap||0)}</b></span>
      </div>
      <div class="adv"><div class="at">Why this event</div><div class="ab">${sc_.reasoning||''}</div></div>
      <div class="adv"><div class="at">Where to sit</div><div class="ab">${st.seat_advice||''}</div></div>
      <div class="adv"><div class="at">Pricing strategy</div><div class="ab">
        List at <b>${st.list_at||'—'}</b> on StubHub + Vivid Seats + SeatGeek.<br>
        2 weeks out: <b>${st.reduce_14||'—'}</b> · 3 days out: <b>${st.reduce_3||'—'}</b><br>
        Never below <b>${st.floor||'—'}</b>
      </div></div>
      <div class="adv"><div class="at">Buy recommendation</div><div class="ab"><b>${st.qty_reason||''}</b></div></div>
      ${(e.codes||[]).length?`<div class="cw"><div class="ct">Presale codes to try</div><div class="cs">${(e.codes||[]).slice(0,10).map(c=>`<span class="code">${c}</span>`).join('')}</div></div>`:''}
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

# ── WEB ROUTES ────────────────────────────────────────────────
async def handle_index(req): return web.Response(text=HTML, content_type="text/html")
async def handle_events(req):
    return web.json_response({
        "events":state["events"],"last_scan":state["last_scan"],
        "scan_count":state["scan_count"],"total_opportunities":state["total_opportunities"]
    }, dumps=lambda o:json.dumps(o,default=str))
async def handle_scan(req):
    asyncio.create_task(run_scan())
    return web.json_response({"status":"scanning"})

async def on_startup(app):
    if DATA.exists():
        try:
            with open(DATA) as f: saved=json.load(f)
            state["events"]              = saved.get("events",[])
            state["last_scan"]           = saved.get("last_scan")
            state["scan_count"]          = saved.get("scan_count",0)
            state["total_opportunities"] = saved.get("total_opportunities",0)
            log.info(f"Loaded {len(state['events'])} saved events")
        except: pass
    asyncio.create_task(scan_loop())

# ── MAIN ──────────────────────────────────────────────────────
def main():
    mode = "LIVE" if TM_KEY else "DEMO"
    tg_status = "Enabled ✓" if TG_TOKEN else "Add TELEGRAM_TOKEN to .env"
    claude_status = "Enabled ✓" if CLAUDE_KEY else "Add ANTHROPIC_API_KEY to .env"

    print(f"""
╔{'═'*51}╗
║  TICKET INTEL PRO                               ║
╠{'═'*51}╣
║  Mode:       {mode:<38}║
║  Dashboard:  http://localhost:{PORT:<22}║
║  Telegram:   {tg_status:<38}║
║  Claude AI:  {claude_status:<38}║
║  Scanning:   Every {SCAN_MINS} minutes                    ║
╚{'═'*51}╝

  Open http://localhost:{PORT} in your browser
  Press Ctrl+C to stop
""")

    app = web.Application()
    app.router.add_get("/",           handle_index)
    app.router.add_get("/api/events", handle_events)
    app.router.add_post("/api/scan",  handle_scan)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT, print=lambda _:None)

if __name__ == "__main__":
    main()
