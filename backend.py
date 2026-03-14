from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from pymongo import MongoClient
from urllib.parse import quote_plus, urlsplit, urlunsplit, unquote_plus
from werkzeug.utils import secure_filename
from bson import ObjectId, Binary
from bson.errors import InvalidId
import bcrypt
import jwt
import os
import json
import math
import re
from uuid import uuid4
from datetime import datetime, timedelta
from ai_features import ai_features

# ---------------- APP SETUP ----------------
app = Flask(__name__)

def _cors_origins():
    raw = (os.environ.get("PATHEASE_CORS_ORIGINS", "") or "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "https://patheasee.netlify.app",
        "http://localhost:3000",
    ]


ALLOWED_CORS_ORIGINS = _cors_origins()
CORS(
    app,
    resources={r"/*": {"origins": ALLOWED_CORS_ORIGINS}},
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


def _apply_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin and origin in ALLOWED_CORS_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Max-Age"] = "86400"
    return response


@app.before_request
def handle_cors_preflight():
    if request.method == "OPTIONS":
        return _apply_cors_headers(app.make_default_options_response())


@app.after_request
def add_cors_headers(response):
    return _apply_cors_headers(response)


# ---------------- CONFIG ----------------
def _build_upload_folder():
    configured = (os.environ.get("PATHEASE_UPLOAD_FOLDER", "") or "").strip()
    if configured:
        return configured

    # On Azure App Service Linux, keep user uploads in /home to avoid code-deploy churn.
    if os.environ.get("WEBSITE_SITE_NAME"):
        return "/home/site/uploads"

    return os.path.join(app.root_path, "uploads")


UPLOAD_FOLDER = _build_upload_folder()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = int(float(os.environ.get("PATHEASE_MAX_UPLOAD_MB", "10")) * 1024 * 1024)
print("Upload folder in use:", app.config["UPLOAD_FOLDER"])
print("Max upload size (bytes):", app.config["MAX_CONTENT_LENGTH"])

# Register AI blueprint (modular add-on)
app.register_blueprint(ai_features)

JWT_SECRET = os.environ.get("PATHEASE_JWT_SECRET", "replace_this_with_a_real_secret")


@app.route("/")
def root():
    return jsonify({"status": "ok", "service": "Pathease API"})


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

_env_mongo_uri = (os.environ.get("PATHEASE_MONGO_URI", "").strip() or os.environ.get("MONGO_URI", "").strip())
mongo_uri = normalize_mongo_uri(_env_mongo_uri)
if not mongo_uri:
    raise RuntimeError("PATHEASE_MONGO_URI or MONGO_URI must be set")
print("Mongo URI in use:", _redact_mongo_uri(mongo_uri))
client = MongoClient(mongo_uri)
db = client["patheaseDB"]

users_collection = db["users"]
places_collection = db["places"]
tracking_requests_collection = db["tracking_requests"]
live_locations_collection = db["live_locations"]
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}

