"""Configuration loader for Veronica Mars voice AI agent."""

import os
from dotenv import load_dotenv

load_dotenv()

# SignalWire
SIGNALWIRE_PROJECT_ID = os.getenv("SIGNALWIRE_PROJECT_ID", "")
SIGNALWIRE_TOKEN = os.getenv("SIGNALWIRE_TOKEN", "")
SIGNALWIRE_SPACE = os.getenv("SIGNALWIRE_SPACE", "")
SIGNALWIRE_PHONE_NUMBER = os.getenv("SIGNALWIRE_PHONE_NUMBER", "")
SWML_BASIC_AUTH_USER = os.getenv("SWML_BASIC_AUTH_USER", "")
SWML_BASIC_AUTH_PASSWORD = os.getenv("SWML_BASIC_AUTH_PASSWORD", "")

# Trestle Reverse Phone API
TRESTLE_API_KEY = os.getenv("TRESTLE_API_KEY", "")
TRESTLE_BASE_URL = os.getenv("TRESTLE_BASE_URL", "https://api.trestleiq.com/3.2")

# ZeroBounce Email Validation
ZEROBOUNCE_API_KEY = os.getenv("ZEROBOUNCE_API_KEY", "")
ZEROBOUNCE_BASE_URL = os.getenv("ZEROBOUNCE_BASE_URL", "https://api.zerobounce.net/v2")

# Google Maps Geocoding (Phase 2)
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# Smarty US Street Address (Phase 2)
SMARTY_AUTH_ID = os.getenv("SMARTY_AUTH_ID", "")
SMARTY_AUTH_TOKEN = os.getenv("SMARTY_AUTH_TOKEN", "")

# Postmark (Phase 4)
POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "")
POSTMARK_FROM_EMAIL = os.getenv("POSTMARK_FROM_EMAIL", "")

# AI Model
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")

# Server
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3000"))

# Caller record TTL (days)
TTL_ADDRESS_DAYS = int(os.getenv("TTL_ADDRESS_DAYS", "90"))
TTL_EMAIL_DAYS = int(os.getenv("TTL_EMAIL_DAYS", "180"))
TTL_LINE_TYPE_DAYS = int(os.getenv("TTL_LINE_TYPE_DAYS", "180"))


def validate():
    """Warn about missing required configuration."""
    missing = []
    if not TRESTLE_API_KEY:
        missing.append("TRESTLE_API_KEY")
    if not ZEROBOUNCE_API_KEY:
        missing.append("ZEROBOUNCE_API_KEY")
    if not SIGNALWIRE_PHONE_NUMBER:
        missing.append("SIGNALWIRE_PHONE_NUMBER")
    if missing:
        print(f"WARNING: Missing config: {', '.join(missing)}")
        print("Some features may not work. Copy .env.example to .env and fill in values.")
