from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
import os
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import firebase_admin
from firebase_admin import firestore, credentials
import uvicorn

app = FastAPI()

# ────── CONFIG ──────
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
PROJECT_ID = os.getenv("PROJECT_ID") or "ingest-gmail-api"

# Initialize Firebase (Firestore)
if not firebase_admin._apps:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {
        'projectId': PROJECT_ID,
    })
db = firestore.client()

# OAuth scopes we need
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://mail.google.com/'  # for push notifications
]

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <h1>Gmail → Sheets Lead Ingest</h1>
    <p>Ready for agents to install.</p>
    """

# ────── OAuth Flow ──────
@app.get("/install")
async def install():
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"https://{os.getenv('CLOUD_RUN_URL','') or 'YOUR_URL.run.app'}/auth/callback"]
            }
        },
        scopes=SCOPES
    )
    flow.redirect_uri = f"https://{request.headers.get('host')}/auth/callback"
    auth_url, _ = flow.authorization_url(prompt='consent')
    return HTMLResponse(f'<h2>Connect Your Gmail & Sheets</h2><p><a href="{auth_url}">Click here to authorize</a></p>')

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
                "token_uri": "https://oauth2.googleapis.com/token"
            }
        },
        scopes=SCOPES
    )
    flow.redirect_uri = f"https://{request.headers.get('host')}/auth/callback"
    flow.fetch_token(code=code)
    
    creds = flow.credentials
    user_id = creds.id_token['sub'] if creds.id_token else "unknown"
    
    db.collection('agents').document(user_id).set({
        'refresh_token': creds.refresh_token,
        'installed_at': firestore.SERVER_TIMESTAMP
    }, merge=True)
    
    return HTMLResponse("<h1>Success!</h1><p>You’re connected. You can close this tab.</p>")