# ---------------- HELPERS ----------------
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat/2)**2 + math.cos(math.radians(lat1)) * \
        math.cos(math.radians(lat2)) * math.sin(dLon/2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def normalize_email(email):
    return email.lower().strip()

def get_user_or_404(email):
    normalized = normalize_email(email)
    if not normalized:
        return None, (jsonify({"error": "Email required"}), 400)
    user = users_collection.find_one({"email": normalized})
    if not user:
        return None, (jsonify({"error": "User not found"}), 404)
    return user, None

def normalize_cart_item(item):
    if not isinstance(item, dict):
        return None
    place_name = (item.get("placeName") or "").strip()
    if not place_name:
        return None
    normalized = {
        "_id": str(item.get("_id") or "").strip() or str(ObjectId()),
        "placeName": place_name,
        "image": item.get("image"),
        "distance": item.get("distance"),
    }
    return normalized

def normalize_stop(stop):
    if isinstance(stop, str):
        stop = {"name": stop}
    if not isinstance(stop, dict):
        return None
    name = (stop.get("name") or "").strip()
    if not name:
        return None
    normalized = {"name": name}
    for key in ["lat", "lng", "distance_km"]:
        if stop.get(key) is not None:
            normalized[key] = stop.get(key)
    return normalized

def normalize_itinerary_day(day, index):
    if not isinstance(day, dict):
        day = {}
    normalized = {
        "day": day.get("day") if day.get("day") is not None else index + 1,
        "title": (day.get("title") or f"Day {index + 1}").strip(),
        "morning": day.get("morning") or "",
        "afternoon": day.get("afternoon") or "",
        "evening": day.get("evening") or "",
        "tips": day.get("tips") or "",
    }
    if day.get("est_cost") is not None:
        normalized["est_cost"] = day.get("est_cost")
    if day.get("currency") is not None:
        normalized["currency"] = day.get("currency")
    if day.get("currency_symbol") is not None:
        normalized["currency_symbol"] = day.get("currency_symbol")
    if day.get("day_distance_km") is not None:
        normalized["day_distance_km"] = day.get("day_distance_km")
    stops = [normalize_stop(stop) for stop in (day.get("stops") or [])]
    normalized["stops"] = [stop for stop in stops if stop]
    return normalized

def normalize_itinerary_plan(plan):
    if not isinstance(plan, dict):
        return None
    title = (plan.get("title") or "Trip Itinerary").strip()
    itinerary = [
        normalize_itinerary_day(day, idx)
        for idx, day in enumerate(plan.get("itinerary") or [])
    ]
    normalized = {
        "id": str(plan.get("id") or f"plan-{uuid4().hex}"),
        "title": title,
        "destination": (plan.get("destination") or "").strip(),
        "created_at": plan.get("created_at") or datetime.utcnow().isoformat(),
        "itinerary": itinerary,
        "notes": plan.get("notes") or "",
        "meta": plan.get("meta") if isinstance(plan.get("meta"), dict) else {},
        "updatedAt": datetime.utcnow().isoformat(),
    }
    return normalized

def normalize_location(loc):
    try:
        return float(loc["lat"]), float(loc["lng"])
    except:
        return None, None

def resolve_image(image):
    if not image:
        return None
    if not isinstance(image, str):
        return None
    if image.startswith("http"):
        return image
    if image.startswith("/uploads/"):
        return image
    if image.startswith("uploads/"):
        return f"/{image}"
    return f"/uploads/{image.lstrip('/')}"

def resolve_images(images):
    if not images:
        return []
    return [resolve_image(i) for i in images if i]

def image_to_stored_name(image):
    if not image or not isinstance(image, str):
        return None
    if image.startswith("http"):
        try:
            path = urlsplit(image).path or ""
            if "/uploads/" in path:
                return path.split("/uploads/", 1)[1].lstrip("/")
        except Exception:
            return None
        return None
    if image.startswith("/uploads/"):
        return image.split("/uploads/", 1)[1].lstrip("/")
    if image.startswith("uploads/"):
        return image.split("uploads/", 1)[1].lstrip("/")
    return image.lstrip("/")


def is_allowed_image_filename(filename):
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_IMAGE_EXTENSIONS


def save_uploaded_file(file_obj):
    raw_name = secure_filename((file_obj.filename or "").strip())
    if not raw_name:
        return None
    if not is_allowed_image_filename(raw_name):
        return None
    stem, ext = os.path.splitext(raw_name)
    unique_name = f"{stem}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}{ext}"
    file_obj.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
    return unique_name

def compute_feature_avg_ratings(reviews):
    if not reviews:
        return {}
    sums = {}
    counts = {}
    for r in reviews:
        ratings = r.get("ratings") or {}
        for k, v in ratings.items():
            try:
                val = float(v)
            except Exception:
                continue
            if val <= 0:
                continue
            sums[k] = sums.get(k, 0) + val
            counts[k] = counts.get(k, 0) + 1
    avgs = {}
    for k in sums:
        if counts.get(k):
            avgs[k] = round(sums[k] / counts[k], 2)
    return avgs

# ---------------- FILE SERVING ----------------
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    filename = (filename or "").lstrip("/")
    if filename.startswith("http"):
        abort(404)
    if filename.startswith("uploads/"):
        filename = filename.split("/", 1)[1]
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# ---------------- AUTH ----------------
@app.route("/signup", methods=["POST"])
def signup():
    data = request.json or {}
    email = normalize_email(data.get("email", ""))

    if users_collection.find_one({"email": email}):
        return jsonify({"error": "Email exists"}), 400

    pw = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt())
    users_collection.insert_one({
        "name": data.get("name"),
        "email": email,
        "password": Binary(pw),
        "role": "user",
        "blocked": False,
        "createdAt": datetime.utcnow()
    })
    return jsonify({"message": "Signup successful"})

@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    email = normalize_email(data.get("email", ""))

    user = users_collection.find_one({"email": email})
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401
    if user.get("blocked"):
        return jsonify({"error": "Account blocked. Contact admin."}), 403

    stored_pw = user.get("password")
    if isinstance(stored_pw, Binary):
        stored_pw = bytes(stored_pw)
    elif isinstance(stored_pw, bytes):
        stored_pw = stored_pw
    elif isinstance(stored_pw, str):
        stored_pw = stored_pw.encode()
    else:
        return jsonify({"error": "Invalid credentials"}), 401

    if not bcrypt.checkpw(data["password"].encode(), stored_pw):
        return jsonify({"error": "Invalid credentials"}), 401

    token = jwt.encode({
        "email": email,
        "role": user.get("role", "user"),
        "exp": datetime.utcnow() + timedelta(hours=5)
    }, JWT_SECRET, algorithm="HS256")

    return jsonify({
        "token": token,
        "name": user.get("name"),
        "email": email,
        "role": user.get("role", "user"),
        "user": {"name": user.get("name"), "email": email, "role": user.get("role", "user")}
    })

