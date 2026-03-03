import streamlit as st
import pandas as pd
import json
import os
import io
import hashlib
from datetime import datetime, date, time
from pathlib import Path
import googlemaps
import folium
from streamlit_folium import st_folium
from streamlit_searchbox import st_searchbox
import qrcode
from PIL import Image

# ── Data persistence ──────────────────────────────────────────────────────────
DATA_FILE = Path(__file__).parent / "data.json"

def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            d = json.load(f)
        if "users" not in d:
            d["users"] = {}
        return d
    return {"rides": [], "bookings": [], "insurance": {}, "passenger_ids": {}, "medical_info": {}, "parking_bookings": [], "vehicles": [], "users": {}}

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

# ── QR Code helpers ───────────────────────────────────────────────────────────
def make_qr_bytes(payload: dict) -> bytes:
    """Generate a QR code PNG from a dict and return raw bytes."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(json.dumps(payload, default=str))
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a1a2e", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ── Google Maps helpers ────────────────────────────────────────────────────────
@st.cache_resource
def get_gmaps():
    key = st.secrets.get("GOOGLE_MAPS_API_KEY", "")
    return googlemaps.Client(key=key) if key else None

def search_places(query: str) -> list:
    client = get_gmaps()
    if not client or not query or len(query) < 2:
        return []
    try:
        return [r["description"] for r in client.places_autocomplete(query)]
    except Exception:
        return []

def geocode(address: str):
    client = get_gmaps()
    if not client or not address:
        return None
    try:
        result = client.geocode(address)
        if result:
            loc = result[0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
    except Exception:
        pass
    return None

def show_route_map(from_addr, to_addr, height=280):
    from_coords = geocode(from_addr) if from_addr else None
    to_coords   = geocode(to_addr)   if to_addr   else None
    if not from_coords and not to_coords:
        return
    center = from_coords or to_coords
    m = folium.Map(location=center, zoom_start=7)
    if from_coords:
        folium.Marker(from_coords, tooltip=from_addr,
                      icon=folium.Icon(color="green", icon="play")).add_to(m)
    if to_coords:
        folium.Marker(to_coords, tooltip=to_addr,
                      icon=folium.Icon(color="red", icon="stop")).add_to(m)
    if from_coords and to_coords:
        folium.PolyLine([from_coords, to_coords], color="#667eea", weight=4).add_to(m)
        m.fit_bounds([from_coords, to_coords], padding=[40, 40])
    st_folium(m, height=height, use_container_width=True, returned_objects=[])

# ── Emergency helpers ─────────────────────────────────────────────────────────
def find_nearby_hospitals(lat: float, lng: float, radius: int = 8000) -> list:
    client = get_gmaps()
    if not client:
        return []
    try:
        result = client.places_nearby(location=(lat, lng), radius=radius, type="hospital")
        return result.get("results", [])[:8]
    except Exception:
        return []

def show_hospital_map(lat: float, lng: float, hospitals: list, height: int = 320):
    m = folium.Map(location=(lat, lng), zoom_start=13)
    folium.Marker((lat, lng), tooltip="📍 Your location",
                  icon=folium.Icon(color="red", icon="star")).add_to(m)
    for h in hospitals:
        loc = h["geometry"]["location"]
        folium.Marker(
            (loc["lat"], loc["lng"]),
            tooltip=h.get("name", "Hospital"),
            popup=h.get("vicinity", ""),
            icon=folium.Icon(color="blue", icon="plus-sign"),
        ).add_to(m)
    st_folium(m, height=height, use_container_width=True, returned_objects=[])

# ── Insurance / ID validation helpers ─────────────────────────────────────────
def has_valid_insurance(user: str, data: dict) -> tuple:
    ins = data.get("insurance", {}).get(user, {})
    if not ins:
        return False, "No insurance on file"
    if not ins.get("covers_passengers"):
        return False, "Policy does not cover passengers"
    try:
        exp = datetime.strptime(ins["expiry_date"], "%Y-%m-%d").date()
        if exp < date.today():
            return False, f"Policy expired on {ins['expiry_date']}"
    except (KeyError, ValueError):
        return False, "Invalid expiry date"
    return True, ins.get("provider", "Insured")

def has_valid_id(user: str, data: dict) -> tuple:
    id_proof = data.get("passenger_ids", {}).get(user, {})
    if not id_proof:
        return False, "No ID proof on file"
    try:
        exp = datetime.strptime(id_proof["expiry_date"], "%Y-%m-%d").date()
        if exp < date.today():
            return False, f"ID expired on {id_proof['expiry_date']}"
    except (KeyError, ValueError):
        return False, "Invalid expiry date"
    return True, id_proof.get("id_type", "ID verified")

# ── Auth helpers ──────────────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def seed_admin(data: dict):
    """Create a default admin account if none exists."""
    if not any(u.get("role") == "admin" for u in data["users"].values()):
        data["users"]["admin"] = {
            "password_hash": hash_password("admin123"),
            "role": "admin",
            "society": "Earthen Ambience",
            "created_at": str(datetime.now()),
        }
        save_data(data)

def authenticate(username: str, password: str, data: dict):
    """Return user dict on success, None on failure."""
    user = data["users"].get(username)
    if user and user["password_hash"] == hash_password(password):
        return user
    return None

def register_user(username: str, password: str, role: str, society: str, data: dict) -> tuple:
    """Register a new user. Returns (ok: bool, message: str)."""
    if not username or not password:
        return False, "Username and password are required."
    if username in data["users"]:
        return False, "Username already exists."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."
    data["users"][username] = {
        "password_hash": hash_password(password),
        "role": role,
        "society": society,
        "created_at": str(datetime.now()),
    }
    save_data(data)
    return True, "Account created successfully!"

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CarpoolConnect",
    page_icon="🚗",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .ride-card {
        background: #f8f9fa;
        border: 1px solid #dee2e6;
        border-radius: 10px;
        padding: 16px;
        margin-bottom: 12px;
    }
    .badge-available  { background:#d4edda; color:#155724; padding:3px 10px; border-radius:12px; font-size:0.8em; }
    .badge-full       { background:#f8d7da; color:#721c24; padding:3px 10px; border-radius:12px; font-size:0.8em; }
    .badge-insured    { background:#cce5ff; color:#004085; padding:3px 10px; border-radius:12px; font-size:0.8em; }
    .badge-unverified { background:#fff3cd; color:#856404; padding:3px 10px; border-radius:12px; font-size:0.8em; }
    .stat-box {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        border-radius: 10px;
        padding: 20px;
        text-align: center;
    }
    .stat-number { font-size: 2.5rem; font-weight: bold; }
    .stat-label  { font-size: 0.9rem; opacity: 0.9; }
    .emergency-banner {
        background: #dc3545; color: white;
        border-radius: 10px; padding: 18px 20px;
        margin-bottom: 18px; font-size: 1.15em; font-weight: bold;
        text-align: center; letter-spacing: 0.5px;
    }
    .medical-card {
        background: #fff8e1; border: 2px solid #ffc107;
        border-radius: 10px; padding: 16px; margin-bottom: 12px;
    }
    .ec-card {
        background: #e8f4fd; border: 1px solid #90caf9;
        border-radius: 10px; padding: 16px; margin-bottom: 12px;
    }
    .hospital-card {
        background: #e8f5e9; border: 1px solid #a5d6a7;
        border-radius: 10px; padding: 12px; margin-bottom: 8px;
    }
</style>
""", unsafe_allow_html=True)

# ── Session state defaults ────────────────────────────────────────────────────
if "current_user" not in st.session_state:
    st.session_state.current_user = "Guest"
if "page" not in st.session_state:
    st.session_state.page = "Home"
if "emergency_active" not in st.session_state:
    st.session_state.emergency_active = False
