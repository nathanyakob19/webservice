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
DAY_LABELS = {"en": "Day", "hi": "दिन", "mr": "दिवस"}
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

@ai_features.route("/ai/guide-chat", methods=["POST"])
def guide_chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    destination = (data.get("destination") or "").strip()
    language = normalize_lang(data.get("language") or "en")
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
        "5) If user mentions family, couple, solo, student, luxury, or adventure — personalize suggestions.\n"
        "6) Mention visa considerations only if relevant.\n"
        "7) Highlight sustainable and eco-friendly options where possible.\n"
        "8) If data is approximate, state it clearly.\n"
        "9) Keep responses clear, structured, and useful.\n"
        "10) If details are missing (budget, duration, country), ask one clarifying question only.\n\n"
        "SPECIAL MODES:\n"
        "If user says: Luxury → focus on premium hotels, fine dining, private transport. Budget → hostels, public transport, free attractions. Adventure → trekking, water sports, wildlife. Cultural → museums, heritage sites, local traditions. Accessible travel → wheelchair-friendly info. Hidden gems → less crowded alternatives.\n\n"
        "OUTPUT STRUCTURE:\n"
        "1) Overview\n"
        "2) Why Visit\n"
        "3) Best Time to Visit\n"
        "4) Budget Estimate\n"
        "5) 3–5 Day Itinerary (if applicable)\n"
        "6) Travel Tips\n"
        "7) Optional Upgrades\n\n"
        "Respond in the requested language code, be concise but complete, and tailor answers to any destination provided."
    )
    user_prompt = (
        f"Language: {language}\n"
        f"Destination: {destination or 'Not specified'}\n"
        f"User message: {message}\n"
    )
    content, _err = call_llm(system_prompt, user_prompt)
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
            return "Nov–Feb (cooler, drier); Jun–Sep monsoon with heavy rain"
        if "goa" in n:
            return "Nov–Feb for beaches; Jun–Sep monsoon for lush landscapes"
        if "delhi" in n:
            return "Oct–Mar (pleasant); Apr–Jun very hot"
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
                "best_time": "Nov–Feb; expect heavy monsoon Jun–Sep",
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
            return "Low: < 5,000; Mid: 5,000–15,000; High: > 15,000 (approx, INR)"
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
            lines.append(f"Range: Low/Mid/High — {band}. Provide a budget for tailored splits.")
        lines.append("")
        if info and info.get("attractions"):
            lines.append("Top Attractions")
            lines.append(_format_bullets(info["attractions"]))
            lines.append("")
        lines.append("3–5 Day Itinerary")
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
        lines.append("• " + " • ".join(tips))
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
        lines.append("• Prefer hygienic vendors, carry cash, and check timings.")
        lines.append("")
        lines.append("Optional Upgrades")
        lines.append("Guided food tour or chef-led tasting.")
        return "\n".join(lines)

    dest, d_days, d_budget, d_modes = _extract_params(message, destination)
    want_itin = any(k in message.lower() for k in ["itinerary", "day plan", "plan"])
    want_food = "food" in message.lower()
    want_attr = "attraction" in message.lower()

    if want_itin or d_days or d_budget:
        return jsonify({"reply": _structured_response(dest, d_days, d_budget, d_modes, language)})
    if want_food and dest:
        return jsonify({"reply": _food_response(dest, d_budget, d_modes)})
    if want_attr and dest:
        info = _city_info(dest)
        if info and info.get("attractions"):
            text = "Top Attractions\n" + ("\n- " + "\n- ".join(info["attractions"]))
            return jsonify({"reply": text})
    return jsonify({"reply": _structured_response(dest, None, d_budget, d_modes, language)})

@ai_features.route("/ai/sentiment", methods=["POST"])
def sentiment():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Text is required"}), 400
    return jsonify(_basic_sentiment(text))

# ---------------- FREE AI (UNCHANGED) ----------------
def call_llm(system_prompt, user_prompt):
    api_key = os.environ.get("LLM_API_KEY")
    api_base = os.environ.get("LLM_API_BASE")

    if not api_key or not api_base:
        return None, "ENV missing"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "inputs": f"{system_prompt}\n\n{user_prompt}",
        "parameters": {"max_new_tokens": 400}
    }

    try:
        req = Request(api_base, data=json.dumps(payload).encode(), headers=headers)
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            return data[0]["generated_text"], None
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
    
        # 🔥 NEW LOGIC
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
    
                itinerary.append({
                    "day": d,
                    "title": f"{DAY_LABELS[lang]} {d}: Explore {destination}",
                    "morning": fmt_place(day_places[0]) if len(day_places) > 0 else "",
                    "afternoon": fmt_place(day_places[1]) if len(day_places) > 1 else "",
                    "evening": fmt_place(day_places[2]) if len(day_places) > 2 else "",
                    "tips": "",
                    "est_cost": day_cost_converted,
                    "currency": currency,
                    "currency_symbol": CURRENCY_SYMBOLS.get(currency, currency + " "),
                    "day_distance_km": day_distance,
                    "stops": stops
                })
    
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
    
        # 🔁 EXISTING AI FALLBACK
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