# ---------------- AI CHATBOT ----------------
SYSTEM_PROMPT = """
You are Pathease, an intelligent AI-powered tourism assistant and virtual travel guide.
Your task is to help users by creating personalized travel itineraries, recommending tourist attractions,
answering travel-related questions, and providing safety-aware guidance.
Based on the user’s destination, number of travel days, budget, travel type, interests, and current location,
generate a clear and tourist-friendly response.
Keep the language simple, concise, and easy to understand.
"""

@app.route("/chat", methods=["POST"])
def chat():
    data = request.json or {}
    user_message = data.get("message", "")
    
    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    # TODO: Integrate with real AI provider (OpenAI, Gemini, etc.)
    # For now, we return a simulated response based on keywords
    
    response_text = "I am Pathease, your virtual travel guide. I can help you plan your trip! (AI integration pending)"
    
    lower_msg = user_message.lower()
    if "plan" in lower_msg or "itinerary" in lower_msg:
        response_text = "I can certainly help you plan an itinerary! Please tell me your destination, number of days, and budget."
    elif "hello" in lower_msg or "hi" in lower_msg:
        response_text = "Hello! I am Pathease. Where would you like to travel today?"
    elif "mumbai" in lower_msg:
        response_text = "Mumbai is a vibrant city! You should visit the Gateway of India, Marine Drive, and Elephanta Caves."
    elif "safety" in lower_msg:
        response_text = "Safety is important. Always travel in groups at night and keep emergency contacts handy."
        
    return jsonify({"reply": response_text})

# ---------------- PLACES ----------------
@app.route("/submit-place", methods=["POST"])
def submit_place():
    placeName = request.form.get("placeName")
    description = request.form.get("description")
    features = json.loads(request.form.get("features") or "{}")
    location = json.loads(request.form.get("location") or "{}")
    submitted_by = normalize_email(request.form.get("submittedBy", "") or "")

    image = request.files.get("image")
    filename = None
    if image:
        filename = save_uploaded_file(image)
        if not filename:
            return jsonify({"error": "Invalid image file. Allowed: jpg, jpeg, png, gif, webp"}), 400

    images_list = []
    if filename:
        images_list.append(filename)

    places_collection.insert_one({
        "placeName": placeName,
        "description": description,
        "features": features,
        "location": location,
        "image": filename,
        "images": images_list,
        "images_uploads": [],
        "submittedBy": submitted_by,
        "reviews": [],
        "feature_ratings": {},
        "accessibility_level": "",
        "approved": False,
        "submittedAt": datetime.utcnow()
    })

    return jsonify({"message": "Place submitted"})

@app.route("/approve-place", methods=["POST"])
def approve_place():
    places_collection.update_one(
        {"_id": ObjectId(request.json["id"])},
        {"$set": {"approved": True}}
    )
    return jsonify({"message": "Approved"})

@app.route("/get-approved-places")
def get_approved():
    places = list(places_collection.find({"approved": True}))
    for p in places:
        p["_id"] = str(p["_id"])
        lat, lng = normalize_location(p.get("location", {}))
        p["location"] = {"lat": lat, "lng": lng} if lat else None
        p["image"] = resolve_image(p.get("image"))
        p["images"] = resolve_images(p.get("images", []))
        all_reviews = p.get("reviews") or []
        p["reviews"] = all_reviews
        p["feature_avg_ratings"] = compute_feature_avg_ratings(all_reviews)
        if p.get("submittedAt"):
            try:
                p["submittedAt"] = p["submittedAt"].isoformat()
            except Exception:
                pass
    return jsonify(places)

@app.route("/get-unapproved-places")
def get_unapproved():
    places = list(places_collection.find({"approved": False}))
    for p in places:
        p["_id"] = str(p["_id"])
        lat, lng = normalize_location(p.get("location", {}))
        p["location"] = {"lat": lat, "lng": lng} if lat else None
        p["image"] = resolve_image(p.get("image"))
    return jsonify(places)

@app.route("/add-place-review", methods=["POST"])
def add_place_review():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("place_id"))
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400

    email = normalize_email(data.get("email", ""))
    user = users_collection.find_one({"email": email}) if email else None
    avatar = user.get("avatar") if user else None
    display_name = (user.get("name") if user else None) or data.get("name") or "Anonymous"

    review = {
        "review_id": str(ObjectId()),
        "name": display_name,
        "email": email,
        "avatar": avatar,
        "comment": data.get("comment") or "",
        "ratings": data.get("ratings") or {},
        "approved": True,
        "createdAt": datetime.utcnow()
    }

    places_collection.update_one(
        {"_id": oid},
        {"$push": {"reviews": review}}
    )
    return jsonify({"message": "Review added"})

