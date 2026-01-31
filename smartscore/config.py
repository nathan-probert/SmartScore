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

# Admin client (service role key)
SUPABASE_ADMIN_AUTH_CLIENT: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
)

# Email
GMAIL_EMAIL = os.environ.get("GMAIL_EMAIL")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

BREVO_SMTP_LOGIN = os.environ.get("BREVO_SMTP_LOGIN")
BREVO_SMTP_KEY = os.environ.get("BREVO_SMTP_KEY")
BREVO_FROM_EMAIL = os.environ.get("BREVO_FROM_EMAIL")
