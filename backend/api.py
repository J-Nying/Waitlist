import os
import logging
import threading
import time
from typing import Optional
import requests
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine, text
from twilio.rest import Client as TwilioClient
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv

from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

"""
/c:/Users/jeane/Desktop/github/waitlist/backend/api.py

Environment variables (required):
- KEYCLOAK_BASE_URL e.g. "http://localhost:8080"
- KEYCLOAK_REALM       target realm where the user will be created
- KEYCLOAK_ADMIN_REALM realm used for admin token (usually "master")
- KEYCLOAK_ADMIN_USERNAME
- KEYCLOAK_ADMIN_PASSWORD
- KEYCLOAK_CLIENT_ID    (default "admin-cli")
- DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
- TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
- SENDGRID_API_KEY, SENDGRID_FROM_EMAIL
"""

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Keycloak config ───────────────────────────────────────────────────────────
KEYCLOAK_BASE        = os.getenv("KEYCLOAK_BASE_URL", "http://localhost:8083").rstrip("/")
KEYCLOAK_REALM       = os.getenv("KEYCLOAK_REALM", "master")
KEYCLOAK_ADMIN_REALM = os.getenv("KEYCLOAK_ADMIN_REALM", "master")
KEYCLOAK_ADMIN_USER  = os.getenv("KEYCLOAK_ADMIN_USERNAME")
KEYCLOAK_ADMIN_PASS  = os.getenv("KEYCLOAK_ADMIN_PASSWORD")
KEYCLOAK_CLIENT_ID   = os.getenv("KEYCLOAK_CLIENT_ID", "admin-cli")

if not (KEYCLOAK_ADMIN_USER and KEYCLOAK_ADMIN_PASS):
    log.warning("KEYCLOAK_ADMIN_USERNAME or KEYCLOAK_ADMIN_PASSWORD not set; token requests will fail")

# ── Database config ───────────────────────────────────────────────────────────
DB_URL = (
    f"mysql+pymysql://{os.getenv('DB_USER', 'root')}:{os.getenv('DB_PASSWORD', '')}"
    f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '3306')}"
    f"/{os.getenv('DB_NAME', 'waitlist_db')}"
)
engine = create_engine(DB_URL)

# ── Twilio (SMS) ──────────────────────────────────────────────────────────────
twilio_client = TwilioClient(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)
TWILIO_FROM = os.getenv("TWILIO_FROM_NUMBER")

# ── SendGrid (Email) ──────────────────────────────────────────────────────────
sendgrid_client = SendGridAPIClient(os.getenv("SENDGRID_API_KEY"))
SENDGRID_FROM   = os.getenv("SENDGRID_FROM_EMAIL")

app = FastAPI(title="Waitlist API")


# ── Pydantic models ───────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str
    email: Optional[EmailStr] = None
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True
    emailVerified: bool = False

class WaitlistJoin(BaseModel):
    product_id: int
    email: Optional[EmailStr] = None
    phone: Optional[str] = None

class ProductCreate(BaseModel):
    shop_id: int
    name: str
    description: Optional[str] = None
    in_stock: bool = False

class StockUpdate(BaseModel):
    in_stock: bool


# ── Keycloak helpers (your existing code) ─────────────────────────────────────

def get_admin_token() -> str:
    token_url = f"{KEYCLOAK_BASE}/realms/{KEYCLOAK_ADMIN_REALM}/protocol/openid-connect/token"
    data = {
        "grant_type": "password",
        "client_id": KEYCLOAK_CLIENT_ID,
        "username": KEYCLOAK_ADMIN_USER,
        "password": KEYCLOAK_ADMIN_PASS,
    }
    resp = requests.post(token_url, data=data, timeout=10)
    if resp.status_code != 200:
        log.error("Failed to obtain admin token: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to obtain Keycloak admin token")
    return resp.json()["access_token"]


