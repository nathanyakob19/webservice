import json
import os
import re
import uuid
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import math
import time
import traceback

from flask import Blueprint, jsonify, request

from pymongo import MongoClient
from urllib.parse import quote_plus, urlsplit, urlunsplit, unquote_plus


def read_env_value(key, default=""):
    if os.environ.get(key):
        return os.environ.get(key)
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    except Exception:
        pass
    return default
ai_features = Blueprint("ai_features", __name__)

SUPPORTED_LANGS = {"en": "English", "hi": "Hindi", "mr": "Marathi"}
DAY_LABELS = {"en": "Day", "hi": "Din", "mr": "Divas"}
CURRENCY_SYMBOLS = {
    "USD": "$",
    "INR": "Rs ",
    "EUR": "EUR ",
    "GBP": "GBP ",
    "AUD": "AUD ",
    "CAD": "CAD ",
}

_rates_cache = {"base": "INR", "rates": {}, "ts": 0}
OPENTRIPMAP_API_KEY = read_env_value("OPENTRIPMAP_API_KEY", "").strip()

# ---------------- DB (SAME DB AS backend.py) ----------------
username = quote_plus("nate")
password = quote_plus("Simba234")
DEFAULT_URI = f"mongodb+srv://{username}:{password}@pathease.1vbi85h.mongodb.net/patheaseDB"

def _redact_mongo_uri(uri):
    if not uri:
        return uri
    try:
        parts = urlsplit(uri)
        if "@" not in parts.netloc:
            return uri
        userinfo, hostinfo = parts.netloc.rsplit("@", 1)
        if ":" not in userinfo:
            return uri
        user, _pwd = userinfo.split(":", 1)
        netloc = f"{user}:***@{hostinfo}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "<invalid_mongo_uri>"

def normalize_mongo_uri(uri):
    if not uri:
        return uri
    try:
        uri = "".join(str(uri).split())
        parts = urlsplit(uri)
        if parts.scheme not in ("mongodb", "mongodb+srv"):
            return uri
        if "@" not in parts.netloc:
            return uri
        userinfo, hostinfo = parts.netloc.rsplit("@", 1)
        user, pwd = (userinfo.split(":", 1) + [""])[:2]
        user = quote_plus(unquote_plus(user))
        pwd = quote_plus(unquote_plus(pwd)) if pwd else ""
        netloc = f"{user}:{pwd}@{hostinfo}" if pwd else f"{user}@{hostinfo}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return uri

# Prefer PATHEASE_MONGO_URI, then MONGO_URI, then DEFAULT_URI
_env_mongo_uri = read_env_value("PATHEASE_MONGO_URI", "").strip() or read_env_value("MONGO_URI", "").strip()
MONGO_URI = normalize_mongo_uri(_env_mongo_uri or DEFAULT_URI)
print("Mongo URI in use:", _redact_mongo_uri(MONGO_URI))
client = MongoClient(MONGO_URI) if MONGO_URI else None
db = client["patheaseDB"] if client is not None else None
places_collection = db["places"] if db is not None else None

# ---------------- HELPERS ----------------
def normalize_lang(lang):
    return lang if lang in SUPPORTED_LANGS else "en"

def safe_int(v, d=1):
    try:
        return max(1, int(v))
    except:
        return d

def safe_float(v, d=0.0):
    try:
        return float(re.sub(r"[^0-9.]", "", str(v)))
    except:
        return d

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = (
        math.sin(dLat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dLon / 2) ** 2
    )
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def fetch_rates(base="INR"):
    if _rates_cache["rates"] and _rates_cache["base"] == base and (time.time() - _rates_cache["ts"] < 3600):
        return _rates_cache["rates"]

    try:
        url = f"https://api.exchangerate.host/latest?base={base}"
        with urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
            rates = data.get("rates", {})
            if rates:
                _rates_cache["base"] = base
                _rates_cache["rates"] = rates
                _rates_cache["ts"] = time.time()
                return rates
    except Exception:
        pass
    return {}


def convert_amount(amount, from_currency, to_currency):
    if amount is None:
        return None
    if from_currency == to_currency:
        return round(amount, 2)
    rates = fetch_rates(base=from_currency)
    rate = rates.get(to_currency)
    if not rate:
        return round(amount, 2)
    return round(amount * float(rate), 2)



def fetch_json(url):
    try:
        with urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def fetch_otm_places(destination, limit=12, radius=8000):
    if not OPENTRIPMAP_API_KEY or not destination:
        return []
    name = destination.strip()
    geo_url = f"https://api.opentripmap.com/0.1/en/places/geoname?name={name}&apikey={OPENTRIPMAP_API_KEY}"
    geo = fetch_json(geo_url)
    if not geo or "lat" not in geo or "lon" not in geo:
        return []
    lat = geo["lat"]
    lon = geo["lon"]
    radius_url = (
        "https://api.opentripmap.com/0.1/en/places/radius"
        f"?radius={radius}&lon={lon}&lat={lat}&format=json&limit={limit}&apikey={OPENTRIPMAP_API_KEY}"
    )
    items = fetch_json(radius_url) or []
    places = []
    for it in items:
        if "name" not in it or not it.get("name"):
            continue
        places.append({
            "placeName": it.get("name"),
            "location": {"lat": it.get("lat"), "lng": it.get("lon")},
            "source": "opentripmap",
            "xid": it.get("xid"),
        })
    return places


def fetch_otm_places_by_coords(lat, lon, limit=12, radius=8000):
    if not OPENTRIPMAP_API_KEY:
        return []
    if lat is None or lon is None:
        return []
    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return []

    radius_url = (
        "https://api.opentripmap.com/0.1/en/places/radius"
        f"?radius={radius}&lon={lon}&lat={lat}&format=json&limit={limit}&apikey={OPENTRIPMAP_API_KEY}"
    )
    items = fetch_json(radius_url) or []
    places = []
    for it in items:
        if "name" not in it or not it.get("name"):
            continue
        places.append({
            "placeName": it.get("name"),
            "location": {"lat": it.get("lat"), "lng": it.get("lon")},
            "source": "opentripmap",
            "xid": it.get("xid"),
        })
    return places