@app.route("/admin/review/add", methods=["POST"])
def admin_add_review():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("place_id"))
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400
    review = {
        "review_id": str(ObjectId()),
        "name": data.get("name") or "Admin",
        "email": normalize_email(data.get("email", "")),
        "avatar": data.get("avatar"),
        "comment": data.get("comment") or "",
        "ratings": data.get("ratings") or {},
        "approved": True,
        "createdAt": datetime.utcnow()
    }
    places_collection.update_one({"_id": oid}, {"$push": {"reviews": review}})
    return jsonify({"message": "Review added"})

@app.route("/admin/review/update", methods=["POST"])
def admin_update_review():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("place_id"))
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400
    idx = data.get("review_index")
    if idx is None:
        return jsonify({"error": "review_index required"}), 400
    try:
        idx = int(idx)
    except Exception:
        return jsonify({"error": "Invalid review_index"}), 400
    comment = data.get("comment", "")
    places_collection.update_one(
        {"_id": oid},
        {"$set": {f"reviews.{idx}.comment": comment}}
    )
    return jsonify({"message": "Review updated"})

@app.route("/admin/review/approve", methods=["POST"])
def admin_approve_review():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("place_id"))
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400
    idx = data.get("review_index")
    if idx is None:
        return jsonify({"error": "review_index required"}), 400
    try:
        idx = int(idx)
    except Exception:
        return jsonify({"error": "Invalid review_index"}), 400
    places_collection.update_one(
        {"_id": oid},
        {"$set": {f"reviews.{idx}.approved": True}}
    )
    return jsonify({"message": "Review approved"})

@app.route("/admin/review/delete", methods=["POST"])
def admin_delete_review():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("place_id"))
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400
    idx = data.get("review_index")
    if idx is None:
        return jsonify({"error": "review_index required"}), 400
    try:
        idx = int(idx)
    except Exception:
        return jsonify({"error": "Invalid review_index"}), 400

    place = places_collection.find_one({"_id": oid}, {"reviews": 1})
    reviews = place.get("reviews", []) if place else []
    if idx < 0 or idx >= len(reviews):
        return jsonify({"error": "Review not found"}), 404
    reviews.pop(idx)
    places_collection.update_one({"_id": oid}, {"$set": {"reviews": reviews}})
    return jsonify({"message": "Review deleted"})

@app.route("/admin-rate-place", methods=["POST"])
def admin_rate_place():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("place_id"))
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400

    raw_feature_ratings = data.get("feature_ratings") or {}
    feature_ratings = {}
    for k, v in raw_feature_ratings.items():
        try:
            val = float(v)
        except Exception:
            continue
        if val < 0:
            val = 0
        if val > 5:
            val = 5
        feature_ratings[k] = round(val, 2)
    accessibility_level = data.get("accessibility_level") or ""
    place = places_collection.find_one({"_id": oid}, {"features": 1}) or {}
    merged_features = dict(place.get("features") or {})
    merged_features.update(feature_ratings)

    places_collection.update_one(
        {"_id": oid},
        {"$set": {
            "feature_ratings": feature_ratings,
            "features": merged_features,
            "accessibility_level": accessibility_level,
            "ratedAt": datetime.utcnow()
        }}
    )
    return jsonify({"message": "Ratings saved"})

@app.route("/admin/place/update", methods=["POST"])
def admin_update_place():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("place_id"))
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400

    existing = places_collection.find_one({"_id": oid}) or {}

    update_fields = {"updatedAt": datetime.utcnow()}

    if "placeName" in data:
        update_fields["placeName"] = (data.get("placeName") or "").strip()
    if "description" in data:
        update_fields["description"] = (data.get("description") or "").strip()
    if "city" in data:
        update_fields["city"] = (data.get("city") or "").strip()

    if "location" in data:
        raw_loc = data.get("location") or {}
        lat = raw_loc.get("lat")
        lng = raw_loc.get("lng")
        location = existing.get("location") or {}
        try:
            if lat not in (None, "") and lng not in (None, ""):
                location = {"lat": float(lat), "lng": float(lng)}
            update_fields["location"] = location
        except Exception:
            return jsonify({"error": "Invalid location"}), 400

    if "features" in data:
        features = data.get("features")
        if not isinstance(features, dict):
            return jsonify({"error": "features must be object"}), 400
        update_fields["features"] = features

    primary_image = data.get("primary_image")
    if primary_image:
        primary_stored = image_to_stored_name(primary_image)
        images = [img for img in (existing.get("images") or []) if img]
        if primary_stored:
            if primary_stored in images:
                # Keep a true queue: primary image comes first.
                images = [primary_stored] + [img for img in images if img != primary_stored]
                update_fields["images"] = images
                update_fields["image"] = primary_stored
            else:
                return jsonify({"error": "Primary image not found in queue"}), 400

    places_collection.update_one({"_id": oid}, {"$set": update_fields})
    return jsonify({"message": "Place updated"})

