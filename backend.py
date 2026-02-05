from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from pymongo import MongoClient
from urllib.parse import quote_plus
from werkzeug.utils import secure_filename
from bson import ObjectId, Binary
from bson.errors import InvalidId
import bcrypt
import jwt
import os
import json
import math
from datetime import datetime, timedelta
from ai_features import ai_features

# ---------------- APP SETUP ----------------
app = Flask(__name__)
CORS(app)

# ---------------- CONFIG ----------------
UPLOAD_FOLDER = os.path.join(app.root_path, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Register AI blueprint (modular add-on)
app.register_blueprint(ai_features)

JWT_SECRET = os.environ.get("PATHEASE_JWT_SECRET", "replace_this_with_a_real_secret")

# ---------------- MONGO ----------------
username = quote_plus("nate")
password = quote_plus("Simba234")
mongo_uri = f"mongodb+srv://{username}:{password}@pathease.1vbi85h.mongodb.net/patheaseDB"
client = MongoClient(mongo_uri)
db = client["patheaseDB"]

users_collection = db["users"]
places_collection = db["places"]
tracking_requests_collection = db["tracking_requests"]
live_locations_collection = db["live_locations"]

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

def normalize_location(loc):
    try:
        return float(loc["lat"]), float(loc["lng"])
    except:
        return None, None

def resolve_image(image):
    if not image:
        return None
    if isinstance(image, str) and image.startswith("http"):
        return image
    return f"/uploads/{image}"

def resolve_images(images):
    if not images:
        return []
    return [resolve_image(i) for i in images if i]

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
    if filename.startswith("http"):
        abort(404)
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
        filename = secure_filename(image.filename)
        image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

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
        approved_reviews = [r for r in (p.get("reviews") or []) if r.get("approved")]
        p["reviews"] = approved_reviews
        p["feature_avg_ratings"] = compute_feature_avg_ratings(approved_reviews)
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
        "approved": False,
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

    feature_ratings = data.get("feature_ratings") or {}
    accessibility_level = data.get("accessibility_level") or ""

    places_collection.update_one(
        {"_id": oid},
        {"$set": {
            "feature_ratings": feature_ratings,
            "accessibility_level": accessibility_level,
            "ratedAt": datetime.utcnow()
        }}
    )
    return jsonify({"message": "Ratings saved"})

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
        filename = secure_filename(f.filename)
        if filename:
            f.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            filenames.append(filename)

    if not filenames:
        return jsonify({"error": "No valid files"}), 400

    places_collection.update_one(
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
        }
    })

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
    filename = secure_filename(image.filename)
    image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    users_collection.update_one({"email": email}, {"$set": {"avatar": filename}})
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
    regex = {"$regex": q, "$options": "i"}
    results = list(places_collection.find({
        "approved": True,
        "$or": [
            {"placeName": regex},
            {"description": regex},
            {"city": regex},
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
