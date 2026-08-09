import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 7 * 24 * 3600  # 7 days
LOCAL_USERS_FILE = Path("data/users.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuthService:
    """Auth backed by Supabase Auth when configured, else a local dev fallback.

    Fallback mode keeps signup/login working without external services so the
    demo always runs; switch to Supabase by setting SUPABASE_URL +
    SUPABASE_ANON_KEY in .env (passwords are then stored by Supabase, never by us).
    """

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        self._supabase_enabled = bool(settings.supabase_url and settings.supabase_anon_key)
        self._url = settings.supabase_url.rstrip("/")
        self._anon_key = settings.supabase_anon_key
        self.local_users_file = LOCAL_USERS_FILE

    # ---------- local dev fallback helpers ----------
    def _load_local_users(self) -> dict:
        if not self.local_users_file.exists():
            return {"users": []}
        try:
            return json.loads(self.local_users_file.read_text(encoding="utf-8"))
        except Exception:
            return {"users": []}

    def _save_local_users(self, data: dict):
        self.local_users_file.parent.mkdir(parents=True, exist_ok=True)
        self.local_users_file.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()

    @staticmethod
    def _make_token(user_id: str) -> str:
        payload = base64.urlsafe_b64encode(
            json.dumps({"uid": user_id, "exp": int(time.time()) + TOKEN_TTL_SECONDS}).encode()
        ).decode().rstrip("=")
        sig = hmac.new(
            settings.auth_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{sig}"

    @staticmethod
    def _verify_token(token: str) -> Optional[str]:
        try:
            payload_b64, sig = token.split(".")
            expected = hmac.new(
                settings.auth_secret.encode(), payload_b64.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
            if int(payload["exp"]) < int(time.time()):
                return None
            return payload["uid"]
        except Exception:
            return None

    # ---------- supabase helpers ----------
    def _supabase_headers(self) -> dict:
        return {"apikey": self._anon_key, "Content-Type": "application/json"}

    async def _supabase_me(self, token: str) -> Optional[dict]:
        try:
            r = await self._client.get(
                f"{self._url}/auth/v1/user",
                headers={**self._supabase_headers(), "Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                return None
            user = r.json()
            return {"id": user["id"], "email": user["email"]}
        except Exception as e:
            logger.error(f"Supabase /user failed: {e}")
            return None

    # ---------- public API ----------
    async def signup(self, email: str, password: str) -> Optional[dict]:
        email = email.strip().lower()
        if self._supabase_enabled:
            try:
                r = await self._client.post(
                    f"{self._url}/auth/v1/signup",
                    headers=self._supabase_headers(),
                    json={"email": email, "password": password},
                )
                if r.status_code >= 400:
                    logger.error(f"Supabase signup error {r.status_code}: {r.text[:400]}")
                    return None
                data = r.json()
                access_token = data.get("access_token")
                user = data.get("user", {})
                # Email confirmation enabled -> no access_token is issued until
                # the user verifies. Surface that so the client can guide them.
                if not access_token and user.get("id"):
                    return {"confirm_required": True, "email": email, "user": user}
                if not access_token or not user.get("id"):
                    return None
                return {"token": access_token, "user": {"id": user["id"], "email": user["email"]}}
            except Exception as e:
                logger.error(f"Supabase signup error: {e}")
                return None

        # local dev fallback
        data = self._load_local_users()
        for u in data["users"]:
            if u["email"] == email:
                return None  # already registered
        salt = secrets.token_hex(8)
        uid = hashlib.sha256(f"{email}:{salt}".encode()).hexdigest()[:24]
        data["users"].append({
            "id": uid,
            "email": email,
            "password_hash": self._hash_password(password, salt),
            "salt": salt,
            "created_at": _now(),
        })
        self._save_local_users(data)
        logger.info(f"Local user created: {email}")
        return {"token": self._make_token(uid), "user": {"id": uid, "email": email}}

    async def login(self, email: str, password: str) -> Optional[dict]:
        email = email.strip().lower()
        if self._supabase_enabled:
            try:
                r = await self._client.post(
                    f"{self._url}/auth/v1/token?grant_type=password",
                    headers=self._supabase_headers(),
                    json={"email": email, "password": password},
                )
                if r.status_code >= 400:
                    return None
                data = r.json()
                user = data.get("user", {})
                return {"token": data["access_token"], "user": {"id": user["id"], "email": user["email"]}}
            except Exception as e:
                logger.error(f"Supabase login error: {e}")
                return None

        data = self._load_local_users()
        for u in data["users"]:
            if u["email"] == email and hmac.compare_digest(
                u["password_hash"], self._hash_password(password, u["salt"])
            ):
                return {"token": self._make_token(u["id"]), "user": {"id": u["id"], "email": u["email"]}}
        return None

    async def get_user(self, token: str) -> Optional[dict]:
        if not token:
            return None
        if self._supabase_enabled:
            return await self._supabase_me(token)
        uid = self._verify_token(token)
        if not uid:
            return None
        for u in self._load_local_users()["users"]:
            if u["id"] == uid:
                return {"id": u["id"], "email": u["email"]}
        return None

    async def close(self):
        await self._client.aclose()


auth_service = AuthService()