def create_keycloak_user(token: str, realm: str, payload: dict) -> str:
    url = f"{KEYCLOAK_BASE}/admin/realms/{realm}/users"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    if resp.status_code == 201:
        location = resp.headers.get("Location", "")
        user_id = location.rstrip("/").split("/")[-1] if location else ""
        log.info("Created user %s (id=%s)", payload.get("username"), user_id)
        return user_id
    if resp.status_code == 409:
        log.warning("User already exists: %s", payload.get("username"))
        qurl = f"{KEYCLOAK_BASE}/admin/realms/{realm}/users"
        r = requests.get(qurl, params={"username": payload.get("username")}, headers=headers, timeout=10)
        if r.status_code == 200 and r.json():
            return r.json()[0].get("id")
        raise HTTPException(status_code=409, detail="User already exists")
    log.error("Error creating user: %s %s", resp.status_code, resp.text)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to create user in Keycloak")


def set_user_password(token: str, realm: str, user_id: str, password: str, temporary: bool = False) -> None:
    if not password:
        return
    url = f"{KEYCLOAK_BASE}/admin/realms/{realm}/users/{user_id}/reset-password"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"type": "password", "value": password, "temporary": temporary}
    resp = requests.put(url, json=body, headers=headers, timeout=10)
    if resp.status_code not in (204,):
        log.error("Failed to set password for user %s: %s %s", user_id, resp.status_code, resp.text)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to set user password")


# ── Keycloak user sync ────────────────────────────────────────────────────────

def sync_keycloak_users():
    """Polls Keycloak every 30 seconds and syncs users to MySQL."""
    while True:
        try:
            token = get_admin_token()
            url = f"{KEYCLOAK_BASE}/admin/realms/{KEYCLOAK_REALM}/users"
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.get(url, headers=headers, timeout=10)

            if resp.status_code != 200:
                log.error("Failed to fetch Keycloak users: %s", resp.status_code)
            else:
                users = resp.json()
                with engine.connect() as conn:
                    for user in users:
                        conn.execute(text("""
                            INSERT INTO users (keycloak_user_id, username, email, first_name, last_name)
                            VALUES (:id, :username, :email, :first_name, :last_name)
                            ON DUPLICATE KEY UPDATE email = VALUES(email)
                        """), {
                            "id":         user.get("id"),
                            "username":   user.get("username"),
                            "email":      user.get("email"),
                            "first_name": user.get("firstName"),
                            "last_name":  user.get("lastName"),
                        })
                    conn.commit()
                log.info("✅ Synced %d users from Keycloak", len(users))
        except Exception as e:
            log.error("❌ Keycloak sync error: %s", e)

        time.sleep(30)


# Start sync in background thread on startup
@app.on_event("startup")
def start_sync():
    thread = threading.Thread(target=sync_keycloak_users, daemon=True)
    thread.start()
    log.info("🔄 Keycloak user sync started")


# ── Notification helpers ──────────────────────────────────────────────────────

def send_email(to_email: str, product_name: str):
    try:
        message = Mail(
            from_email=SENDGRID_FROM,
            to_emails=to_email,
            subject=f"✅ {product_name} is back in stock!",
            html_content=f"""
                <h2>{product_name} is back in stock!</h2>
                <p>Good news! The item you were waiting for is now available.</p>
                <p>Head to the shop before it sells out again!</p>
            """
        )
        sendgrid_client.send(message)
        log.info("📧 Email sent to %s", to_email)
    except Exception as e:
        log.error("Email error: %s", e)


def send_sms(to_phone: str, product_name: str):
    try:
        twilio_client.messages.create(
            body=f"✅ Good news! {product_name} is back in stock. Grab it before it sells out!",
            from_=TWILIO_FROM,
            to=to_phone
        )
        log.info("📱 SMS sent to %s", to_phone)
    except Exception as e:
        log.error("SMS error: %s", e)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Keycloak user creation ────────────────────────────────────────────────────

