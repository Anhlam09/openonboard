"""All settings come from environment variables (.env)."""

import os

from dotenv import load_dotenv

load_dotenv()

MIDDESK_API_KEY = os.getenv("MIDDESK_API_KEY", "").strip()
MIDDESK_BASE_URL = os.getenv("MIDDESK_BASE_URL", "https://api-sandbox.middesk.com/v1").rstrip("/")
# Middesk's API reference uses "Authorization: Bearer <key>". Set to "basic" only if your account needs it.
MIDDESK_AUTH = os.getenv("MIDDESK_AUTH", "bearer").strip().lower()
# The KYB package to order. Leave blank to use your Middesk account's default package.
MIDDESK_ORDER_PRODUCT = os.getenv("MIDDESK_ORDER_PRODUCT", "business_verification_verify").strip()
MIDDESK_WEBHOOK_SECRET = os.getenv("MIDDESK_WEBHOOK_SECRET", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "openonboard.db")
DOCUMENTS_DIR = os.getenv("DOCUMENTS_DIR", "uploads")
# One shared password for the reviewer side (queue, decisions, maintenance, exports).
# Blank in mock mode = reviewer pages are open, for demos. Blank with a Middesk key = reviewer pages refuse to load.
REVIEWER_PASSWORD = os.getenv("REVIEWER_PASSWORD", "")

# Set only by web/bridge.py, when the app runs inside the visitor's browser (the static GitHub Pages demo).
IN_BROWSER = os.getenv("IN_BROWSER", "").strip().lower() == "true"

MOCK_MODE = MIDDESK_API_KEY == ""
