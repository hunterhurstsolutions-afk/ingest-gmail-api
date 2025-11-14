from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
import os
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import firebase_admin
from firebase_admin import firestore, credentials
import logging

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# === CONFIG ===
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
PROJECT_ID = os.getenv("PROJECT_ID", "ingest-gmail-api")
CLOUD_RUN_URL = os.getenv("CLOUD_RUN_URL", "http://localhost:8080")

# Initialize Firebase
if not firebase_admin._apps:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {"projectId": PROJECT_ID})
db = firestore.client()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets"
]

REDIRECT_URI = f"{CLOUD_RUN_URL}/auth/callback"

# === ROUTES ===
@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <h1>Gmail Leads Ingest</h1>
    <p><a href="/install">Connect Your Gmail & Sheets</a></p>
    """

@app.get("/install", response_class=HTMLResponse)
async def install(request: Request):
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
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return f'<h2>Connect Gmail</h2><p><a href="{auth_url}">Click to Authorize</a></p>'

@app.get("/auth/callback")
async def callback(request: Request, code: str = None, error: str = None):
    if error:
        raise HTTPException(status_code=400, detail=error)
    if not code:
        raise HTTPException(status_code=400, detail="No code")

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
    flow.fetch_token(authorization_response=str(request.url))

    creds = flow.credentials
    # Get user email
    service = build("oauth2", "v2", credentials=creds)
    user_info = service.userinfo().get().execute()
    email = user_info["email"]

    # Save to Firestore
    db.collection("users").document(email).set({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "saved_at": firestore.SERVER_TIMESTAMP
    }, merge=True)

    # === Set up Gmail Watch ===
    gmail = build("gmail", "v1", credentials=creds)
    try:
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
        logging.warning(f"Watch failed (maybe already set): {e}")

    return HTMLResponse(f"""
    <h2>Success!</h2>
    <p>Connected: <strong>{email}</strong></p>
    <p>Tokens saved. Gmail watch active.</p>
    <p>You can close this tab.</p>
    """)