@app.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(u: UserCreate):
    token = get_admin_token()
    payload = {k: v for k, v in {
        "username":      u.username,
        "email":         u.email,
        "firstName":     u.firstName,
        "lastName":      u.lastName,
        "enabled":       u.enabled,
        "emailVerified": u.emailVerified,
    }.items() if v is not None}
    user_id = create_keycloak_user(token, KEYCLOAK_REALM, payload)
    if u.password:
        set_user_password(token, KEYCLOAK_REALM, user_id, u.password, temporary=False)
    return {"id": user_id, "username": u.username}


# ── Products ──────────────────────────────────────────────────────────────────

@app.post("/products", status_code=201)
def create_product(p: ProductCreate):
    with engine.connect() as conn:
        result = conn.execute(text(
            "INSERT INTO products (shop_id, name, description, in_stock) VALUES (:shop_id, :name, :desc, :in_stock)"
        ), {"shop_id": p.shop_id, "name": p.name, "desc": p.description, "in_stock": p.in_stock})
        conn.commit()
        return {"id": result.lastrowid, "name": p.name}


@app.get("/products")
def get_products(shop_id: Optional[int] = None):
    with engine.connect() as conn:
        if shop_id:
            rows = conn.execute(text("SELECT * FROM products WHERE shop_id = :shop_id"), {"shop_id": shop_id})
        else:
            rows = conn.execute(text("SELECT * FROM products"))
        return [dict(r._mapping) for r in rows]


# ── Stock update ──────────────────────────────────────────────────────────────

@app.patch("/products/{product_id}/stock")
def update_stock(product_id: int, body: StockUpdate):
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM products WHERE id = :id"), {"id": product_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")

        product = dict(row._mapping)

        conn.execute(text(
            "UPDATE products SET in_stock = :in_stock WHERE id = :id"
        ), {"in_stock": body.in_stock, "id": product_id})
        conn.commit()

        # Notify waitlist if coming back in stock
        if body.in_stock and not product["in_stock"]:
            waitlist = conn.execute(text(
                "SELECT * FROM product_waitlist WHERE product_id = :pid AND notified = FALSE"
            ), {"pid": product_id}).fetchall()

            for entry in waitlist:
                e = dict(entry._mapping)
                if e.get("email"):
                    send_email(e["email"], product["name"])
                if e.get("phone"):
                    send_sms(e["phone"], product["name"])

            conn.execute(text(
                "UPDATE product_waitlist SET notified = TRUE WHERE product_id = :pid"
            ), {"pid": product_id})
            conn.commit()

            log.info("✅ Notified %d people for product %d", len(waitlist), product_id)

        return {"product_id": product_id, "in_stock": body.in_stock}


# ── Waitlist ──────────────────────────────────────────────────────────────────

@app.post("/waitlist", status_code=201)
def join_waitlist(entry: WaitlistJoin):
    if not entry.email and not entry.phone:
        raise HTTPException(status_code=400, detail="Email or phone number is required")

    with engine.connect() as conn:
        product = conn.execute(text("SELECT * FROM products WHERE id = :id"), {"id": entry.product_id}).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")

        existing = conn.execute(text(
            "SELECT id FROM product_waitlist WHERE product_id = :pid AND email = :email"
        ), {"pid": entry.product_id, "email": entry.email}).fetchone()

        if existing:
            raise HTTPException(status_code=409, detail="Already on waitlist for this product")

        conn.execute(text(
            "INSERT INTO product_waitlist (product_id, email, phone) VALUES (:pid, :email, :phone)"
        ), {"pid": entry.product_id, "email": entry.email, "phone": entry.phone})
        conn.commit()

    return {"message": "Added to waitlist successfully"}


@app.get("/waitlist/{product_id}")
def get_waitlist(product_id: int):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT * FROM product_waitlist WHERE product_id = :pid ORDER BY joined_at DESC"
        ), {"pid": product_id})
        return [dict(r._mapping) for r in rows]


@app.delete("/waitlist/{entry_id}")
def leave_waitlist(entry_id: int):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM product_waitlist WHERE id = :id"), {"id": entry_id})
        conn.commit()
    return {"message": "Removed from waitlist"}