def path_distance_km(start_lat, start_lng, places):
    total = 0.0
    prev_lat, prev_lng = start_lat, start_lng
    for p in places:
        loc = p.get("location") or {}
        if "lat" in loc and "lng" in loc:
            try:
                lat = float(loc["lat"])
                lng = float(loc["lng"])
            except Exception:
                continue
            total += haversine(prev_lat, prev_lng, lat, lng)
            prev_lat, prev_lng = lat, lng
    return round(total, 2)

def split_budget(budget, currency="INR"):
    if not budget or budget <= 0:
        return None
    return {
        "currency": currency,
        "symbol": CURRENCY_SYMBOLS.get(currency, currency + " "),
        "stay": round(budget * 0.4, 2),
        "food": round(budget * 0.25, 2),
        "transport": round(budget * 0.2, 2),
        "tickets": round(budget * 0.1, 2),
        "misc": round(budget * 0.05, 2),
    }

# ---------------- SIMPLE CHAT + SENTIMENT ----------------
_POS_WORDS = {
    "good", "great", "amazing", "awesome", "love", "loved", "nice", "excellent",
    "fantastic", "beautiful", "wonderful", "friendly", "clean", "safe", "helpful",
    "pleasant", "enjoyed", "enjoy", "best",
}
_NEG_WORDS = {
    "bad", "terrible", "awful", "hate", "hated", "poor", "dirty", "unsafe",
    "worst", "boring", "rude", "slow", "expensive", "crowded", "noisy",
    "disappointed", "disappointing",
}

def _basic_sentiment(text):
    tokens = re.findall(r"[a-zA-Z']+", text.lower())
    pos = sum(1 for t in tokens if t in _POS_WORDS)
    neg = sum(1 for t in tokens if t in _NEG_WORDS)
    total = max(1, len(tokens))
    score = round((pos - neg) / total, 3)
    if score > 0.1:
        label = "positive"
    elif score < -0.1:
        label = "negative"
    else:
        label = "neutral"
    return {"label": label, "score": score, "word_count": len(tokens)}


def _extract_first_json_object(text):
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except Exception:
        return None

