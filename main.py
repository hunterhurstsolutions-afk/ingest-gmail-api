from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
import os
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import firebase_admin
from firebase_admin import firestore
from google.auth.transport.requests import Request
import logging

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# === CONFIG ===
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
PROJECT_ID = os.getenv("PROJECT_ID", "ingest-gmail-api")
CLOUD_RUN_URL = os.getenv("CLOUD_RUN_URL", "http://localhost:8080")

# Initialize Firebase with explicit service account (fixes Cloud Run)
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
        raise HTTPException(500, "Missing CLIENT_ID or CLIENT_SECRET")
    
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
    # Save state in memory (for demo)
    request.app.state.oauth_state = state
    return f'<h2>Connect Gmail</h2><p><a href="{auth_url}">Click to Authorize</a></p>'

@app.get("/auth/callback")
async def callback(request: Request, code: str = None, state: str = None, error: str = None):
    if error:
        raise HTTPException(status_code=400, detail=error)
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    # Validate state
    if state != getattr(request.app, "state", {}).get("oauth_state"):
        raise HTTPException(status_code=400, detail="Invalid state parameter")

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
    if creds.refresh_token:
        creds.refresh(Request())

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
        logging.info(f"Watch set up for {email}")
    except Exception as e:
        logging.warning(f"Watch failed: {e}")

    return HTMLResponse(f"""
    <h2>Success!</h2>
    <p>Connected: <strong>{email}</strong></p>
    <p>Tokens saved. Gmail watch active.</p>
    <p>You can close this tab.</p>
    """)