@app.route("/admin/place/delete", methods=["POST"])
def admin_delete_place():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("place_id"))
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400

    result = places_collection.delete_one({"_id": oid})
    if result.deleted_count == 0:
        return jsonify({"error": "Place not found"}), 404
    return jsonify({"message": "Place deleted"})

@app.route("/admin/place/image/delete", methods=["POST"])
def admin_delete_place_image():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("place_id"))
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400

    stored_name = image_to_stored_name(data.get("image"))
    if not stored_name:
        return jsonify({"error": "Invalid image"}), 400

    place = places_collection.find_one({"_id": oid}) or {}
    images = [img for img in (place.get("images") or []) if img != stored_name]
    current_primary = image_to_stored_name(place.get("image"))
    next_primary = place.get("image")
    if current_primary == stored_name:
        next_primary = images[0] if images else None

    places_collection.update_one(
        {"_id": oid},
        {"$set": {"images": images, "image": next_primary, "updatedAt": datetime.utcnow()}}
    )
    return jsonify({"message": "Image deleted"})

@app.route("/upload-place-images", methods=["POST"])
def upload_place_images():
    place_id = request.form.get("place_id")
    uploader_email = normalize_email(request.form.get("uploader_email", "") or "")
    try:
        oid = ObjectId(place_id)
    except InvalidId:
        return jsonify({"error": "Invalid place id"}), 400

    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "No images"}), 400

    filenames = []
    for f in files:
        filename = save_uploaded_file(f)
        if filename:
            filenames.append(filename)

    if not filenames:
        return jsonify({"error": "No valid files. Allowed: jpg, jpeg, png, gif, webp"}), 400

    result = places_collection.update_one(
        {"_id": oid},
        {
            "$push": {
                "images": {"$each": filenames},
                "images_uploads": {
                    "$each": [
                        {
                            "filename": fn,
                            "uploadedBy": uploader_email,
                            "uploadedAt": datetime.utcnow()
                        }
                        for fn in filenames
                    ]
                }
            }
        }
    )
    if result.matched_count == 0:
        return jsonify({"error": "Place not found"}), 404
    return jsonify({"message": "Images added", "images": filenames})

@app.route("/get-profile", methods=["POST"])
def get_profile():
    email = normalize_email((request.json or {}).get("email", ""))
    user = users_collection.find_one({"email": email})
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "name": user.get("name"),
        "email": user.get("email"),
        "role": user.get("role", "user"),
        "avatar": user.get("avatar")
    })

@app.route("/profile/activity", methods=["POST"])
def profile_activity():
    email = normalize_email((request.json or {}).get("email", ""))
    if not email:
        return jsonify({"error": "Email required"}), 400

    submitted = list(places_collection.find(
        {"submittedBy": email},
        {"placeName": 1, "image": 1, "approved": 1, "submittedAt": 1}
    ))
    for p in submitted:
        p["_id"] = str(p["_id"])
        p["image"] = resolve_image(p.get("image"))

    comments = []
    ratings_sums = {}
    ratings_counts = {}
    for p in places_collection.find({"reviews.email": email}, {"placeName": 1, "reviews": 1}):
        for r in p.get("reviews", []):
            if (r.get("email") or "") == email:
                comments.append({
                    "place_id": str(p["_id"]),
                    "placeName": p.get("placeName"),
                    "comment": r.get("comment"),
                    "approved": r.get("approved", False),
                    "createdAt": r.get("createdAt").isoformat() if r.get("createdAt") else None
                })
                for k, v in (r.get("ratings") or {}).items():
                    try:
                        val = float(v)
                    except Exception:
                        continue
                    if val <= 0:
                        continue
                    ratings_sums[k] = ratings_sums.get(k, 0) + val
                    ratings_counts[k] = ratings_counts.get(k, 0) + 1

    uploads = []
    for p in places_collection.find({"images_uploads.uploadedBy": email}, {"placeName": 1, "images_uploads": 1}):
        for up in p.get("images_uploads", []):
            if (up.get("uploadedBy") or "") == email:
                uploads.append({
                    "place_id": str(p["_id"]),
                    "placeName": p.get("placeName"),
                    "filename": resolve_image(up.get("filename")),
                    "uploadedAt": up.get("uploadedAt").isoformat() if up.get("uploadedAt") else None
                })

    ratings_avg = {}
    total_sum = 0.0
    total_count = 0
    for k in ratings_sums:
        if ratings_counts.get(k):
            avg = round(ratings_sums[k] / ratings_counts[k], 2)
            ratings_avg[k] = avg
            total_sum += ratings_sums[k]
            total_count += ratings_counts[k]

    overall_avg = round(total_sum / total_count, 2) if total_count else 0

    return jsonify({
        "submitted_places": submitted,
        "comments": comments,
        "uploads": uploads,
        "ratings_summary": {
            "overall_avg": overall_avg,
            "feature_avg": ratings_avg,
            "count": total_count
        },
        "itinerary_count": len(user.get("itineraries") or []) if (user := users_collection.find_one({"email": email}, {"itineraries": 1})) else 0
    })