@ai_features.route("/ai/guide-chat", methods=["POST"])
def guide_chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    destination = (data.get("destination") or "").strip()
    language = normalize_lang(data.get("language") or "en")
    user_lat = data.get("lat")
    user_lng = data.get("lng")
    try:
        user_lat = float(user_lat) if user_lat is not None else None
        user_lng = float(user_lng) if user_lng is not None else None
    except Exception:
        user_lat = None
        user_lng = None
    if not message:
        return jsonify({"error": "Message is required"}), 400

    system_prompt = (
        "You are an advanced Global Tourism Intelligence Assistant.\n\n"
        "Your goal is to provide accurate, well-structured, globally relevant tourism guidance including destinations, itineraries, travel tips, culture, safety, accessibility, budget planning, transportation, and seasonal recommendations.\n\n"
        "CORE BEHAVIOR:\n"
        "1) Provide globally balanced tourism insights (not limited to one country unless asked).\n"
        "2) When suggesting destinations, include: Why it is famous; Best time to visit; Approximate budget range (low/mid/high); Safety overview; Accessibility (if relevant).\n"
        "3) If user asks for itinerary: Structure response as Day 1, Day 2, Day 3 (and so on) with activities, travel time, and tips.\n"
        "4) If user mentions budget, optimize accordingly.\n"
        "5) If user mentions family, couple, solo, student, luxury, or adventure - personalize suggestions.\n"
        "6) Mention visa considerations only if relevant.\n"
        "7) Highlight sustainable and eco-friendly options where possible.\n"
        "8) If data is approximate, state it clearly.\n"
        "9) Keep responses clear, structured, and useful.\n"
        "10) If details are missing (budget, duration, country), ask one clarifying question only.\n\n"
        "SPECIAL MODES:\n"
        "If user says: Luxury -> focus on premium hotels, fine dining, private transport. Budget -> hostels, public transport, free attractions. Adventure -> trekking, water sports, wildlife. Cultural -> museums, heritage sites, local traditions. Accessible travel -> wheelchair-friendly info. Hidden gems -> less crowded alternatives.\n\n"
        "OUTPUT STRUCTURE:\n"
        "1) Overview\n"
        "2) Why Visit\n"
        "3) Best Time to Visit\n"
        "4) Budget Estimate\n"
        "5) 3-5 Day Itinerary (if applicable)\n"
        "6) Travel Tips\n"
        "7) Optional Upgrades\n\n"
        "Respond in the requested language code. Answer only what the user asked. Do not add extra sections, itinerary blocks, or optional tips unless explicitly requested."
    )
    user_prompt = (
        f"Language: {language}\n"
        f"Destination: {destination or 'Not specified'}\n"
        f"User message: {message}\n"
    )
    content, llm_err = call_llm(system_prompt, user_prompt)
    if content:
        return jsonify({"reply": content.strip()})

    def _extract_params(text, existing_dest):
        dest = existing_dest
        days = None
        budget = None
        modes = set()
        t = text.lower()
        m = re.search(r"destination\s*:\s*([a-zA-Z ,\-]+)", text, re.I)
        if m:
            dest = m.group(1).strip()
        m = re.search(r"days?\s*:\s*(\d+)", text, re.I)
        if m:
            try:
                days = int(m.group(1))
            except Exception:
                days = None
        m = re.search(r"budget\s*:\s*([\d,\.]+)", text, re.I)
        if m:
            try:
                budget = float(m.group(1).replace(",", ""))
            except Exception:
                budget = None
        for kw in ["family", "couple", "solo", "student", "luxury", "adventure", "cultural", "accessible", "hidden gems", "budget"]:
            if kw in t:
                modes.add(kw)
        return dest, days, budget, modes

    def _best_time_for_city(name):
        n = (name or "").lower()
        if "mumbai" in n:
            return "Nov-Feb (cooler, drier); Jun-Sep monsoon with heavy rain"
        if "goa" in n:
            return "Nov-Feb for beaches; Jun-Sep monsoon for lush landscapes"
        if "delhi" in n:
            return "Oct-Mar (pleasant); Apr-Jun very hot"
        return "Depends on region; generally spring and autumn are comfortable"

    def _why_visit(name):
        n = (name or "").strip()
        if not n:
            return "Vibrant culture, local cuisine, and accessible attractions."
        ln = n.lower()
        if "mumbai" in ln:
            return "Coastal city with Marine Drive, heritage architecture, Bollywood culture, and legendary street food."
        if "goa" in ln:
            return "Beaches, Portuguese heritage, seafood, and relaxed nightlife."
        if "delhi" in ln:
            return "Historic monuments, museums, markets, and diverse food."
        return f"Known for local culture, food, landmarks, and unique neighborhood vibes in {n}."

    def _city_info(name):
        n = (name or "").lower().strip()
        if "mumbai" in n:
            return {
                "best_time": "Nov-Feb; expect heavy monsoon Jun-Sep",
                "why": "Marine Drive promenade, Gateway of India, heritage architecture, film culture, and iconic street food.",
                "attractions": [
                    "Marine Drive",
                    "Gateway of India",
                    "Colaba Causeway",
                    "Chhatrapati Shivaji Terminus",
                    "Sanjay Gandhi National Park",
                    "Elephanta Caves (ferry from Gateway)",
                    "Bandra Fort & Bandstand",
                    "Siddhivinayak Temple",
                    "Juhu Beach",
                    "Prince of Wales Museum",
                ],
                "food_spots": [
                    "Vada Pav at Ashok Vada Pav (Dadar)",
                    "Pav Bhaji at Sardar Refreshments (Tardeo)",
                    "Seafood at Gajalee (Vile Parle)",
                    "Kebabs at Bademiya (Colaba)",
                    "Street chaat at Girgaum Chowpatty",
                    "Irani cafe snacks at Kyani & Co (Marine Lines)",
                    "South Indian at Cafe Madras (Matunga)",
                ],
                "itinerary": [
                    {
                        "morning": "Gateway of India & ferry views",
                        "afternoon": "Colaba Causeway shopping + Prince of Wales Museum",
                        "evening": "Marine Drive sunset walk",
                    },
                    {
                        "morning": "Elephanta Caves trip",
                        "afternoon": "Girgaum Chowpatty street food",
                        "evening": "Marine Drive or Nariman Point",
                    },
                    {
                        "morning": "Sanjay Gandhi National Park or Kanheri Caves",
                        "afternoon": "Bandra Fort & Bandstand",
                        "evening": "Juhu Beach",
                    },
                    {
                        "morning": "Siddhivinayak Temple",
                        "afternoon": "Kala Ghoda art district",
                        "evening": "Food trail at Bademiya/Colaba",
                    },
                ],
            }
        return None

    def _budget_band(b):
        if b is None or b <= 0:
            return "Low: < 5,000; Mid: 5,000-15,000; High: > 15,000 (approx, INR)"
        if b < 5000:
            return "Low"
        if b <= 15000:
            return "Mid"
        return "High"

    def _format_bullets(items):
        return "\n- " + "\n- ".join(items) if items else "\n- N/A"

    def _structured_response(dest, days, budget, modes, lang):
        d = (dest or "").strip() or "your destination"
        dy = days or 3
        bd = budget or 0
        info = _city_info(dest)
        if info:
            base_itin = info["itinerary"]
            itin = []
            for i in range(dy):
                src = base_itin[i % len(base_itin)]
                itin.append({
                    "day": i + 1,
                    "morning": src["morning"],
                    "afternoon": src["afternoon"],
                    "evening": src["evening"],
                    "tips": "",
                })
        else:
            itin = fallback_itinerary(d, dy, bd, lang)
        band = _budget_band(bd)
        lines = []
        lines.append("Overview")
        lines.append(f"{d.title()} trip tailored to your preferences. Focus: " + (", ".join(sorted(modes)) if modes else "general travel").title())
        lines.append("")
        lines.append("Why Visit")
        lines.append(info["why"] if info else _why_visit(dest))
        lines.append("")
        lines.append("Best Time to Visit")
        lines.append(info["best_time"] if info else _best_time_for_city(dest))
        lines.append("")
        lines.append("Budget Estimate")
        if bd and bd > 0:
            lines.append(f"Approx total: {int(bd)} INR ({band}). Breakdown may include stay, food, transport, tickets, misc.")
        else:
            lines.append(f"Range: Low/Mid/High - {band}. Provide a budget for tailored splits.")
        lines.append("")
        if info and info.get("attractions"):
            lines.append("Top Attractions")
            lines.append(_format_bullets(info["attractions"]))
            lines.append("")
        lines.append("3-5 Day Itinerary")
        for dday in itin:
            lines.append(f"Day {dday['day']}")
            lines.append(f"Morning: {dday['morning'] or 'Explore a landmark'}")
            lines.append(f"Afternoon: {dday['afternoon'] or 'Local food + market'}")
            lines.append(f"Evening: {dday['evening'] or 'Waterfront walk / cultural show'}")
            lines.append("Tips: " + (dday.get("tips") or "Carry water, plan buffer, check opening hours."))
            lines.append("")
        lines.append("Travel Tips")
        tips = []
        if "budget" in modes:
            tips.append("Use public transport and free attractions; eat at local joints.")
        if "luxury" in modes:
            tips.append("Book premium stays, fine dining, and private transfers.")
        if "adventure" in modes:
            tips.append("Add trekking, cycling, or water sports with certified operators.")
        if "cultural" in modes:
            tips.append("Visit museums, heritage walks, and local performances.")
        if "accessible" in modes:
            tips.append("Prefer wheelchair-friendly venues; confirm ramps and elevators.")
        if not tips:
            tips.append("Buy tickets online, start early, and check local advisories.")
        lines.append("- " + " - ".join(tips))
        lines.append("")
        lines.append("Optional Upgrades")
        lines.append("Private guide, express entry tickets, curated food tour, or sunset cruise (availability varies).")
        return "\n".join(lines)

    def _food_response(dest, budget, modes):
        info = _city_info(dest)
        lines = []
        lines.append("Overview")
        lines.append(f"Food trail in {dest.title() if dest else 'the city'}.")
        lines.append("")
        lines.append("Why Visit")
        lines.append(info["why"] if info else _why_visit(dest))
        lines.append("")
        lines.append("Best Time to Visit")
        lines.append(info["best_time"] if info else _best_time_for_city(dest))
        lines.append("")
        lines.append("Budget Estimate")
        lines.append(_budget_band(budget))
        lines.append("")
        lines.append("Food Spots")
        lines.append(_format_bullets(info["food_spots"] if info else ["Street food zone", "Popular local eateries", "Regional specialties"]))
        lines.append("")
        lines.append("Travel Tips")
        lines.append("- Prefer hygienic vendors, carry cash, and check timings.")
        lines.append("")
        lines.append("Optional Upgrades")
        lines.append("Guided food tour or chef-led tasting.")
        return "\n".join(lines)

    def _get_place_stats(dest_name, lat=None, lng=None):
        if dest_name:
            places = fetch_otm_places(dest_name, limit=15)
        else:
            places = fetch_otm_places_by_coords(lat, lng, limit=15)
        valid = []
        for p in places:
            loc = p.get("location") or {}
            if "lat" in loc and "lng" in loc:
                try:
                    valid.append({
                        "name": p.get("placeName") or "Place",
                        "lat": float(loc["lat"]),
                        "lng": float(loc["lng"]),
                    })
                except Exception:
                    continue

        if not valid:
            return None

        stats = {
            "count": len(valid),
            "nearest_km": None,
            "avg_km": None,
            "est_hours": None,
        }

        if lat is not None and lng is not None:
            dists = [haversine(lat, lng, p["lat"], p["lng"]) for p in valid]
            if dists:
                stats["nearest_km"] = round(min(dists), 1)
                stats["avg_km"] = round(sum(dists) / len(dists), 1)
                # Rough local travel estimate (walking + local transport mix)
                stats["est_hours"] = round((sum(dists) * 0.08) + (len(valid) * 0.5), 1)
        else:
            c_lat = sum(p["lat"] for p in valid) / len(valid)
            c_lng = sum(p["lng"] for p in valid) / len(valid)
            spread = [haversine(c_lat, c_lng, p["lat"], p["lng"]) for p in valid]
            if spread:
                avg_spread = sum(spread) / len(spread)
                stats["avg_km"] = round(avg_spread, 1)
                stats["est_hours"] = round((len(valid) * 0.45) + max(0.5, avg_spread * 0.35), 1)

        return stats


    def _distance_to_place(place_name, lat, lng):
        if not OPENTRIPMAP_API_KEY or not place_name:
            return None
        if lat is None or lng is None:
            return None
        try:
            lat = float(lat)
            lng = float(lng)
        except Exception:
            return None

        try:
            q = quote_plus(place_name.strip())
            geo_url = f"https://api.opentripmap.com/0.1/en/places/geoname?name={q}&apikey={OPENTRIPMAP_API_KEY}"
            geo = fetch_json(geo_url)
            if not geo or "lat" not in geo or "lon" not in geo:
                return None
            d = haversine(lat, lng, float(geo["lat"]), float(geo["lon"]))
            return round(d, 1)
        except Exception:
            return None

    def _chatty_fallback(msg, dest_name, days_val, budget_val):
        msg_l = msg.lower().strip()
        tokens = re.findall(r"[a-zA-Z]+", msg_l)

        greeting_words = {"hi", "hii", "hello", "hey", "how", "yo", "sup"}
        is_greeting = msg_l in greeting_words or (len(tokens) <= 2 and all(tok in greeting_words for tok in tokens))

        wants_itin = any(k in msg_l for k in ["itinerary", "day plan", "plan", "schedule"])
        wants_food = any(k in msg_l for k in ["food", "eat", "restaurant", "cafe"])
        wants_attr = any(k in msg_l for k in ["attraction", "places to visit", "things to do", "sightseeing"])
        wants_distance = any(k in msg_l for k in ["distance", "how far", "near", "nearby", "long", "travel time", "how much time", "km", "hours", "minutes"])
        wants_count = any(k in msg_l for k in ["how many", "count", "number of places", "how much place"])

        place_match = re.search(r"(?:how\s+far\s+is|distance\s+to|how\s+far\s+to)\s+([a-zA-Z0-9 ,\-']+)", msg_l)
        if place_match:
            place_name = place_match.group(1).strip(" .?!")
            if user_lat is None or user_lng is None:
                return "Please allow location access so I can calculate distance from your current location."
            d = _distance_to_place(place_name, user_lat, user_lng)
            if d is not None:
                return f"{place_name.title()} is about {d} km from your current location."
            return f"I could not locate {place_name.title()} right now. Try adding city name, like 'Marine Drive Mumbai'."

        if is_greeting and not (wants_itin or wants_food or wants_attr or wants_distance or wants_count):
            if dest_name:
                return (
                    f"Hi! I can help with {dest_name.title()}. "
                    "Ask your exact travel question."
                )
            return (
                "Hi! I am your travel assistant. "
                "Share destination and your exact question."
            )

        if not dest_name and (wants_itin or wants_food or wants_attr or wants_distance or wants_count):
            if user_lat is None or user_lng is None:
                return "Please share destination or allow location access."

        if (wants_distance or wants_count):
            stats = _get_place_stats(dest_name, user_lat, user_lng)
            if not stats:
                place_label = dest_name.title() if dest_name else "your current location"
                return (
                    f"I could not fetch reliable place stats for {place_label} right now. "
                    "Try again in a moment, or ask for a day-wise itinerary."
                )

            if dest_name:
                parts = [f"For {dest_name.title()}, I found around {stats['count']} notable places."]
            else:
                parts = [f"From your current location, I found around {stats['count']} nearby places."]
            if stats.get("nearest_km") is not None:
                parts.append(f"Nearest is about {stats['nearest_km']} km from your location.")
            if stats.get("avg_km") is not None:
                parts.append(f"Average distance is about {stats['avg_km']} km.")
            if stats.get("est_hours") is not None:
                parts.append(f"A practical visit loop is roughly {stats['est_hours']} hours.")
            parts.append("Ask if you want an itinerary.")
            return " ".join(parts)

        if wants_food and dest_name:
            return _food_response(dest_name, budget_val, set())
        if wants_food and not dest_name and user_lat is not None and user_lng is not None:
            stats = _get_place_stats(None, user_lat, user_lng)
            if stats:
                return (
                    f"From your current location, I can see around {stats['count']} nearby places. "
                    "Tell me destination for detailed food spots."
                )

        if wants_attr:
            if dest_name:
                info = _city_info(dest_name)
                if info and info.get("attractions"):
                    return "Top Attractions\n" + ("\n- " + "\n- ".join(info["attractions"]))
            stats = _get_place_stats(dest_name, user_lat, user_lng)
            if stats:
                if dest_name:
                    return (
                        f"I found around {stats['count']} places in {dest_name.title()}. "
                        "Ask for an itinerary if needed."
                    )
                return (
                    f"I found around {stats['count']} nearby places from your current location. "
                    "Ask for an itinerary if needed."
                )

        if wants_itin or days_val or budget_val:
            return _structured_response(dest_name, days_val, budget_val, set(), language)

        if dest_name:
            return (
                f"I can plan {dest_name.title()} for you. "
                "Ask your exact travel question."
            )

        if user_lat is not None and user_lng is not None:
            return (
                "I can use your current location. Ask your exact travel question."
            )
        return "Tell me destination and your exact travel question."

    dest, d_days, d_budget, d_modes = _extract_params(message, destination)
    fallback_reply = _chatty_fallback(message, dest, d_days, d_budget)
    return jsonify({"reply": fallback_reply, "source": "fallback", "llm_error": llm_err})


