import os
import logging
from typing import Optional
import requests
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, EmailStr

"""
/c:/Users/jeane/Desktop/github/waitlist/backend/api.py

Simple FastAPI-based endpoint to create a Keycloak user via the Keycloak Admin REST API.

Environment variables (required):
- KEYCLOAK_BASE_URL e.g. "http://localhost:8080"
- KEYCLOAK_REALM       target realm where the user will be created
- KEYCLOAK_ADMIN_REALM realm used for admin token (usually "master")
- KEYCLOAK_ADMIN_USERNAME
- KEYCLOAK_ADMIN_PASSWORD
- KEYCLOAK_CLIENT_ID    (default "admin-cli")
"""



logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KEYCLOAK_BASE = os.getenv("KEYCLOAK_BASE_URL", "http://localhost:8080").rstrip("/")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "master")
KEYCLOAK_ADMIN_REALM = os.getenv("KEYCLOAK_ADMIN_REALM", "master")
KEYCLOAK_ADMIN_USER = os.getenv("KEYCLOAK_ADMIN_USERNAME")
KEYCLOAK_ADMIN_PASS = os.getenv("KEYCLOAK_ADMIN_PASSWORD")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "admin-cli")

if not (KEYCLOAK_ADMIN_USER and KEYCLOAK_ADMIN_PASS):
    log.warning("KEYCLOAK_ADMIN_USERNAME or KEYCLOAK_ADMIN_PASSWORD not set; token requests will fail")

app = FastAPI(title="Keycloak User API")


class UserCreate(BaseModel):
    username: str
    email: Optional[EmailStr] = None
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True
    emailVerified: bool = False


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
        # user exists
        log.warning("User already exists: %s", payload.get("username"))
        # try to find existing user id by search
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


@app.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(u: UserCreate):
    token = get_admin_token()
    payload = {
        "username": u.username,
        "email": u.email,
        "firstName": u.firstName,
        "lastName": u.lastName,
        "enabled": u.enabled,
        "emailVerified": u.emailVerified,
    }
    # remove None values
    payload = {k: v for k, v in payload.items() if v is not None}
    user_id = create_keycloak_user(token, KEYCLOAK_REALM, payload)
    if u.password:
        set_user_password(token, KEYCLOAK_REALM, user_id, u.password, temporary=False)
    return {"id": user_id, "username": u.username}


# Optional health check
@app.get("/health")
def health():
    return {"status": "ok"}