@app.route("/user/cart", methods=["POST"])
def get_user_cart():
    email = normalize_email((request.json or {}).get("email", ""))
    user, error = get_user_or_404(email)
    if error:
        return error
    return jsonify({"items": user.get("itinerary_cart") or []})

@app.route("/user/cart/add", methods=["POST"])
def add_user_cart_item():
    data = request.json or {}
    email = normalize_email(data.get("email", ""))
    item = normalize_cart_item(data.get("item"))
    if not item:
        return jsonify({"error": "Valid cart item required"}), 400

    user, error = get_user_or_404(email)
    if error:
        return error

    cart = user.get("itinerary_cart") or []
    exists = any(
        (c.get("_id") and c.get("_id") == item["_id"]) or
        ((c.get("placeName") or "").strip().lower() == item["placeName"].lower())
        for c in cart
    )
    if not exists:
        cart.append(item)
        users_collection.update_one(
            {"email": email},
            {"$set": {"itinerary_cart": cart, "travelUpdatedAt": datetime.utcnow()}}
        )
    return jsonify({"items": cart})

@app.route("/user/cart/remove", methods=["POST"])
def remove_user_cart_item():
    data = request.json or {}
    email = normalize_email(data.get("email", ""))
    item_id = str(data.get("item_id") or "").strip()
    place_name = (data.get("placeName") or "").strip().lower()

    if not item_id and not place_name:
        return jsonify({"error": "item_id or placeName required"}), 400

    user, error = get_user_or_404(email)
    if error:
        return error

    cart = user.get("itinerary_cart") or []
    next_cart = [
        c for c in cart
        if not (
            (item_id and str(c.get("_id") or "").strip() == item_id) or
            (place_name and (c.get("placeName") or "").strip().lower() == place_name)
        )
    ]
    users_collection.update_one(
        {"email": email},
        {"$set": {"itinerary_cart": next_cart, "travelUpdatedAt": datetime.utcnow()}}
    )
    return jsonify({"items": next_cart})

@app.route("/user/cart/clear", methods=["POST"])
def clear_user_cart():
    email = normalize_email((request.json or {}).get("email", ""))
    _user, error = get_user_or_404(email)
    if error:
        return error
    users_collection.update_one(
        {"email": email},
        {"$set": {"itinerary_cart": [], "travelUpdatedAt": datetime.utcnow()}}
    )
    return jsonify({"items": []})

@app.route("/user/itineraries", methods=["POST"])
def get_user_itineraries():
    email = normalize_email((request.json or {}).get("email", ""))
    user, error = get_user_or_404(email)
    if error:
        return error
    return jsonify({"items": user.get("itineraries") or []})

@app.route("/user/itineraries/create", methods=["POST"])
def create_user_itinerary():
    data = request.json or {}
    email = normalize_email(data.get("email", ""))
    plan = normalize_itinerary_plan(data.get("plan"))
    if not plan:
        return jsonify({"error": "Valid itinerary plan required"}), 400

    user, error = get_user_or_404(email)
    if error:
        return error

    itineraries = user.get("itineraries") or []
    itineraries.insert(0, plan)
    users_collection.update_one(
        {"email": email},
        {"$set": {"itineraries": itineraries, "travelUpdatedAt": datetime.utcnow()}}
    )
    return jsonify({"item": plan, "items": itineraries})

@app.route("/user/itineraries/update", methods=["POST"])
def update_user_itinerary():
    data = request.json or {}
    email = normalize_email(data.get("email", ""))
    plan = normalize_itinerary_plan(data.get("plan"))
    if not plan:
        return jsonify({"error": "Valid itinerary plan required"}), 400

    user, error = get_user_or_404(email)
    if error:
        return error

    itineraries = user.get("itineraries") or []
    updated = False
    next_items = []
    for existing in itineraries:
        if str(existing.get("id")) == plan["id"]:
            if not plan.get("created_at"):
                plan["created_at"] = existing.get("created_at") or datetime.utcnow().isoformat()
            next_items.append(plan)
            updated = True
        else:
            next_items.append(existing)
    if not updated:
        return jsonify({"error": "Itinerary not found"}), 404

    users_collection.update_one(
        {"email": email},
        {"$set": {"itineraries": next_items, "travelUpdatedAt": datetime.utcnow()}}
    )
    return jsonify({"item": plan, "items": next_items})

@app.route("/user/itineraries/delete", methods=["POST"])
def delete_user_itinerary():
    data = request.json or {}
    email = normalize_email(data.get("email", ""))
    itinerary_id = str(data.get("itinerary_id") or "").strip()
    if not itinerary_id:
        return jsonify({"error": "itinerary_id required"}), 400

    user, error = get_user_or_404(email)
    if error:
        return error

    itineraries = user.get("itineraries") or []
    next_items = [item for item in itineraries if str(item.get("id")) != itinerary_id]
    users_collection.update_one(
        {"email": email},
        {"$set": {"itineraries": next_items, "travelUpdatedAt": datetime.utcnow()}}
    )
    return jsonify({"items": next_items})