@ai_features.route("/ai/voice-assistant", methods=["POST"])
def voice_assistant():
    data = request.get_json(silent=True) or {}
    transcript = (data.get("message") or "").strip()
    language = normalize_lang((data.get("language") or "en").split("-")[0].lower())
    current_path = (data.get("current_path") or "").strip()
    if not transcript:
        return jsonify({"error": "Message is required"}), 400

    def _local_voice_parse(text):
        t = (text or "").lower().strip()
        actions = []
        reply = ""
        def _val_after(regex):
            m = re.search(regex, t, re.I)
            return (m.group(1).strip() if m and m.group(1) else "")
        if not t:
            return [], ""
        if t == "home" or "go home" in t:
            actions.append({"type": "navigate", "path": "/"})
            reply = "Opening home."
            return actions, reply
        if "open maps" in t or "maps" in t:
            actions.append({"type": "navigate", "path": "/maps"})
            reply = "Opening maps."
            return actions, reply
        if "open admin" in t:
            actions.append({"type": "navigate", "path": "/admin"})
            reply = "Opening admin."
            return actions, reply
        if "open upload" in t or "upload" in t:
            actions.append({"type": "navigate", "path": "/upload"})
            reply = "Opening upload."
            return actions, reply
        if "guardian requests" in t:
            actions.append({"type": "navigate", "path": "/guardian-request"})
            reply = "Opening guardian requests."
            return actions, reply
        if "live tracking" in t:
            actions.append({"type": "navigate", "path": "/guardian-tracking"})
            reply = "Opening live tracking."
            return actions, reply
        if "ai chat" in t:
            actions.append({"type": "navigate", "path": "/ai-chat"})
            reply = "Opening AI chat."
            return actions, reply
        if "ai itinerary" in t or "trip planner" in t:
            actions.append({"type": "navigate", "path": "/ai-itinerary"})
            reply = "Opening itinerary."
            return actions, reply
        if "ai sentiment" in t:
            actions.append({"type": "navigate", "path": "/ai-sentiment"})
            reply = "Opening sentiment."
            return actions, reply
        if "itinerary" in t:
            actions.append({"type": "navigate", "path": "/itinerary"})
            reply = "Opening itinerary."
            return actions, reply
        if "profile" in t:
            actions.append({"type": "navigate", "path": "/profile"})
            reply = "Opening profile."
            return actions, reply
        if "accessibility page" in t:
            actions.append({"type": "navigate", "path": "/accessibility"})
            reply = "Opening accessibility."
            return actions, reply
        if "accessibility" in t or "color blind" in t:
            actions.append({"type": "toggle_accessibility"})
            reply = "Toggling accessibility."
            return actions, reply
        if "speech on" in t:
            actions.append({"type": "toggle_speech", "enabled": True})
            reply = "Speech is on."
            return actions, reply
        if "speech off" in t:
            actions.append({"type": "toggle_speech", "enabled": False})
            reply = "Speech is off."
            return actions, reply
        if "open quick menu" in t:
            actions.append({"type": "toggle_quick_menu", "open": True})
            reply = "Opening quick menu."
            return actions, reply
        if "close quick menu" in t:
            actions.append({"type": "toggle_quick_menu", "open": False})
            reply = "Closing quick menu."
            return actions, reply
        if "open cart" in t:
            actions.append({"type": "navigate", "path": "/cart"})
            reply = "Opening cart."
            return actions, reply
        if "open place" in t:
            name = _val_after(r"open place\s+(.+)")
            if name:
                actions.append({"type": "voice_event", "event_type": "open-place", "name": name})
                reply = f"Opening {name}."
            else:
                reply = "Please say the place name."
            return actions, reply
        if "close place" in t:
            actions.append({"type": "voice_event", "event_type": "close-place"})
            reply = "Closed place panel."
            return actions, reply
        if "add to cart" in t:
            name = _val_after(r"add\s+(.+?)\s+to cart") or _val_after(r"add to cart\s+(.+)")
            payload = {"type": "voice_event", "event_type": "add-to-cart"}
            if name:
                payload["name"] = name
                reply = f"Added {name} to cart."
            else:
                reply = "Added to cart."
            actions.append(payload)
            return actions, reply
        if "remove from cart" in t:
            name = _val_after(r"remove\s+(.+?)\s+from cart") or _val_after(r"remove from cart\s+(.+)")
            payload = {"type": "voice_event", "event_type": "remove-from-cart"}
            if name:
                payload["name"] = name
                reply = f"Removed {name} from cart."
            else:
                reply = "Removed from cart."
            actions.append(payload)
            return actions, reply
        if "generate itinerary" in t or "create itinerary" in t:
            actions.append({"type": "voice_event", "event_type": "generate-itinerary"})
            reply = "Generating itinerary."
            return actions, reply
        if "save itinerary" in t:
            actions.append({"type": "voice_event", "event_type": "save-itinerary"})
            reply = "Saving itinerary."
            return actions, reply
        if "use current location" in t:
            actions.append({"type": "voice_event", "event_type": "use-current-location"})
            reply = "Using current location."
            return actions, reply
        if "set destination" in t:
            value = _val_after(r"set destination(?: to)?\s+(.+)")
            if value:
                actions.append({"type": "voice_event", "event_type": "set-destination", "value": value})
                reply = "Destination set."
                return actions, reply
        if "set budget" in t:
            value = _val_after(r"set budget(?: to)?\s+(.+)")
            if value:
                actions.append({"type": "voice_event", "event_type": "set-budget", "value": value})
                reply = "Budget set."
                return actions, reply
        if "set days" in t:
            value = _val_after(r"set days(?: to)?\s+(.+)")
            if value:
                actions.append({"type": "voice_event", "event_type": "set-days", "value": value})
                reply = "Days set."
                return actions, reply
        if "set travel type" in t:
            value = _val_after(r"set travel type(?: to)?\s+(.+)")
            if value:
                actions.append({"type": "voice_event", "event_type": "set-travel-type", "value": value})
                reply = "Travel type set."
                return actions, reply
        if "set interests" in t:
            value = _val_after(r"set interests(?: to)?\s+(.+)")
            if value:
                actions.append({"type": "voice_event", "event_type": "set-interests", "value": value})
                reply = "Interests set."
                return actions, reply
        if "set currency" in t:
            value = _val_after(r"set currency(?: to)?\s+(.+)")
            if value:
                actions.append({"type": "voice_event", "event_type": "set-currency", "value": value})
                reply = "Currency set."
                return actions, reply
        if "logout" in t:
            actions.append({"type": "logout"})
            reply = "Logging out."
            return actions, reply
        if t.startswith("help"):
            reply = "Say Hey PathEase, then commands like go home, open maps, open itinerary, open place Gateway of India, add to cart, generate itinerary, save itinerary, speech on, speech off, open cart, or logout."
            return [], reply
        return [], ""

    system_prompt = (
        "You are PathEase Voice Assistant Command Router.\n"
        "Convert user speech into STRICT JSON with fields: reply, actions.\n"
        "actions is an array of objects.\n"
        "Allowed action types:\n"
        "- navigate: {\"type\":\"navigate\",\"path\":\"/...\"}\n"
        "- toggle_speech: {\"type\":\"toggle_speech\",\"enabled\":true|false}\n"
        "- toggle_quick_menu: {\"type\":\"toggle_quick_menu\",\"open\":true|false}\n"
        "- toggle_accessibility: {\"type\":\"toggle_accessibility\"}\n"
        "- voice_event: {\"type\":\"voice_event\",\"event_type\":\"open-place|close-place|add-to-cart|remove-from-cart|generate-itinerary|save-itinerary|set-destination|set-days|set-budget|set-travel-type|set-interests|set-currency|use-current-location\",\"name\":\"optional\",\"value\":\"optional\"}\n"
        "- logout: {\"type\":\"logout\"}\n"
        "Rules:\n"
        "1) Output ONLY JSON.\n"
        "2) Keep reply short and natural.\n"
        "3) If command unknown, return empty actions and a helpful reply.\n"
        "4) For open place/add/remove commands include place name if spoken.\n"
        "5) For set commands include value.\n"
        "6) If user says help, include no dangerous actions.\n"
    )

    user_prompt = (
        f"Language: {language}\n"
        f"Current route: {current_path or '/'}\n"
        f"User speech: {transcript}\n"
        "Return JSON now."
    )

    llm_text, llm_err = call_llm(system_prompt, user_prompt)
    parsed = _extract_first_json_object(llm_text or "")
    if isinstance(parsed, dict):
        reply = str(parsed.get("reply") or "").strip()
        actions = parsed.get("actions")
        if not isinstance(actions, list):
            actions = []
        sanitized_actions = []
        allowed_types = {
            "navigate",
            "toggle_speech",
            "toggle_quick_menu",
            "toggle_accessibility",
            "voice_event",
            "logout",
        }
        allowed_paths = {
            "/",
            "/maps",
            "/admin",
            "/admin/pending",
            "/admin/users",
            "/admin/analytics",
            "/upload",
            "/guardian-request",
            "/guardian-tracking",
            "/ai-chat",
            "/ai-itinerary",
            "/ai-sentiment",
            "/search-results",
            "/itinerary",
            "/profile",
            "/accessibility",
            "/cart",
            "/login",
        }
        allowed_voice_events = {
            "open-place",
            "close-place",
            "add-to-cart",
            "remove-from-cart",
            "generate-itinerary",
            "save-itinerary",
            "set-destination",
            "set-days",
            "set-budget",
            "set-travel-type",
            "set-interests",
            "set-currency",
            "use-current-location",
        }

        for action in actions[:8]:
            if not isinstance(action, dict):
                continue
            a_type = str(action.get("type") or "").strip()
            if a_type not in allowed_types:
                continue
            if a_type == "navigate":
                path = str(action.get("path") or "").strip()
                if path in allowed_paths:
                    sanitized_actions.append({"type": "navigate", "path": path})
                continue
            if a_type == "toggle_speech":
                sanitized_actions.append({"type": "toggle_speech", "enabled": bool(action.get("enabled"))})
                continue
            if a_type == "toggle_quick_menu":
                sanitized_actions.append({"type": "toggle_quick_menu", "open": bool(action.get("open"))})
                continue
            if a_type == "toggle_accessibility":
                sanitized_actions.append({"type": "toggle_accessibility"})
                continue
            if a_type == "logout":
                sanitized_actions.append({"type": "logout"})
                continue
            if a_type == "voice_event":
                event_type = str(action.get("event_type") or "").strip()
                if event_type in allowed_voice_events:
                    payload = {"type": "voice_event", "event_type": event_type}
                    if action.get("name") is not None:
                        payload["name"] = str(action.get("name"))
                    if action.get("value") is not None:
                        payload["value"] = str(action.get("value"))
                    sanitized_actions.append(payload)
                continue

        if reply:
            return jsonify({"reply": reply, "actions": sanitized_actions, "source": "nvidia"})
        if sanitized_actions:
            return jsonify({"reply": "Done.", "actions": sanitized_actions, "source": "nvidia"})

    local_actions, local_reply = _local_voice_parse(transcript)
    if local_actions or local_reply:
        return jsonify({"reply": (local_reply or "Done."), "actions": local_actions, "source": "local"})
    return jsonify({"reply": "Sorry, I could not process that command clearly. Please try again.", "actions": [], "source": "fallback", "llm_error": llm_err})

