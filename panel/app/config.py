import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI", "http://localhost:8000/oauth/callback")

PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
VIDEO_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "panel.db"

WORKER_INTERVAL = int(os.getenv("WORKER_INTERVAL", "10"))