@app.route("/update-profile", methods=["POST"])
def update_profile():
    data = request.json or {}
    email = normalize_email(data.get("email", ""))
    if not email:
        return jsonify({"error": "Email required"}), 400
    update = {}
    if data.get("name"):
        update["name"] = data.get("name")
    if data.get("avatar"):
        update["avatar"] = data.get("avatar")
    users_collection.update_one({"email": email}, {"$set": update})
    return jsonify({"message": "Profile updated"})

@app.route("/upload-profile-pic", methods=["POST"])
def upload_profile_pic():
    email = normalize_email(request.form.get("email", ""))
    if not email:
        return jsonify({"error": "Email required"}), 400
    image = request.files.get("image")
    if not image:
        return jsonify({"error": "No image"}), 400
    filename = save_uploaded_file(image)
    if not filename:
        return jsonify({"error": "Invalid image file. Allowed: jpg, jpeg, png, gif, webp"}), 400
    result = users_collection.update_one({"email": email}, {"$set": {"avatar": filename}})
    if result.matched_count == 0:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"message": "Profile image updated", "avatar": filename})

@app.route("/admin/users")
def admin_users():
    users = list(users_collection.find({}, {"password": 0}))
    for u in users:
        u["_id"] = str(u["_id"])
        if "createdAt" not in u and ObjectId.is_valid(u["_id"]):
            try:
                u["createdAt"] = ObjectId(u["_id"]).generation_time.isoformat()
            except Exception:
                pass
        if u.get("avatar"):
            u["avatar"] = resolve_image(u["avatar"])
    return jsonify(users)

@app.route("/admin/user/<user_id>")
def admin_user_detail(user_id):
    try:
        oid = ObjectId(user_id)
    except InvalidId:
        return jsonify({"error": "Invalid user id"}), 400
    user = users_collection.find_one({"_id": oid}, {"password": 0})
    if not user:
        return jsonify({"error": "User not found"}), 404
    user["_id"] = str(user["_id"])
    if user.get("avatar"):
        user["avatar"] = resolve_image(user["avatar"])

    # Simple analytics for admin view
    email = user.get("email")
    review_count = places_collection.count_documents({"reviews.email": email})
    submitted_count = places_collection.count_documents({"submittedBy": email})
    user["analytics"] = {
        "reviews": review_count,
        "submitted_places": submitted_count
    }
    return jsonify(user)

@app.route("/admin/user/update", methods=["POST"])
def admin_user_update():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("user_id"))
    except InvalidId:
        return jsonify({"error": "Invalid user id"}), 400

    update = {}
    if data.get("name") is not None:
        update["name"] = data.get("name")
    if data.get("role") in ["user", "admin"]:
        update["role"] = data.get("role")
    if data.get("blocked") is not None:
        update["blocked"] = bool(data.get("blocked"))

    if not update:
        return jsonify({"error": "No fields to update"}), 400

    users_collection.update_one({"_id": oid}, {"$set": update})
    return jsonify({"message": "User updated"})

@app.route("/admin/user/block", methods=["POST"])
def admin_user_block():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("user_id"))
    except InvalidId:
        return jsonify({"error": "Invalid user id"}), 400
    blocked = bool(data.get("blocked", True))
    users_collection.update_one({"_id": oid}, {"$set": {"blocked": blocked}})
    return jsonify({"message": "User updated", "blocked": blocked})

@app.route("/admin/user/delete", methods=["POST"])
def admin_user_delete():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("user_id"))
    except InvalidId:
        return jsonify({"error": "Invalid user id"}), 400
    users_collection.delete_one({"_id": oid})
    return jsonify({"message": "User deleted"})

@app.route("/admin/analytics")
def admin_analytics():
    users_count = users_collection.count_documents({})
    approved = places_collection.count_documents({"approved": True})
    pending = places_collection.count_documents({"approved": False})
    reviews_count = places_collection.count_documents({"reviews.0": {"$exists": True}})
    return jsonify({
        "users": users_count,
        "approved_places": approved,
        "pending_places": pending,
        "places_with_reviews": reviews_count
    })

@app.route("/get-pending-places")
def get_pending_places():
    places = list(places_collection.find({"approved": False}))
    for p in places:
        p["_id"] = str(p["_id"])
        lat, lng = normalize_location(p.get("location", {}))
        p["location"] = {"lat": lat, "lng": lng} if lat else None
        p["image"] = resolve_image(p.get("image"))
        if p.get("submittedAt"):
            try:
                p["submittedAt"] = p["submittedAt"].isoformat()
            except Exception:
                pass
    return jsonify(places)