@ai_features.route("/ai/sentiment", methods=["POST"])
def sentiment():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Text is required"}), 400
    return jsonify(_basic_sentiment(text))

# ---------------- FREE AI (UNCHANGED) ----------------
def call_llm(system_prompt, user_prompt):
    api_key = (
        read_env_value("NVIDIA_API_KEY", "").strip()
        or read_env_value("LLM_API_KEY", "").strip()
    )
    api_base = (
        read_env_value("NVIDIA_API_BASE", "").strip()
        or "https://integrate.api.nvidia.com/v1/chat/completions"
    )
    model = (
        read_env_value("NVIDIA_MODEL", "").strip()
        or "meta/llama-4-maverick-17b-128e-instruct"
    )

    if not api_key:
        return None, "ENV missing"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 400,
    }

    try:
        req = Request(api_base, data=json.dumps(payload).encode(), headers=headers)
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            if content:
                return content, None
            return None, "Empty LLM response"
    except Exception as e:
        return None, str(e)

def generate_day_tips(itinerary, destination, budget, travel_type, interests, currency):
    # Try LLM for richer tips; fallback to simple tips if unavailable
    system_prompt = "You are a helpful local travel guide."
    days_payload = [
        {
            "day": d.get("day"),
            "places": [d.get("morning"), d.get("afternoon"), d.get("evening")],
        }
        for d in itinerary
    ]
    user_prompt = (
        "Create short, practical tips (1-2 sentences) for each day.\n"
        f"Destination: {destination}\n"
        f"Travel type: {travel_type}\n"
        f"Budget: {budget} {currency}\n"
        f"Interests: {', '.join(interests) if interests else 'general'}\n"
        "Return JSON only: {\"tips\": [\"tip day1\", \"tip day2\", ...]}\n"
        f"Days: {json.dumps(days_payload)}"
    )

    content, err = call_llm(system_prompt, user_prompt)
    if content:
        try:
            parsed = json.loads(content)
            tips = parsed.get("tips", [])
            if isinstance(tips, list) and len(tips) >= len(itinerary):
                return tips[: len(itinerary)]
        except Exception:
            pass

    # Fallback tips
    return [
        "Start early, carry water, and keep some buffer time between places."
        for _ in itinerary
    ]


