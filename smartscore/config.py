"""
ENV: Environment (dev, prod)
"""

import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

ENV = os.environ.get("ENV", "dev")

SUPABASE_API_KEY = os.environ.get("SUPABASE_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

# Default client (anon key)
SUPABASE_CLIENT: Client = create_client(SUPABASE_URL, SUPABASE_API_KEY)

# Service role client. Bypasses RLS, so it can reach tables with no public
# policies (Player-Snapshots) and call auth RPCs (get_opted_in_emails).
SUPABASE_ADMIN_CLIENT: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
)

# Email
GMAIL_EMAIL = os.environ.get("GMAIL_EMAIL")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

BREVO_SMTP_LOGIN = os.environ.get("BREVO_SMTP_LOGIN")
BREVO_SMTP_KEY = os.environ.get("BREVO_SMTP_KEY")
BREVO_FROM_EMAIL = os.environ.get("BREVO_FROM_EMAIL")
