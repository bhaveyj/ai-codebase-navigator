"""GitHub OAuth identity and opaque, server-side sessions.

GitHub credentials are exchanged only by the API. The browser receives an
unpredictable session cookie; only its hash is stored in MongoDB.
"""
import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse

from .contracts import DomainError

SESSION_COOKIE = "navigator_session"
GUEST_COOKIE = "navigator_guest"
STATE_COOKIE = "navigator_oauth_state"
SESSION_SECONDS = 14 * 86400
GUEST_SECONDS = 30 * 86400
STATE_SECONDS = 600


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def secure_cookie(settings) -> bool:
    return urlsplit(settings.github_oauth_redirect_uri).scheme == "https"


def _cookie(response, name: str, value: str, settings, max_age: int):
    response.set_cookie(name, value, max_age=max_age, httponly=True,
                        secure=secure_cookie(settings), samesite="lax", path="/")


def start_login(store, settings):
    if not settings.github_oauth_client_id or not settings.github_oauth_client_secret:
        raise DomainError("AUTH_NOT_CONFIGURED", "GitHub sign-in has not been configured.", 503)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = hashlib.sha256(verifier.encode()).digest()
    import base64
    challenge = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()
    state_record = {"id": digest(state), "verifier": verifier,
                    "expiresAt": int(time.time()) + STATE_SECONDS}
    if store.is_mongo:
        state_record["deleteAfter"] = datetime.now(timezone.utc) + timedelta(seconds=STATE_SECONDS)
    store.put("oauth_states", state_record)
    query = urlencode({"client_id": settings.github_oauth_client_id,
                       "redirect_uri": settings.github_oauth_redirect_uri,
                       "state": state, "scope": "", "code_challenge": challenge,
                       "code_challenge_method": "S256"})
    response = RedirectResponse(f"https://github.com/login/oauth/authorize?{query}", status_code=302)
    _cookie(response, STATE_COOKIE, state, settings, STATE_SECONDS)
    return response


def finish_login(store, settings, state: str, cookie_state: str | None, code: str,
                 guest_token: str | None = None):
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise DomainError("INVALID_OAUTH_STATE", "GitHub sign-in expired. Please try again.", 400)
    saved = store.pop("oauth_states", {"id": digest(state)})
    if not saved or saved["expiresAt"] < int(time.time()):
        raise DomainError("INVALID_OAUTH_STATE", "GitHub sign-in expired. Please try again.", 400)
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            token_response = client.post("https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"}, data={
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret,
                    "code": code, "redirect_uri": settings.github_oauth_redirect_uri,
                    "code_verifier": saved["verifier"]})
            token_response.raise_for_status()
            token = token_response.json().get("access_token")
            if not token:
                raise ValueError("Missing GitHub access token")
            user_response = client.get("https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"})
            user_response.raise_for_status()
            github_user = user_response.json()
            github_id = int(github_user["id"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        raise DomainError("GITHUB_SIGN_IN_FAILED", "GitHub sign-in could not be completed. Please retry.", 503) from error
    user_id = f"github:{github_id}"
    store.ensure("users", {"id": user_id, "githubId": github_id,
                           "login": github_user.get("login", ""), "avatarUrl": github_user.get("avatar_url", ""),
                           "questionsUsed": 0})
    store.update("users", {"id": user_id}, {"login": github_user.get("login", ""),
                                             "avatarUrl": github_user.get("avatar_url", "")})
    guest = guest_for_token(store, guest_token)
    if guest:
        for grant in store.find("user_analyses", {"userId": guest["id"]}):
            store.ensure("user_analyses", {**grant, "id": f"{user_id}:{grant['analysisId']}",
                                            "userId": user_id})
        for turn in store.find("chat_turns", {"userId": guest["id"]}):
            store.ensure("chat_turns", {**turn, "id": f"migrated:{user_id}:{turn['id']}",
                                        "userId": user_id})
    session = secrets.token_urlsafe(48)
    session_record = {"id": digest(session), "userId": user_id,
                      "expiresAt": int(time.time()) + SESSION_SECONDS}
    if store.is_mongo:
        session_record["deleteAfter"] = datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)
    store.put("auth_sessions", session_record)
    response = RedirectResponse("/", status_code=302)
    _cookie(response, SESSION_COOKIE, session, settings, SESSION_SECONDS)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


def current_user(store, request: Request, settings):
    if settings.mode == "local":
        return {"id": "local-dev", "login": "Local developer", "questionsUsed": 0}
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    session = store.one("auth_sessions", {"id": digest(token)})
    if not session or session["expiresAt"] <= int(time.time()):
        return None
    return store.one("users", {"id": session["userId"]})


def guest_for_token(store, token: str | None):
    if not token:
        return None
    session = store.one("guest_sessions", {"id": digest(token)})
    if not session or session["expiresAt"] <= int(time.time()):
        return None
    return store.one("users", {"id": session["userId"]})


def current_guest(store, request: Request):
    return guest_for_token(store, request.cookies.get(GUEST_COOKIE))


def ensure_guest(store, request: Request, response, settings):
    guest = current_guest(store, request)
    if guest:
        return guest
    token = secrets.token_urlsafe(48)
    guest = store.ensure("users", {"id": f"guest:{secrets.token_urlsafe(24)}",
                                    "login": "Guest", "questionsUsed": 0, "guest": True})
    session = {"id": digest(token), "userId": guest["id"],
               "expiresAt": int(time.time()) + GUEST_SECONDS}
    if store.is_mongo:
        session["deleteAfter"] = datetime.now(timezone.utc) + timedelta(seconds=GUEST_SECONDS)
    store.put("guest_sessions", session)
    _cookie(response, GUEST_COOKIE, token, settings, GUEST_SECONDS)
    return guest


def current_identity(store, request: Request, settings):
    return current_user(store, request, settings) or current_guest(store, request)


def require_identity(store, request: Request, settings):
    identity = current_identity(store, request, settings)
    if not identity:
        raise DomainError("AUTH_REQUIRED", "Open the workspace to start a guest session or sign in.", 401)
    return identity


def require_user(store, request, settings):
    user = current_user(store, request, settings)
    if not user:
        raise DomainError("AUTH_REQUIRED", "Sign in with GitHub to continue.", 401)
    return user


def logout(store, request, settings):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        store.delete("auth_sessions", {"id": digest(token)})
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