def refine_itinerary_schedule(itinerary, destination, travel_type, interests):
    """Try LLM to refine morning/afternoon/evening slots with natural trip flow."""
    system_prompt = "You are an expert travel planner. Keep outputs concise and practical."
    days_payload = [
        {
            "day": d.get("day"),
            "morning": d.get("morning"),
            "afternoon": d.get("afternoon"),
            "evening": d.get("evening"),
            "stops": [s.get("name") for s in (d.get("stops") or []) if s.get("name")],
        }
        for d in itinerary
    ]
    user_prompt = (
        "Refine this day-wise itinerary. Ensure each day has non-empty morning, afternoon, and evening.\n"
        "Keep activities realistic and location-aware, do not add unrelated cities.\n"
        "Return JSON only: {\"days\": [{\"day\":1,\"morning\":\"...\",\"afternoon\":\"...\",\"evening\":\"...\"}, ...]}\n"
        f"Destination: {destination}\n"
        f"Travel type: {travel_type}\n"
        f"Interests: {', '.join(interests) if interests else 'general'}\n"
        f"Input days: {json.dumps(days_payload)}"
    )

    content, _err = call_llm(system_prompt, user_prompt)
    if not content:
        return None

    try:
        parsed = json.loads(content)
        days = parsed.get("days", [])
        if not isinstance(days, list) or len(days) < len(itinerary):
            return None
        out = []
        for i in range(len(itinerary)):
            d = days[i] if i < len(days) else {}
            out.append({
                "morning": (d.get("morning") or "").strip(),
                "afternoon": (d.get("afternoon") or "").strip(),
                "evening": (d.get("evening") or "").strip(),
            })
        return out
    except Exception:
        return None

