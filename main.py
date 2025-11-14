from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
import os
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import firebase_admin
from firebase_admin import firestore
import logging
from typing import Dict

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# === CONFIG ===
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
PROJECT_ID = os.getenv("PROJECT_ID", "ingest-gmail-api")
CLOUD_RUN_URL = os.getenv("CLOUD_RUN_URL", "http://localhost:8080")

# Global state storage (in-memory, per instance)
STATE_STORE: Dict[str, str] = {}

# Initialize Firebase with default credentials
if not firebase_admin._apps:
    firebase_admin.initialize_app()
db = firestore.client()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets"
]

REDIRECT_URI = f"{CLOUD_RUN_URL}/auth/callback"

@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <h1>Gmail Leads Ingest</h1>
    <p><a href="/install">Connect Your Gmail & Sheets</a></p>
    """

@app.get("/install", response_class=HTMLResponse)
async def install(request: Request):
    if not CLIENT_ID or not CLIENT_SECRET:
        logging.error("Missing CLIENT_ID or CLIENT_SECRET")
        raise HTTPException(500, "Server misconfigured")

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI]
            }
        },
        scopes=SCOPES
    )
    flow.redirect_uri = REDIRECT_URI
    auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
    
    # Store state with a random key
    import uuid
    state_key = str(uuid.uuid4())
    STATE_STORE[state_key] = state
    #Pass state_key as query param instead of appending to auth_url
    return f'<h2>Connect Gmail</h2><p><a href="{auth_url}?my_state_key={state_key}">Click to Authorize</a></p>'

@app.get("/auth/callback")
async def callback(request: Request, code: str = None, state: str = None, error: str = None):
    if error:
        logging.error(f"OAuth error: {error}")
        raise HTTPException(400, f"OAuth error: {error}")
    if not code or not state:
        raise HTTPException(400, "Missing code or state")

    # Get my_state_key from callback URL
    my_state_key = request.query_params.get('my_state_key')
    if not my_state_key or my_state_key not in STATE_STORE:
        logging.error(f"Invalid or expired state: {my_state_key}")
        raise HTTPException(400, "Invalid state")
    expected_state = STATE_STORE.pop(my_state_key)  # Remove after use

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI]
            }
        },
        scopes=SCOPES
    )
    flow.redirect_uri = REDIRECT_URI

    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as e:
        logging.error(f"Token fetch failed: {e}")
        raise HTTPException(500, "Failed to get token")

    creds = flow.credentials

    # Get user email
    try:
        service = build("oauth2", "v2", credentials=creds)
        user_info = service.userinfo().get().execute()
        email = user_info["email"]
    except Exception as e:
        logging.error(f"User info failed: {e}")
        raise HTTPException(500, "Failed to get user info")

    # Save to Firestore
    try:
        db.collection("users").document(email).set({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
            "saved_at": firestore.SERVER_TIMESTAMP
        }, merge=True)
        logging.info(f"Tokens saved for {email}")
    except Exception as e:
        logging.error(f"Firestore save failed: {e}")

    # Set up Gmail watch
    try:
        gmail = build("gmail", "v1", credentials=creds)
        gmail.users().watch(
            userId="me",
            body={
                "topicName": f"projects/{PROJECT_ID}/topics/gmail-push",
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "INCLUDE"
            }
        ).execute()
        logging.info(f"Gmail watch set for {email}")
    except Exception as e:
        logging.warning(f"Watch failed (may already exist): {e}")

    return HTMLResponse(f"""
    <h2>SUCCESS!</h2>
    <p>Connected: <strong>{email}</strong></p>
    <p>Tokens saved to Firestore.</p>
    <p>Gmail push active.</p>
    <p>You can close this tab.</p>
    """)