@app.route("/search")
def search_places():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    raw_regex = {"$regex": re.escape(q), "$options": "i"}
    tokens = [t for t in re.split(r"\s+", q.lower()) if t]
    token_patterns = []
    for t in tokens:
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        token_patterns.append(f"{re.escape(t)}s?")
    flexible = ".*".join(token_patterns) if token_patterns else re.escape(q)
    flex_regex = {"$regex": flexible, "$options": "i"}
    results = list(places_collection.find({
        "approved": True,
        "$or": [
            {"placeName": raw_regex},
            {"placeName": flex_regex},
            {"description": raw_regex},
            {"description": flex_regex},
            {"city": raw_regex},
            {"city": flex_regex},
        ]
    }))
    for p in results:
        p["_id"] = str(p["_id"])
        p["image"] = resolve_image(p.get("image"))
    return jsonify(results)

# ---------------- TRACKING SYSTEM ----------------

@app.route("/send-tracking-request", methods=["POST"])
def send_tracking_request():
    data = request.json or {}
    requester = normalize_email(data.get("requester", ""))
    target = normalize_email(data.get("target", ""))

    if not requester or not target or requester == target:
        return jsonify({"error": "Invalid request"}), 400

    if tracking_requests_collection.find_one({
        "$or": [
            {"requester": requester, "target": target},
            {"requester": target, "target": requester}
        ],
        "status": {"$in": ["pending", "accepted"]}
    }):
        return jsonify({"error": "Request already exists"}), 400

    tracking_requests_collection.insert_one({
        "requester": requester,
        "target": target,
        "status": "pending",
        "createdAt": datetime.utcnow()
    })

    return jsonify({"message": "Request sent"})

@app.route("/get-incoming-requests", methods=["POST"])
def get_incoming_requests():
    email = normalize_email(request.json.get("email", ""))
    reqs = list(tracking_requests_collection.find({
        "target": email,
        "status": "pending"
    }))
    for r in reqs:
        r["_id"] = str(r["_id"])
    return jsonify(reqs)

@app.route("/respond-tracking-request", methods=["POST"])
def respond_tracking_request():
    data = request.json or {}
    try:
        oid = ObjectId(data.get("request_id"))
    except InvalidId:
        return jsonify({"error": "Invalid request id"}), 400

    status = data.get("status")
    if status not in ["accepted", "rejected"]:
        return jsonify({"error": "Invalid status"}), 400

    res = tracking_requests_collection.update_one(
        {"_id": oid},
        {"$set": {"status": status, "updatedAt": datetime.utcnow()}}
    )

    if res.modified_count == 0:
        return jsonify({"error": "Request not found"}), 404

    return jsonify({"message": f"Request {status}"})

@app.route("/get-my-connections", methods=["POST"])
def get_my_connections():
    email = normalize_email(request.json.get("email", ""))
    conns = tracking_requests_collection.find({
        "$or": [{"requester": email}, {"target": email}],
        "status": "accepted"
    })

    return jsonify([
        {
            "id": str(c["_id"]),
            "email": c["target"] if c["requester"] == email else c["requester"]
        }
        for c in conns
    ])

@app.route("/stop-tracking", methods=["POST"])
def stop_tracking():
    data = request.json or {}
    a = normalize_email(data.get("requester", ""))
    b = normalize_email(data.get("target", ""))

    tracking_requests_collection.delete_many({
        "$or": [
            {"requester": a, "target": b},
            {"requester": b, "target": a}
        ]
    })
    return jsonify({"message": "Connection removed"})

# ---------------- LIVE LOCATION ----------------
@app.route("/update-live-location", methods=["POST"])
def update_live_location():
    data = request.json or {}
    email = normalize_email(data.get("email", ""))

    if not email:
        return jsonify({"error": "Email missing"}), 400

    live_locations_collection.update_one(
        {"email": email},
        {"$set": {
            "lat": float(data["lat"]),
            "lng": float(data["lng"]),
            "updatedAt": datetime.utcnow()
        }},
        upsert=True
    )

    return jsonify({"message": "Location updated"})

@app.route("/get-partner-location", methods=["POST"])
def get_partner_location():
    data = request.json or {}
    requester = normalize_email(data.get("requester", ""))
    target = normalize_email(data.get("target", ""))

    if not tracking_requests_collection.find_one({
        "$or": [
            {"requester": requester, "target": target},
            {"requester": target, "target": requester}
        ],
        "status": "accepted"
    }):
        return jsonify({"error": "No permission"}), 403

    loc = live_locations_collection.find_one({"email": target})
    if not loc:
        return jsonify({"error": "No location"}), 404

    return jsonify({
        "lat": loc["lat"],
        "lng": loc["lng"],
        "updatedAt": loc["updatedAt"].isoformat(),
        "age_seconds": (datetime.utcnow() - loc["updatedAt"]).total_seconds()
    })

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