# ---------------- NEW: PLACE SELECTION LOGIC ----------------
def get_places(data):
    selected_places = data.get("selected_places", [])
    destination = (data.get("destination") or "").strip()
    lat = data.get("lat")
    lng = data.get("lng")

    if places_collection is None:
        return []

    # 1) Selected places (highest priority)
    if selected_places:
        try:
            return list(places_collection.find(
                {"placeName": {"$in": selected_places}, "approved": True},
                {"_id": 0}
            ))
        except Exception:
            print("ERROR: get_places selected_places query failed")
            print(traceback.format_exc())
            return []

    # 2) Destination-based (use city field first, then fallback to name/description match)
    try:
        all_places = list(places_collection.find({"approved": True}, {"_id": 0}))
    except Exception:
        print("ERROR: get_places approved query failed")
        print(traceback.format_exc())
        return []

    if destination:
        dest_lower = destination.lower()
        dest_places = [
            p for p in all_places
            if (p.get("city") or "").lower() == dest_lower
            or dest_lower in (p.get("placeName") or "").lower()
            or dest_lower in (p.get("description") or "").lower()
        ]
    else:
        dest_places = all_places

    # If nothing matches destination, fallback to all approved places
    places = dest_places if dest_places else all_places

    external = fetch_otm_places(destination) if destination else []
    if external:
        places = places + external

    # 3) Distance-based sorting
    ref_lat = None
    ref_lng = None
    if lat is not None and lng is not None:
        ref_lat, ref_lng = float(lat), float(lng)
    else:
        coords = []
        for p in places:
            loc = p.get("location") or {}
            if "lat" in loc and "lng" in loc:
                try:
                    coords.append((float(loc["lat"]), float(loc["lng"])))
                except Exception:
                    continue
        if coords:
            ref_lat = sum(c[0] for c in coords) / len(coords)
            ref_lng = sum(c[1] for c in coords) / len(coords)

    for p in places:
        loc = p.get("location") or {}
        if ref_lat is not None and ref_lng is not None and "lat" in loc and "lng" in loc:
            p["distance"] = haversine(ref_lat, ref_lng, float(loc["lat"]), float(loc["lng"]))
        else:
            p["distance"] = 99999

    places.sort(key=lambda x: x.get("distance", 99999))
    return places