if "emergency_location" not in st.session_state:
    st.session_state.emergency_location = ""
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_role" not in st.session_state:
    st.session_state.user_role = "passenger"
if "user_society" not in st.session_state:
    st.session_state.user_society = ""

data = load_data()
seed_admin(data)

# ── Login / Register gate ─────────────────────────────────────────────────────
if not st.session_state.logged_in:
    st.markdown("""
    <style>
        .login-box { max-width: 480px; margin: 60px auto; }
        .login-title { font-size: 2.2rem; font-weight: 800; color: #667eea; text-align: center; margin-bottom: 0.2rem; }
        .login-sub   { text-align: center; color: #555; margin-bottom: 1.5rem; }
    </style>
    """, unsafe_allow_html=True)

    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown('<div class="login-title">🚗 CarpoolConnect</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-sub">Save money · Reduce traffic · Travel together</div>', unsafe_allow_html=True)
        st.write("")

        login_tab, register_tab = st.tabs(["🔑 Login", "📝 Register"])

        # ── Login tab ──────────────────────────────────────────────────────────
        with login_tab:
            with st.form("login_form"):
                l_user = st.text_input("Username", placeholder="Enter your username")
                l_pass = st.text_input("Password", type="password", placeholder="Enter your password")
                l_submit = st.form_submit_button("Login", type="primary", use_container_width=True)

                if l_submit:
                    if not l_user or not l_pass:
                        st.error("Please enter both username and password.")
                    else:
                        auth = authenticate(l_user, l_pass, data)
                        if auth:
                            st.session_state.logged_in = True
                            st.session_state.current_user = l_user
                            st.session_state.user_role = auth["role"]
                            st.session_state.user_society = auth.get("society", "")
                            st.session_state.page = "Home"
                            st.rerun()
                        else:
                            st.error("Invalid username or password.")

            st.caption("Default admin credentials: `admin` / `admin123`")

        # ── Register tab ────────────────────────────────────────────────────────
        with register_tab:
            with st.form("register_form"):
                r_user = st.text_input("Choose a username *", placeholder="e.g. john_doe")
                r_pass = st.text_input("Password * (min 6 chars)", type="password")
                r_pass2 = st.text_input("Confirm Password *", type="password")
                r_role = st.selectbox(
                    "I am a… *",
                    ["passenger", "driver"],
                    format_func=lambda x: "🧳 Passenger — I want to book rides" if x == "passenger" else "🚗 Driver — I have a car and want to offer rides",
                )
                r_society = st.selectbox(
                    "Society membership",
                    ["", "Earthen Ambience"],
                    format_func=lambda x: "Not a society member" if x == "" else f"🏘️ {x}",
                )
                r_submit = st.form_submit_button("Create Account", type="primary", use_container_width=True)

                if r_submit:
                    if r_pass != r_pass2:
                        st.error("Passwords do not match.")
                    else:
                        ok, msg = register_user(r_user, r_pass, r_role, r_society, data)
                        if ok:
                            st.success(msg + " Please log in.")
                        else:
                            st.error(msg)

    st.stop()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🚗 CarpoolConnect")
    st.divider()

    # User info
    role_icon = {"admin": "🛡️", "driver": "🚗", "passenger": "🧳"}.get(st.session_state.user_role, "👤")
    st.markdown(f"**{role_icon} {st.session_state.current_user}**")
    st.caption(f"Role: {st.session_state.user_role.capitalize()}")
    if st.session_state.user_society:
        st.caption(f"🏘️ {st.session_state.user_society}")

    st.divider()
    st.subheader("Navigation")

    # Build page list based on role and society
    pages = ["🏠 Home", "🔍 Find a Ride"]
    if st.session_state.user_role in ("driver", "admin"):
        pages.append("➕ Offer a Ride")
    pages += ["📋 My Bookings", "📊 Dashboard", "👤 My Profile"]
    if st.session_state.user_society == "Earthen Ambience" or st.session_state.user_role == "admin":
        pages.append("🅿️ Villa Parking")
    if st.session_state.user_role == "admin":
        pages.append("🔧 Admin Panel")

    for p in pages:
        if st.button(p, use_container_width=True):
            st.session_state.page = p.split(" ", 1)[1]

    st.divider()
    user = st.session_state.current_user
    ins_ok, _ = has_valid_insurance(user, data)
    id_ok,  _ = has_valid_id(user, data)
    if st.session_state.user_role == "driver":
        st.caption("🛡️ Insurance: " + ("✅ Valid" if ins_ok else "❌ Not set"))
    if st.session_state.user_role == "passenger":
        st.caption("🪪 ID Proof: "  + ("✅ Valid" if id_ok  else "❌ Not set"))

    st.divider()
    if st.session_state.emergency_active:
        if st.button("✅ Resolve Emergency", use_container_width=True):
            st.session_state.emergency_active = False
            st.session_state.emergency_location = ""
            st.session_state.page = "Home"
            st.rerun()
    else:
        if st.button("🚨 SOS — Emergency", use_container_width=True, type="primary"):
            st.session_state.emergency_active = True
            st.session_state.page = "Emergency"
            st.rerun()

    st.divider()
    if st.button("🚪 Logout", use_container_width=True):
        for key in ["logged_in", "current_user", "user_role", "user_society", "page",
                    "emergency_active", "emergency_location"]:
            st.session_state.pop(key, None)
        st.rerun()

page = st.session_state.page

# ── Persistent emergency banner ───────────────────────────────────────────────
if st.session_state.emergency_active:
    st.markdown(
        '<div class="emergency-banner">'
        '🚨 EMERGENCY MODE ACTIVE — Call 999 (UK) · 112 (EU) · 911 (US) immediately'
        '</div>',
        unsafe_allow_html=True,
    )

# ── HOME ─────────────────────────────────────────────────────────────────────
if page == "Home":
    st.title("🚗 Welcome to CarpoolConnect")
    st.markdown("**Save money, reduce traffic, and make new friends by sharing your ride.**")
    st.write("")

    col1, col2, col3, col4 = st.columns(4)
    total_rides    = len(data["rides"])
    total_bookings = len(data["bookings"])
    seats_saved    = sum(b.get("seats", 1) for b in data["bookings"])
    active_drivers = len({r["driver"] for r in data["rides"]})

    for col, num, label in zip(
        [col1, col2, col3, col4],
        [total_rides, total_bookings, seats_saved, active_drivers],
        ["Total Rides", "Bookings Made", "Seats Shared", "Active Drivers"],
    ):
        with col:
            st.markdown(f"""
            <div class="stat-box">
                <div class="stat-number">{num}</div>
                <div class="stat-label">{label}</div>
            </div>""", unsafe_allow_html=True)

    st.write("")
    st.subheader("How it works")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.info("**1. Post or Find a Ride**\nDrivers post available seats; passengers search by route and date.")
    with c2:
        st.success("**2. Book Instantly**\nOne-click booking — no phone calls required.")
    with c3:
        st.warning("**3. Travel Together**\nShare costs, reduce emissions, enjoy the journey.")

    st.write("")
    st.subheader("Recent Rides")
    if data["rides"]:
        recent = sorted(data["rides"], key=lambda r: r["date"], reverse=True)[:3]
        for ride in recent:
            seats_booked = sum(
                b["seats"] for b in data["bookings"]
                if b["ride_id"] == ride["id"]
            )
            available = ride["seats"] - seats_booked
            badge = (
                '<span class="badge-available">Available</span>'
                if available > 0
                else '<span class="badge-full">Full</span>'
            )
            st.markdown(f"""
            <div class="ride-card">
                <strong>📍 {ride['from']} → {ride['to']}</strong> &nbsp; {badge}<br>
                🗓 {ride['date']} &nbsp;|&nbsp; 🕐 {ride['time']} &nbsp;|&nbsp;
                💺 {available}/{ride['seats']} seats &nbsp;|&nbsp; 💰 £{ride['price_per_seat']}/seat<br>
                👤 Driver: {ride['driver']}
            </div>""", unsafe_allow_html=True)
    else:
        st.info("No rides posted yet. Be the first to offer a ride!")

