import json
import os
import re
import uuid
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import math
import time

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
        parts = urlsplit(uri)
        if parts.scheme not in ("mongodb", "mongodb+srv"):
            return uri
        if "@" not in parts.netloc:
            return uri
        userinfo, hostinfo = parts.netloc.rsplit("@", 1)
        if ":" not in userinfo:
            return uri
        user, pwd = userinfo.split(":", 1)
        user = quote_plus(unquote_plus(user))
        pwd = quote_plus(unquote_plus(pwd))
        netloc = f"{user}:{pwd}@{hostinfo}"
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
            lat = float(loc["lat"])
            lng = float(loc["lng"])
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
        return list(places_collection.find(
            {"placeName": {"$in": selected_places}, "approved": True},
            {"_id": 0}
        ))

    # 2) Destination-based (use city field first, then fallback to name/description match)
    all_places = list(places_collection.find({"approved": True}, {"_id": 0}))

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
                coords.append((float(loc["lat"]), float(loc["lng"])))
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
                    stops.append({
                        "name": p.get("placeName", "Place"),
                        "lat": float(loc["lat"]) if "lat" in loc else None,
                        "lng": float(loc["lng"]) if "lng" in loc else None,
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
        return jsonify({"error": "Internal server error", "details": str(e)}), 500