# ---------------- FALLBACK ITINERARY (UNCHANGED) ----------------
def fallback_itinerary(destination, days, budget, lang):
    label = DAY_LABELS[lang]
    per_day = budget / days if budget else 0

    return [{
        "day": i,
        "title": f"{label} {i}: Explore {destination}",
        "morning": "Visit a popular attraction",
        "afternoon": "Try local food",
        "evening": "Relax and explore markets",
        "tips": "Carry water and ID",
        "est_cost": round(per_day, 2)
    } for i in range(1, days + 1)]

# ---------------- ROUTE (EXTENDED, NOT REPLACED) ----------------
@ai_features.route("/ai/trip-planner", methods=["POST"])
def trip_planner():
    try:
        data = request.get_json(silent=True) or {}
        print("DEBUG BODY:", data)
    
        destination = (data.get("destination") or "").strip()
        if not destination:
            return jsonify({"error": "Destination required"}), 400
    
        days = safe_int(data.get("days", 1))
        budget = safe_float(data.get("budget", 0))
        lang = normalize_lang(data.get("language", "en"))
        currency = (data.get("currency") or "INR").upper()
        travel_type = (data.get("travel_type") or "leisure").strip()
        interests = data.get("interests") or []
    
        if data.get("lat") is None or data.get("lng") is None:
            return jsonify({"error": "Current location required (lat/lng)."}), 400
        try:
            _lat = float(data.get("lat"))
            _lng = float(data.get("lng"))
        except Exception:
            return jsonify({"error": "Invalid lat/lng. Must be numbers."}), 400
    
        # ðŸ”¥ NEW LOGIC
        places = get_places(data)
    
        if places:
            # distribute all places across days, distance-sorted
            per_day = max(1, math.ceil(len(places) / days))
            itinerary = []
            idx = 0
    
            def fmt_place(p):
                name = p.get("placeName", "Place")
                dist = p.get("distance")
                if dist is not None and dist < 99999:
                    return f"{name} ({dist:.1f} km)"
                return name
    
            for d in range(1, days + 1):
                day_places = places[idx: idx + per_day]
                idx += per_day
    
                day_cost = round(budget / days, 2) if budget else 0
                day_cost_converted = convert_amount(day_cost, "INR", currency) if budget else 0
    
                # distance for this day (from current location if provided)
                day_distance = None
                if data.get("lat") is not None and data.get("lng") is not None:
                    day_distance = path_distance_km(float(data["lat"]), float(data["lng"]), day_places)
    
                stops = []
                for p in day_places[:3]:
                    loc = p.get("location") or {}
                    stop_lat = None
                    stop_lng = None
                    try:
                        if "lat" in loc:
                            stop_lat = float(loc["lat"])
                        if "lng" in loc:
                            stop_lng = float(loc["lng"])
                    except Exception:
                        stop_lat = None
                        stop_lng = None
                    stops.append({
                        "name": p.get("placeName", "Place"),
                        "lat": stop_lat,
                        "lng": stop_lng,
                        "distance_km": round(p.get("distance", 0), 2) if p.get("distance") is not None else None
                    })
    
                morning_slot = fmt_place(day_places[0]) if len(day_places) > 0 else f"City highlights in {destination}"
                afternoon_slot = fmt_place(day_places[1]) if len(day_places) > 1 else f"Local food trail and museums in {destination}"
                evening_slot = fmt_place(day_places[2]) if len(day_places) > 2 else f"Sunset walk and market visit in {destination}"

                itinerary.append({
                    "day": d,
                    "title": f"{DAY_LABELS[lang]} {d}: Explore {destination}",
                    "morning": morning_slot,
                    "afternoon": afternoon_slot,
                    "evening": evening_slot,
                    "tips": "",
                    "est_cost": day_cost_converted,
                    "currency": currency,
                    "currency_symbol": CURRENCY_SYMBOLS.get(currency, currency + " "),
                    "day_distance_km": day_distance,
                    "stops": stops
                })
    
            refined_slots = refine_itinerary_schedule(
                itinerary,
                destination,
                travel_type,
                interests,
            )
            if refined_slots:
                for i, slots in enumerate(refined_slots):
                    if i < len(itinerary):
                        if slots.get("morning"):
                            itinerary[i]["morning"] = slots["morning"]
                        if slots.get("afternoon"):
                            itinerary[i]["afternoon"] = slots["afternoon"]
                        if slots.get("evening"):
                            itinerary[i]["evening"] = slots["evening"]

            tips_list = generate_day_tips(
                itinerary,
                destination,
                budget,
                travel_type,
                interests,
                currency,
            )
            for i, t in enumerate(tips_list):
                if i < len(itinerary):
                    itinerary[i]["tips"] = t
    
            start_point = None
            if data.get("lat") is not None and data.get("lng") is not None:
                start_point = {
                    "lat": float(data["lat"]),
                    "lng": float(data["lng"]),
                    "label": data.get("location_label") or "Current Location",
                }
    
            nearest = None
            if places and places[0].get("distance", 99999) < 99999:
                nearest = {
                    "placeName": places[0].get("placeName"),
                    "distance_km": round(places[0].get("distance", 0), 2)
                }
    
            total_distance = None
            if start_point:
                total_distance = path_distance_km(start_point["lat"], start_point["lng"], places)
    
            return jsonify({
                "source": "db",
                "itinerary": itinerary,
                "places_used": [p.get("placeName") for p in places],
                "start_from": start_point,
                "nearest_place": nearest,
                "total_distance_km": total_distance,
                "cost_breakdown": split_budget(convert_amount(budget, "INR", currency), currency)
            })
    
        # ðŸ” EXISTING AI FALLBACK
        system_prompt = "You are a travel planner."
        user_prompt = f"Plan a {days}-day trip to {destination} with budget {budget}."
    
        content, err = call_llm(system_prompt, user_prompt)
        if content:
            return jsonify({"source": "llm", "plan": content})
    
        return jsonify({
            "source": "fallback",
            "itinerary": fallback_itinerary(destination, days, budget, lang),
            "error": err
        })
    except Exception as e:
        print("ERROR: /ai/trip-planner exception")
        print(traceback.format_exc())
        return jsonify({"error": "Internal server error", "details": str(e)}), 500