# ── FIND A RIDE ───────────────────────────────────────────────────────────────
elif page == "Find a Ride":
    st.title("🔍 Find a Ride")

    with st.expander("Search Filters", expanded=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            search_from = st_searchbox(search_places, label="From",
                                       placeholder="e.g. London", key="find_from")
        with col2:
            search_to = st_searchbox(search_places, label="To",
                                     placeholder="e.g. Manchester", key="find_to")
        with col3:
            search_date = st.date_input("Date", value=date.today())
        min_seats = st.slider("Minimum seats needed", 1, 6, 1)

    if search_from or search_to:
        show_route_map(search_from, search_to)

    available_rides = []
    for ride in data["rides"]:
        seats_booked = sum(
            b["seats"] for b in data["bookings"] if b["ride_id"] == ride["id"]
        )
        seats_left = ride["seats"] - seats_booked
        if seats_left < min_seats:
            continue
        if search_from and search_from.lower() not in ride["from"].lower():
            continue
        if search_to and search_to.lower() not in ride["to"].lower():
            continue
        if str(search_date) != ride["date"]:
            continue
        available_rides.append({**ride, "seats_left": seats_left})

    st.divider()
    st.subheader(f"Found {len(available_rides)} ride(s)")

    if not available_rides:
        st.warning("No rides match your criteria. Try adjusting the filters or post your own ride!")
    else:
        for ride in available_rides:
            ins_ok, ins_msg = has_valid_insurance(ride["driver"], data)
            ins_badge = (
                '<span class="badge-insured">🛡️ Driver Insured</span>'
                if ins_ok else
                '<span class="badge-unverified">⚠️ Insurance Unverified</span>'
            )
            with st.container():
                col1, col2, col3 = st.columns([3, 2, 1])
                with col1:
                    st.markdown(f"### 📍 {ride['from']} → {ride['to']}")
                    st.write(f"🗓 **Date:** {ride['date']}  |  🕐 **Time:** {ride['time']}")
                    st.markdown(f"👤 **Driver:** {ride['driver']} &nbsp; {ins_badge}", unsafe_allow_html=True)
                    if ins_ok:
                        ins_detail = data.get("insurance", {}).get(ride["driver"], {})
                        st.caption(f"🛡️ {ins_detail.get('provider','')} · {ins_detail.get('policy_type','')} · expires {ins_detail.get('expiry_date','')}")
                    if ride.get("notes"):
                        st.caption(f"📝 {ride['notes']}")
                with col2:
                    st.metric("Seats Available", ride["seats_left"])
                    st.metric("Price per Seat", f"£{ride['price_per_seat']}")
                with col3:
                    seats_to_book = st.number_input(
                        "Seats", 1, ride["seats_left"],
                        key=f"seats_{ride['id']}"
                    )
                    if st.button("Book", key=f"book_{ride['id']}", type="primary"):
                        if ride["driver"] == st.session_state.current_user:
                            st.error("You cannot book your own ride.")
                        else:
                            id_ok, id_msg = has_valid_id(st.session_state.current_user, data)
                            if not id_ok:
                                st.error(f"ID required to book: {id_msg}.")
                                if st.button("Add ID Proof →", key=f"go_id_{ride['id']}"):
                                    st.session_state.page = "My Profile"
                                    st.rerun()
                            else:
                                booking = {
                                    "id": f"B{len(data['bookings'])+1:04d}",
                                    "ride_id": ride["id"],
                                    "passenger": st.session_state.current_user,
                                    "seats": seats_to_book,
                                    "booked_at": str(datetime.now()),
                                    "from": ride["from"],
                                    "to": ride["to"],
                                    "date": ride["date"],
                                    "time": ride["time"],
                                    "price": ride["price_per_seat"] * seats_to_book,
                                }
                                data["bookings"].append(booking)
                                save_data(data)
                                st.success(f"Booked {seats_to_book} seat(s)! Booking ID: {booking['id']}")
                                st.rerun()
                st.divider()

# ── OFFER A RIDE ──────────────────────────────────────────────────────────────
elif page == "Offer a Ride":
    st.title("➕ Offer a Ride")
    st.markdown("Share your journey and help others travel affordably.")

    if st.session_state.user_role not in ("driver", "admin"):
        st.error("Only registered **drivers** can offer rides. If you have a car, please create a Driver account.")
        st.stop()

    if st.session_state.current_user != "Guest":
        ins_ok, ins_msg = has_valid_insurance(st.session_state.current_user, data)
        if not ins_ok:
            st.error(
                f"**Insurance required:** {ins_msg}.\n\n"
                "You must have a valid **Third Party or Comprehensive** policy that covers "
                "passengers before you can post a ride."
            )
            if st.button("Go to My Profile → Add Insurance"):
                st.session_state.page = "My Profile"
                st.rerun()
            st.stop()
        else:
            ins_detail = data["insurance"][st.session_state.current_user]
            st.success(
                f"🛡️ Insurance verified — **{ins_detail['provider']}** · "
                f"{ins_detail['policy_type']} · expires {ins_detail['expiry_date']}"
            )

    col1, col2 = st.columns(2)
    with col1:
        origin      = st_searchbox(search_places, label="From *",
                                   placeholder="e.g. London King's Cross", key="offer_from")
        depart_date = st.date_input("Date *", value=date.today())
        seats       = st.number_input("Available Seats *", 1, 7, 2)
    with col2:
        destination = st_searchbox(search_places, label="To *",
                                   placeholder="e.g. Birmingham New Street", key="offer_to")
        depart_time = st.time_input("Departure Time *", value=time(9, 0))
        price       = st.number_input("Price per Seat (£) *", 0.0, 500.0, 10.0, step=0.5)

    car_model = st.text_input("Car Model", placeholder="e.g. Toyota Prius")
    notes     = st.text_area("Additional Notes", placeholder="e.g. No smoking, pets welcome, stops allowed...")

    if origin or destination:
        show_route_map(origin, destination)

    if st.button("Post Ride", type="primary", use_container_width=True):
        if not origin or not destination:
            st.error("Please fill in all required fields (*).")
        else:
            ride = {
                "id": f"R{len(data['rides'])+1:04d}",
                "driver": st.session_state.current_user,
                "from": origin,
                "to": destination,
                "date": str(depart_date),
                "time": str(depart_time),
                "seats": seats,
                "price_per_seat": price,
                "car_model": car_model,
                "notes": notes,
                "posted_at": str(datetime.now()),
            }
            data["rides"].append(ride)
            save_data(data)
            st.success(f"Ride posted successfully! Ride ID: {ride['id']}")
            for k in ["offer_from", "offer_to"]:
                st.session_state.pop(k, None)
            st.balloons()
            st.rerun()

# ── MY BOOKINGS ───────────────────────────────────────────────────────────────
elif page == "My Bookings":
    st.title("📋 My Bookings")

    user = st.session_state.current_user
    if True:
        # Rides I'm driving
        my_rides = [r for r in data["rides"] if r["driver"] == user]
        # Rides I've booked as passenger
        my_bookings = [b for b in data["bookings"] if b["passenger"] == user]

        tab1, tab2 = st.tabs([f"Rides I'm Offering ({len(my_rides)})", f"Rides I've Booked ({len(my_bookings)})"])

        with tab1:
            if not my_rides:
                st.info("You haven't offered any rides yet.")
            for ride in my_rides:
                ride_bookings = [b for b in data["bookings"] if b["ride_id"] == ride["id"]]
                seats_booked  = sum(b["seats"] for b in ride_bookings)
                with st.expander(f"🚗 {ride['from']} → {ride['to']} | {ride['date']} {ride['time']}"):
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Total Seats", ride["seats"])
                    col2.metric("Seats Booked", seats_booked)
                    col3.metric("Seats Left", ride["seats"] - seats_booked)
                    st.write(f"**Price per seat:** £{ride['price_per_seat']}")
                    st.write(f"**Total earnings:** £{ride['price_per_seat'] * seats_booked:.2f}")
                    if ride_bookings:
                        st.subheader("Passengers")
                        for b in ride_bookings:
                            st.write(f"- {b['passenger']} ({b['seats']} seat(s)) — booked {b['booked_at'][:10]}")

                    if st.button("Cancel Ride", key=f"cancel_ride_{ride['id']}"):
                        data["rides"] = [r for r in data["rides"] if r["id"] != ride["id"]]
                        data["bookings"] = [b for b in data["bookings"] if b["ride_id"] != ride["id"]]
                        save_data(data)
                        st.warning("Ride cancelled.")
                        st.rerun()

        with tab2:
            if not my_bookings:
                st.info("You haven't booked any rides yet.")
            for booking in my_bookings:
                with st.expander(f"🎫 {booking['from']} → {booking['to']} | {booking['date']} {booking['time']}"):
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Seats Booked", booking["seats"])
                    col2.metric("Total Cost", f"£{booking['price']:.2f}")
                    col3.metric("Booking ID", booking["id"])
                    st.write(f"**Booked on:** {booking['booked_at'][:10]}")

                    if st.button("Cancel Booking", key=f"cancel_booking_{booking['id']}"):
                        data["bookings"] = [b for b in data["bookings"] if b["id"] != booking["id"]]
                        save_data(data)
                        st.warning("Booking cancelled.")
                        st.rerun()

# ── MY PROFILE ────────────────────────────────────────────────────────────────
elif page == "My Profile":
    st.title("👤 My Profile")

    user = st.session_state.current_user
    tab1, tab2, tab3 = st.tabs(["🛡️ Driver Insurance", "🪪 Passenger ID Proof", "🚨 Medical & Emergency"])

    # ── Insurance tab ──────────────────────────────────────────────────────────
    with tab1:
        st.subheader("Driver Insurance Details")
        st.info(
            "To post rides, your policy must be **Third Party, Third Party Fire & Theft, "
            "or Comprehensive** and must explicitly **cover fare-paying passengers**."
        )

        existing = data.get("insurance", {}).get(user, {})
        ins_ok, ins_msg = has_valid_insurance(user, data)
        if existing:
            if ins_ok:
                st.success(f"✅ Insurance valid — {ins_msg}")
            else:
                st.error(f"⚠️ Issue: {ins_msg}")

        with st.form("insurance_form"):
            provider   = st.text_input("Insurance Provider *",
                                       value=existing.get("provider", ""),
                                       placeholder="e.g. Aviva, Direct Line, Admiral")
            policy_no  = st.text_input("Policy Number *",
                                       value=existing.get("policy_number", ""),
                                       placeholder="e.g. POL-123456789")

            policy_options = ["Third Party", "Third Party Fire & Theft", "Comprehensive"]
            current_type   = existing.get("policy_type", "Third Party")
            policy_type    = st.selectbox(
                "Policy Type *  (must include Third Party liability)",
                policy_options,
                index=policy_options.index(current_type) if current_type in policy_options else 0,
            )

            covers_passengers = st.checkbox(
                "✅ My policy explicitly covers fare-paying passengers *",
                value=existing.get("covers_passengers", False),
                help="Required for carpooling. Check your policy schedule or call your insurer to confirm."
            )

            col1, col2 = st.columns(2)
            with col1:
                vehicle_reg = st.text_input("Vehicle Registration *",
                                            value=existing.get("vehicle_reg", ""),
                                            placeholder="e.g. AB12 CDE")
            with col2:
                try:
                    exp_default = datetime.strptime(existing["expiry_date"], "%Y-%m-%d").date()
                except (KeyError, ValueError):
                    exp_default = date.today().replace(year=date.today().year + 1)
                expiry_date = st.date_input("Policy Expiry Date *",
                                            value=exp_default,
                                            min_value=date.today())

            insurer_contact = st.text_input("Insurer Helpline / Contact",
                                            value=existing.get("insurer_contact", ""),
                                            placeholder="e.g. 0800 123 4567")

            if st.form_submit_button("Save Insurance Details", type="primary", use_container_width=True):
                if not provider or not policy_no or not vehicle_reg:
                    st.error("Please fill in all required fields (*).")
                elif not covers_passengers:
                    st.error(
                        "You must confirm your policy covers passengers. "
                        "Contact your insurer to add passenger cover if needed."
                    )
                else:
                    if "insurance" not in data:
                        data["insurance"] = {}
                    data["insurance"][user] = {
                        "provider":          provider,
                        "policy_number":     policy_no,
                        "policy_type":       policy_type,
                        "covers_passengers": covers_passengers,
                        "vehicle_reg":       vehicle_reg,
                        "expiry_date":       str(expiry_date),
                        "insurer_contact":   insurer_contact,
                        "saved_at":          str(datetime.now()),
                    }
                    save_data(data)
                    st.success("Insurance details saved! You can now post rides.")
                    st.rerun()

    # ── ID Proof tab ───────────────────────────────────────────────────────────
    with tab2:
        st.subheader("Passenger ID Proof")
        st.info(
            "To book rides, you need a valid **government-issued photo ID**. "
            "This is required by the driver's passenger insurance policy."
        )

        existing_id = data.get("passenger_ids", {}).get(user, {})
        id_ok, id_msg = has_valid_id(user, data)
        if existing_id:
            if id_ok:
                st.success(f"✅ ID verified — {id_msg}")
            else:
                st.error(f"⚠️ Issue: {id_msg}")

        with st.form("id_form"):
            full_name = st.text_input("Full Name (as it appears on ID) *",
                                      value=existing_id.get("full_name", ""),
                                      placeholder="e.g. Jane Smith")

            id_options    = ["Passport", "Driver's Licence", "National ID Card", "Residence Permit"]
            current_id    = existing_id.get("id_type", "Passport")
            id_type       = st.selectbox(
                "ID Type *",
                id_options,
                index=id_options.index(current_id) if current_id in id_options else 0,
            )

            col1, col2 = st.columns(2)
            with col1:
                id_number = st.text_input("ID / Document Number *",
                                          value=existing_id.get("id_number", ""),
                                          placeholder="e.g. AB1234567")
            with col2:
                try:
                    id_exp_default = datetime.strptime(existing_id["expiry_date"], "%Y-%m-%d").date()
                except (KeyError, ValueError):
                    id_exp_default = date.today().replace(year=date.today().year + 2)
                id_expiry = st.date_input("ID Expiry Date *",
                                          value=id_exp_default,
                                          min_value=date.today())

            nationality = st.text_input("Nationality",
                                        value=existing_id.get("nationality", ""),
                                        placeholder="e.g. British")

            st.caption(
                "🔒 Your ID details are stored locally and shared only with the driver "
                "for the purpose of passenger insurance verification."
            )

            if st.form_submit_button("Save ID Details", type="primary", use_container_width=True):
                if not full_name or not id_number:
                    st.error("Please fill in all required fields (*).")
                else:
                    if "passenger_ids" not in data:
                        data["passenger_ids"] = {}
                    data["passenger_ids"][user] = {
                        "full_name":   full_name,
                        "id_type":     id_type,
                        "id_number":   id_number,
                        "expiry_date": str(id_expiry),
                        "nationality": nationality,
                        "saved_at":    str(datetime.now()),
                    }
                    save_data(data)
                    st.success("ID details saved! You can now book rides.")
                    st.rerun()

    # ── Medical & Emergency tab ────────────────────────────────────────────────
    with tab3:
        st.subheader("Medical Information & Emergency Contacts")
        st.info(
            "This information is shown to other ride participants and emergency responders "
            "when the **SOS button** is activated. Keep it accurate and up to date."
        )

        existing_med = data.get("medical_info", {}).get(user, {})
        if existing_med:
            st.success("✅ Medical info on file")

        with st.form("medical_form"):
            st.markdown("#### 🩺 Medical Details")
            blood_options = ["Unknown", "A+", "A−", "B+", "B−", "AB+", "AB−", "O+", "O−"]
            current_bg    = existing_med.get("blood_group", "Unknown")
            blood_group   = st.selectbox(
                "Blood Group",
                blood_options,
                index=blood_options.index(current_bg) if current_bg in blood_options else 0,
            )
            col1, col2 = st.columns(2)
            with col1:
                allergies   = st.text_area("Allergies",
                                           value=existing_med.get("allergies", ""),
                                           placeholder="e.g. Penicillin, Nuts, Latex",
                                           height=90)
                conditions  = st.text_area("Medical Conditions",
                                           value=existing_med.get("conditions", ""),
                                           placeholder="e.g. Diabetic, Epileptic, Asthmatic",
                                           height=90)
            with col2:
                medications = st.text_area("Current Medications",
                                           value=existing_med.get("medications", ""),
                                           placeholder="e.g. Insulin, EpiPen (in bag)",
                                           height=90)
                other_notes = st.text_area("Other Notes for Responders",
                                           value=existing_med.get("other_notes", ""),
                                           placeholder="e.g. Wears contact lenses, hearing aid in left ear",
                                           height=90)

            st.markdown("#### 📞 Emergency Contact 1 (Primary)")
            ec1a, ec1b, ec1c = st.columns(3)
            with ec1a:
                ec_name     = st.text_input("Full Name *",
                                            value=existing_med.get("ec_name", ""),
                                            placeholder="e.g. Jane Smith")
            with ec1b:
                ec_relation = st.text_input("Relationship",
                                            value=existing_med.get("ec_relation", ""),
                                            placeholder="e.g. Spouse, Parent")
            with ec1c:
                ec_phone    = st.text_input("Phone Number *",
                                            value=existing_med.get("ec_phone", ""),
                                            placeholder="e.g. 07700 900123")

            st.markdown("#### 📞 Emergency Contact 2 (Secondary)")
            ec2a, ec2b, ec2c = st.columns(3)
            with ec2a:
                ec2_name    = st.text_input("Full Name",
                                            value=existing_med.get("ec2_name", ""),
                                            placeholder="e.g. John Smith",
                                            key="ec2_name")
            with ec2b:
                ec2_relation = st.text_input("Relationship",
                                             value=existing_med.get("ec2_relation", ""),
                                             placeholder="e.g. Sibling, Friend",
                                             key="ec2_rel")
            with ec2c:
                ec2_phone   = st.text_input("Phone Number",
                                            value=existing_med.get("ec2_phone", ""),
                                            placeholder="e.g. 07700 900456",
                                            key="ec2_phone")

            if st.form_submit_button("Save Medical & Emergency Info", type="primary", use_container_width=True):
                if not ec_name or not ec_phone:
                    st.error("At least one emergency contact name and phone number are required.")
                else:
                    if "medical_info" not in data:
                        data["medical_info"] = {}
                    data["medical_info"][user] = {
                        "blood_group":  blood_group,
                        "allergies":    allergies,
                        "conditions":   conditions,
                        "medications":  medications,
                        "other_notes":  other_notes,
                        "ec_name":      ec_name,
                        "ec_relation":  ec_relation,
                        "ec_phone":     ec_phone,
                        "ec2_name":     ec2_name,
                        "ec2_relation": ec2_relation,
                        "ec2_phone":    ec2_phone,
                        "saved_at":     str(datetime.now()),
                    }
                    save_data(data)
                    st.success("Medical & emergency contact info saved!")
                    st.rerun()

# ── EMERGENCY ────────────────────────────────────────────────────────────────
elif page == "Emergency":
    st.markdown(
        '<div class="emergency-banner">🚨 EMERGENCY — Medical Assistance Initiated</div>',
        unsafe_allow_html=True,
    )

    # ── Emergency numbers ──────────────────────────────────────────────────────
    st.subheader("📞 Emergency Numbers")
    c1, c2, c3, c4 = st.columns(4)
    for col, num, label, color in zip(
        [c1, c2, c3, c4],
        ["999", "112", "911", "111"],
        ["🇬🇧 UK Police/Fire/Ambulance", "🌍 EU Emergency", "🇺🇸 US Emergency", "🏥 NHS Non-Emergency"],
        ["#dc3545", "#c82333", "#e74c3c", "#fd7e14"],
    ):
        col.markdown(
            f'<div style="background:{color};color:white;border-radius:10px;'
            f'padding:18px;text-align:center;font-size:1.8rem;font-weight:bold;">'
            f'{num}<br><span style="font-size:0.6rem;">{label}</span></div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Current location + hospital map ───────────────────────────────────────
    st.subheader("🏥 Find Nearest Hospitals")
    loc_input = st.text_input(
        "Enter your current location / nearest landmark",
        value=st.session_state.emergency_location,
        placeholder="e.g. Oxford Street, London",
    )
    if loc_input:
        st.session_state.emergency_location = loc_input

    if loc_input:
        coords = geocode(loc_input)
        if coords:
            hospitals = find_nearby_hospitals(coords[0], coords[1])
            if hospitals:
                col_map, col_list = st.columns([3, 2])
                with col_map:
                    show_hospital_map(coords[0], coords[1], hospitals)
                with col_list:
                    st.markdown("**Nearby hospitals:**")
                    for h in hospitals:
                        name     = h.get("name", "Hospital")
                        vicinity = h.get("vicinity", "")
                        rating   = h.get("rating", "")
                        open_now = h.get("opening_hours", {}).get("open_now")
                        open_str = "🟢 Open now" if open_now else ("🔴 Closed" if open_now is False else "")
                        st.markdown(
                            f'<div class="hospital-card">'
                            f'<strong>🏥 {name}</strong><br>'
                            f'📍 {vicinity}<br>'
                            f'{"⭐ " + str(rating) + " &nbsp;|&nbsp; " if rating else ""}{open_str}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
            else:
                st.warning("No hospitals found nearby. Please call emergency services directly.")
        else:
            st.error("Could not locate that address. Please try again.")

    st.divider()

    # ── My medical card ───────────────────────────────────────────────────────
    user = st.session_state.current_user
    st.subheader("🩺 Medical Information")

    def render_medical_card(username, med, show_ec=True):
        bg = med.get("blood_group", "Unknown")
        allergies  = med.get("allergies", "None") or "None"
        conditions = med.get("conditions", "None") or "None"
        medications = med.get("medications", "None") or "None"
        st.markdown(
            f'<div class="medical-card">'
            f'<strong>👤 {username}</strong><br>'
            f'🩸 <b>Blood Group:</b> {bg} &nbsp;|&nbsp; '
            f'⚠️ <b>Allergies:</b> {allergies}<br>'
            f'💊 <b>Conditions:</b> {conditions}<br>'
            f'💉 <b>Medications:</b> {medications}'
            f'</div>',
            unsafe_allow_html=True,
        )
        if show_ec:
            ec_name  = med.get("ec_name", "")
            ec_phone = med.get("ec_phone", "")
            ec_rel   = med.get("ec_relation", "")
            ec2_name  = med.get("ec2_name", "")
            ec2_phone = med.get("ec2_phone", "")
            if ec_name:
                st.markdown(
                    f'<div class="ec-card">'
                    f'📞 <b>Emergency Contact:</b> {ec_name}'
                    f'{" (" + ec_rel + ")" if ec_rel else ""} — '
                    f'<a href="tel:{ec_phone}"><b>{ec_phone}</b></a>'
                    f'{"<br>📞 <b>Alt Contact:</b> " + ec2_name + " — <a href=tel:" + ec2_phone + "><b>" + ec2_phone + "</b></a>" if ec2_name else ""}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    my_med = data.get("medical_info", {}).get(user, {})
    if my_med:
        render_medical_card(user, my_med)
    else:
        st.warning(f"No medical info on file for **{user}**. Please add it in My Profile → Medical & Emergency.")

    # ── Ride participants' info ────────────────────────────────────────────────
    st.divider()
    st.subheader("👥 Ride Participants' Medical Info")

    # Collect everyone in the same active rides as this user
    participants = set()
    for ride in data.get("rides", []):
        if ride["driver"] == user:
            for b in data.get("bookings", []):
                if b["ride_id"] == ride["id"]:
                    participants.add(b["passenger"])
    for booking in data.get("bookings", []):
        if booking["passenger"] == user:
            for ride in data.get("rides", []):
                if ride["id"] == booking["ride_id"]:
                    participants.add(ride["driver"])

    if participants:
        for p in sorted(participants):
            med = data.get("medical_info", {}).get(p, {})
            if med:
                render_medical_card(p, med)
            else:
                st.markdown(
                    f'<div class="medical-card" style="opacity:0.6;">'
                    f'<strong>👤 {p}</strong> — no medical info on file</div>',
                    unsafe_allow_html=True,
                )
    else:
        st.info("No other ride participants found.")

    st.divider()
    st.subheader("🏥 First Aid Quick Reference")
    fa1, fa2, fa3 = st.columns(3)
    with fa1:
        st.error("**Unconscious / Not Breathing**\n\n1. Call 999 immediately\n2. Begin CPR: 30 chest compressions\n3. 2 rescue breaths\n4. Repeat until help arrives")
    with fa2:
        st.warning("**Severe Bleeding**\n\n1. Apply firm pressure with cloth\n2. Keep pressure on — don't remove\n3. Elevate the injured area\n4. Call 999 if blood soaks through")
    with fa3:
        st.info("**Suspected Spinal Injury**\n\n1. Do NOT move the person\n2. Keep head/neck still\n3. Call 999 immediately\n4. Stay calm and reassure them")

# ── DASHBOARD ────────────────────────────────────────────────────────────────
elif page == "Dashboard":
    st.title("📊 Platform Dashboard")

    if not data["rides"] and not data["bookings"]:
        st.info("No data yet. Post some rides to see analytics here!")
    else:
        col1, col2 = st.columns(2)

        # Rides by route
        if data["rides"]:
            with col1:
                st.subheader("Rides by Route")
                routes = [f"{r['from']} → {r['to']}" for r in data["rides"]]
                route_df = pd.Series(routes).value_counts().reset_index()
                route_df.columns = ["Route", "Count"]
                st.bar_chart(route_df.set_index("Route"))

        # Bookings over time
        if data["bookings"]:
            with col2:
                st.subheader("Bookings Over Time")
                dates = [b["booked_at"][:10] for b in data["bookings"]]
                date_df = pd.Series(dates).value_counts().sort_index().reset_index()
                date_df.columns = ["Date", "Bookings"]
                st.line_chart(date_df.set_index("Date"))

        # Summary table
        st.subheader("All Rides Summary")
        if data["rides"]:
            rows = []
            for ride in data["rides"]:
                booked = sum(b["seats"] for b in data["bookings"] if b["ride_id"] == ride["id"])
                rows.append({
                    "ID":       ride["id"],
                    "Driver":   ride["driver"],
                    "From":     ride["from"],
                    "To":       ride["to"],
                    "Date":     ride["date"],
                    "Time":     ride["time"],
                    "Seats":    ride["seats"],
                    "Booked":   booked,
                    "Available": ride["seats"] - booked,
                    "£/Seat":   ride["price_per_seat"],
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

# ── VILLA PARKING ─────────────────────────────────────────────────────────────
elif page == "Villa Parking":
    st.title("🅿️ Villa Society Parking")
    st.markdown("**5 reserved slots · QR-linked vehicle passes · instant booking**")

    TOTAL_SLOTS = 5
    SLOT_NAMES  = [f"P{i}" for i in range(1, TOTAL_SLOTS + 1)]
    user        = st.session_state.current_user

    if "parking_bookings" not in data:
        data["parking_bookings"] = []
    if "vehicles" not in data:
        data["vehicles"] = []

    # ── helpers ───────────────────────────────────────────────────────────────
    def slots_overlap(s1, e1, s2, e2):
        return s1 < e2 and s2 < e1

    def get_occupied_slots(date_str, start_str, end_str):
        occupied = set()
        for pb in data["parking_bookings"]:
            if pb["date"] == date_str and slots_overlap(start_str, end_str, pb["start_time"], pb["end_time"]):
                occupied.add(pb["slot"])
        return occupied

    def my_vehicles():
        return [v for v in data["vehicles"] if v["owner"] == user]

    # ── tabs ──────────────────────────────────────────────────────────────────
    tab_cars, tab_book, tab_mine, tab_all = st.tabs(
        ["🚗 My Cars & QR", "🅿️ Book a Slot", "📋 My Parking", "📊 All Bookings"]
    )

    # ══════════════════════════════════════════════════════════════════════════
    # Tab 1 — My Cars & QR Codes
    # ══════════════════════════════════════════════════════════════════════════
    with tab_cars:
        st.subheader("Register your vehicle & get a QR pass")

        # ── Register form ─────────────────────────────────────────────
        with st.expander("➕ Register a new vehicle", expanded=len(my_vehicles()) == 0):
            with st.form("register_vehicle_form"):
                col_a, col_b = st.columns(2)
                with col_a:
                    v_owner_name = st.text_input("Owner full name", placeholder="Alice Johnson")
                    v_number     = st.text_input("Vehicle number *", placeholder="AB12 CDE")
                    v_model      = st.text_input("Car model", placeholder="Toyota Prius")
                with col_b:
                    v_color   = st.text_input("Color", placeholder="White")
                    v_phone   = st.text_input("Contact phone", placeholder="07700 900123")
                    v_flat    = st.text_input("Flat / unit no.", placeholder="Villa 4B")
                reg_submit = st.form_submit_button("Register & Generate QR", type="primary", use_container_width=True)

                if reg_submit:
                    if not v_number.strip():
                        st.error("Vehicle number is required.")
                    elif any(v["vehicle_number"] == v_number.strip().upper() and v["owner"] == user
                             for v in data["vehicles"]):
                        st.warning("This vehicle is already registered under your name.")
                    else:
                        vid = f"VH{len(data['vehicles']) + 1:04d}"
                        data["vehicles"].append({
                            "id":             vid,
                            "owner":          user,
                            "owner_name":     v_owner_name.strip() or user,
                            "vehicle_number": v_number.strip().upper(),
                            "model":          v_model.strip(),
                            "color":          v_color.strip(),
                            "phone":          v_phone.strip(),
                            "flat":           v_flat.strip(),
                            "registered_at":  str(datetime.now()),
                            "society":        "Villa Society",
                        })
                        save_data(data)
                        st.success(f"Vehicle **{v_number.strip().upper()}** registered! Your QR pass is below.")
                        st.rerun()

        # ── Registered vehicles & QR codes ────────────────────────────
        vehicles = my_vehicles()
        if not vehicles:
            st.info("No vehicles registered yet. Use the form above.")
        else:
            st.markdown(f"**{len(vehicles)} vehicle(s) registered**")
            for v in vehicles:
                with st.expander(f"🚗 {v['vehicle_number']}  ·  {v.get('model','') or 'Car'}  ·  {v.get('color','') or ''}"):
                    c1, c2 = st.columns([1, 2])
                    with c1:
                        qr_payload = {
                            "type":           "vehicle_pass",
                            "vehicle_id":     v["id"],
                            "vehicle_number": v["vehicle_number"],
                            "owner_name":     v["owner_name"],
                            "model":          v.get("model", ""),
                            "color":          v.get("color", ""),
                            "phone":          v.get("phone", ""),
                            "flat":           v.get("flat", ""),
                            "society":        v.get("society", "Villa Society"),
                        }
                        qr_img = make_qr_bytes(qr_payload)
                        st.image(qr_img, caption="Scan to identify vehicle", width=200)
                        st.download_button(
                            "⬇️ Download QR",
                            data=qr_img,
                            file_name=f"qr_{v['vehicle_number'].replace(' ','_')}.png",
                            mime="image/png",
                            key=f"dl_qr_{v['id']}",
                            use_container_width=True,
                        )
                    with c2:
                        st.markdown(f"**Vehicle ID:** `{v['id']}`")
                        st.markdown(f"**Number:** {v['vehicle_number']}")
                        st.markdown(f"**Owner:** {v['owner_name']}")
                        st.markdown(f"**Model:** {v.get('model','—')}")
                        st.markdown(f"**Color:** {v.get('color','—')}")
                        st.markdown(f"**Phone:** {v.get('phone','—')}")
                        st.markdown(f"**Flat/Unit:** {v.get('flat','—')}")
                        st.markdown(f"**Society:** {v.get('society','Villa Society')}")
                        st.caption(f"Registered: {v['registered_at'][:16]}")
                        if st.button("🗑️ Remove vehicle", key=f"rm_v_{v['id']}"):
                            data["vehicles"] = [x for x in data["vehicles"] if x["id"] != v["id"]]
                            save_data(data)
                            st.success("Vehicle removed.")
                            st.rerun()

    # ══════════════════════════════════════════════════════════════════════════
    # Tab 2 — Book a Slot
    # ══════════════════════════════════════════════════════════════════════════
    with tab_book:
        st.subheader("Check Availability & Book")

        vehicles = my_vehicles()
        if not vehicles:
            st.warning("You have no registered vehicles. Go to the **🚗 My Cars & QR** tab to register one first.")
        else:
            col_d, col_s, col_e = st.columns(3)
            with col_d:
                park_date = st.date_input("Date", value=date.today(), min_value=date.today(), key="park_date")
            with col_s:
                start_h = st.selectbox("Start time", [f"{h:02d}:00" for h in range(0, 24)], index=8, key="park_start")
            with col_e:
                end_options  = [f"{h:02d}:00" for h in range(0, 24)]
                end_h = st.selectbox("End time", end_options, index=18, key="park_end")

            if start_h >= end_h:
                st.error("End time must be after start time.")
            else:
                date_str = str(park_date)
                occupied = get_occupied_slots(date_str, start_h, end_h)

                st.markdown("#### Slot Availability")
                cols = st.columns(TOTAL_SLOTS)
                for i, slot in enumerate(SLOT_NAMES):
                    with cols[i]:
                        occupant_info = ""
                        for pb in data["parking_bookings"]:
                            if (pb["slot"] == slot and pb["date"] == date_str
                                    and slots_overlap(start_h, end_h, pb["start_time"], pb["end_time"])):
                                occupant_info = pb.get("vehicle_number", pb.get("vehicle", ""))
                                break
                        if slot in occupied:
                            st.markdown(
                                f"<div style='background:#dc3545;color:white;border-radius:12px;"
                                f"padding:16px 4px;text-align:center;font-size:1.3em;font-weight:bold;'>"
                                f"{slot}<br><small style='font-size:.6em'>🔴 Occupied</small>"
                                f"{'<br><small style=\"font-size:.55em\">' + occupant_info + '</small>' if occupant_info else ''}"
                                f"</div>",
                                unsafe_allow_html=True,
                            )
                        else:
                            st.markdown(
                                f"<div style='background:#28a745;color:white;border-radius:12px;"
                                f"padding:16px 4px;text-align:center;font-size:1.3em;font-weight:bold;'>"
                                f"{slot}<br><small style='font-size:.6em'>🟢 Free</small></div>",
                                unsafe_allow_html=True,
                            )

                st.write("")
                available_slots = [s for s in SLOT_NAMES if s not in occupied]

                if not available_slots:
                    st.error("All slots are occupied for the selected time window.")
                else:
                    st.markdown("#### Reserve a Slot")
                    vehicle_labels = {
                        v["id"]: f"{v['vehicle_number']}  ·  {v.get('model','') or 'Car'}  ({v.get('color','')})"
                        for v in vehicles
                    }
                    with st.form("park_booking_form"):
                        chosen_slot = st.selectbox("Select slot", available_slots)
                        chosen_vid  = st.selectbox(
                            "Select your vehicle (QR pass)",
                            options=list(vehicle_labels.keys()),
                            format_func=lambda k: vehicle_labels[k],
                        )
                        book_submit = st.form_submit_button("Confirm Booking", type="primary", use_container_width=True)

                        if book_submit:
                            still_occupied = get_occupied_slots(date_str, start_h, end_h)
                            if chosen_slot in still_occupied:
                                st.error(f"Slot {chosen_slot} was just taken. Please choose another.")
                            else:
                                chosen_vehicle = next(v for v in vehicles if v["id"] == chosen_vid)
                                new_id = f"PK{len(data['parking_bookings']) + 1:04d}"
                                booking_record = {
                                    "id":             new_id,
                                    "slot":           chosen_slot,
                                    "user":           user,
                                    "vehicle_id":     chosen_vehicle["id"],
                                    "vehicle_number": chosen_vehicle["vehicle_number"],
                                    "owner_name":     chosen_vehicle["owner_name"],
                                    "model":          chosen_vehicle.get("model", ""),
                                    "color":          chosen_vehicle.get("color", ""),
                                    "phone":          chosen_vehicle.get("phone", ""),
                                    "flat":           chosen_vehicle.get("flat", ""),
                                    "date":           date_str,
                                    "start_time":     start_h,
                                    "end_time":       end_h,
                                    "booked_at":      str(datetime.now()),
                                    "vehicle":        chosen_vehicle["vehicle_number"],
                                }
                                data["parking_bookings"].append(booking_record)
                                save_data(data)
                                st.success(f"✅ Slot **{chosen_slot}** booked for **{chosen_vehicle['vehicle_number']}** on {date_str}  {start_h}–{end_h}!")
                                st.rerun()

    # ══════════════════════════════════════════════════════════════════════════
    # Tab 3 — My Parking Bookings
    # ══════════════════════════════════════════════════════════════════════════
    with tab_mine:
        st.subheader("My Parking Reservations")

        my_bookings = [pb for pb in data["parking_bookings"] if pb["user"] == user]

        if not my_bookings:
            st.info("You have no parking bookings yet.")
        else:
            for pb in sorted(my_bookings, key=lambda x: (x["date"], x["start_time"]), reverse=True):
                is_upcoming = pb["date"] >= str(date.today())
                label_color = "🟢" if is_upcoming else "⚫"
                with st.expander(
                    f"{label_color} Slot **{pb['slot']}** — {pb['date']}  {pb['start_time']}–{pb['end_time']}  |  {pb.get('vehicle_number', pb.get('vehicle',''))}",
                    expanded=is_upcoming,
                ):
                    left, right = st.columns([2, 1])
                    with left:
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Slot", pb["slot"])
                        c2.metric("Date", pb["date"])
                        c3.metric("From", pb["start_time"])
                        c4.metric("Until", pb["end_time"])
                        st.markdown(f"**Vehicle:** {pb.get('vehicle_number', pb.get('vehicle','—'))}")
                        st.markdown(f"**Owner:** {pb.get('owner_name', user)}")
                        st.markdown(f"**Model:** {pb.get('model','—')}  ·  **Color:** {pb.get('color','—')}")
                        st.markdown(f"**Phone:** {pb.get('phone','—')}  ·  **Flat:** {pb.get('flat','—')}")
                        st.caption(f"Booking ID: `{pb['id']}`  |  Booked: {pb['booked_at'][:16]}")
                        if st.button("❌ Cancel Booking", key=f"cancel_park_{pb['id']}"):
                            data["parking_bookings"] = [x for x in data["parking_bookings"] if x["id"] != pb["id"]]
                            save_data(data)
                            st.success("Booking cancelled.")
                            st.rerun()
                    with right:
                        # Booking QR — contains full slot + owner info
                        booking_qr_payload = {
                            "type":           "parking_booking",
                            "booking_id":     pb["id"],
                            "slot":           pb["slot"],
                            "vehicle_number": pb.get("vehicle_number", pb.get("vehicle", "")),
                            "owner_name":     pb.get("owner_name", user),
                            "model":          pb.get("model", ""),
                            "color":          pb.get("color", ""),
                            "phone":          pb.get("phone", ""),
                            "flat":           pb.get("flat", ""),
                            "date":           pb["date"],
                            "start_time":     pb["start_time"],
                            "end_time":       pb["end_time"],
                            "society":        "Villa Society",
                        }
                        bqr = make_qr_bytes(booking_qr_payload)
                        st.image(bqr, caption="Booking QR", width=160)
                        st.download_button(
                            "⬇️ Download",
                            data=bqr,
                            file_name=f"booking_{pb['id']}.png",
                            mime="image/png",
                            key=f"dl_bqr_{pb['id']}",
                            use_container_width=True,
                        )

    # ══════════════════════════════════════════════════════════════════════════
    # Tab 4 — All Bookings overview
    # ══════════════════════════════════════════════════════════════════════════
    with tab_all:
        st.subheader("All Parking Reservations")

        all_pb = data["parking_bookings"]
        if not all_pb:
            st.info("No parking bookings have been made yet.")
        else:
            rows = [
                {
                    "ID":           pb["id"],
                    "Slot":         pb["slot"],
                    "User":         pb["user"],
                    "Vehicle":      pb.get("vehicle_number", pb.get("vehicle", "")),
                    "Owner":        pb.get("owner_name", pb["user"]),
                    "Model":        pb.get("model", ""),
                    "Color":        pb.get("color", ""),
                    "Flat":         pb.get("flat", ""),
                    "Date":         pb["date"],
                    "Start":        pb["start_time"],
                    "End":          pb["end_time"],
                    "Booked At":    pb["booked_at"][:16],
                }
                for pb in sorted(all_pb, key=lambda x: (x["date"], x["slot"]))
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

# ── ADMIN PANEL ───────────────────────────────────────────────────────────────
elif page == "Admin Panel":
    if st.session_state.user_role != "admin":
        st.error("Access denied. Admins only.")
        st.stop()

    st.title("🔧 Admin Panel")
    st.caption("Full platform management — only visible to admins.")

    admin_tab1, admin_tab2, admin_tab3, admin_tab4 = st.tabs(
        ["👥 Users", "🚗 Rides", "🎫 Bookings", "🅿️ Parking"]
    )

    # ── Users tab ─────────────────────────────────────────────────────────────
    with admin_tab1:
        st.subheader("Registered Users")
        users_data = data.get("users", {})
        if not users_data:
            st.info("No users registered yet.")
        else:
            user_rows = [
                {
                    "Username": uname,
                    "Role": uinfo.get("role", "—"),
                    "Society": uinfo.get("society", "—") or "None",
                    "Created": uinfo.get("created_at", "—")[:16],
                }
                for uname, uinfo in users_data.items()
            ]
            st.dataframe(pd.DataFrame(user_rows), use_container_width=True)

            st.divider()
            st.subheader("Delete a User")
            del_user_options = [u for u in users_data if u != st.session_state.current_user]
            if del_user_options:
                del_user = st.selectbox("Select user to delete", del_user_options)
                if st.button("Delete User", type="primary"):
                    del data["users"][del_user]
                    save_data(data)
                    st.success(f"User **{del_user}** deleted.")
                    st.rerun()
            else:
                st.info("No other users to delete.")

    # ── Rides tab ─────────────────────────────────────────────────────────────
    with admin_tab2:
        st.subheader("All Rides")
        if not data["rides"]:
            st.info("No rides posted yet.")
        else:
            ride_rows = [
                {
                    "ID":      r["id"],
                    "Driver":  r["driver"],
                    "From":    r["from"],
                    "To":      r["to"],
                    "Date":    r["date"],
                    "Time":    r["time"],
                    "Seats":   r["seats"],
                    "Price":   f"£{r['price_per_seat']}",
                }
                for r in data["rides"]
            ]
            st.dataframe(pd.DataFrame(ride_rows), use_container_width=True)

            st.divider()
            del_ride_id = st.selectbox("Select ride to delete", [r["id"] for r in data["rides"]])
            if st.button("Delete Ride", type="primary", key="del_ride"):
                data["rides"] = [r for r in data["rides"] if r["id"] != del_ride_id]
                data["bookings"] = [b for b in data["bookings"] if b["ride_id"] != del_ride_id]
                save_data(data)
                st.success(f"Ride **{del_ride_id}** and its bookings deleted.")
                st.rerun()

    # ── Bookings tab ──────────────────────────────────────────────────────────
    with admin_tab3:
        st.subheader("All Bookings")
        if not data["bookings"]:
            st.info("No bookings yet.")
        else:
            booking_rows = [
                {
                    "ID":        b["id"],
                    "Ride":      b["ride_id"],
                    "Passenger": b["passenger"],
                    "From":      b["from"],
                    "To":        b["to"],
                    "Date":      b["date"],
                    "Seats":     b["seats"],
                    "Price":     f"£{b['price']:.2f}",
                    "Booked":    b["booked_at"][:16],
                }
                for b in data["bookings"]
            ]
            st.dataframe(pd.DataFrame(booking_rows), use_container_width=True)

            st.divider()
            del_booking_id = st.selectbox("Select booking to delete", [b["id"] for b in data["bookings"]])
            if st.button("Delete Booking", type="primary", key="del_booking"):
                data["bookings"] = [b for b in data["bookings"] if b["id"] != del_booking_id]
                save_data(data)
                st.success(f"Booking **{del_booking_id}** deleted.")
                st.rerun()

    # ── Parking tab ───────────────────────────────────────────────────────────
    with admin_tab4:
        st.subheader("All Parking Bookings")
        all_pk = data.get("parking_bookings", [])
        if not all_pk:
            st.info("No parking bookings yet.")
        else:
            pk_rows = [
                {
                    "ID":      pb["id"],
                    "Slot":    pb["slot"],
                    "User":    pb["user"],
                    "Vehicle": pb.get("vehicle_number", ""),
                    "Date":    pb["date"],
                    "Start":   pb["start_time"],
                    "End":     pb["end_time"],
                }
                for pb in sorted(all_pk, key=lambda x: (x["date"], x["slot"]))
            ]
            st.dataframe(pd.DataFrame(pk_rows), use_container_width=True)

            st.divider()
            del_pk_id = st.selectbox("Select parking booking to delete", [pb["id"] for pb in all_pk])
            if st.button("Delete Parking Booking", type="primary", key="del_pk"):
                data["parking_bookings"] = [pb for pb in all_pk if pb["id"] != del_pk_id]
                save_data(data)
                st.success(f"Parking booking **{del_pk_id}** deleted.")
                st.rerun()
