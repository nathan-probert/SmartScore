"""
ENV: Environment (dev, prod)
"""

import os

from dotenv import load_dotenv

load_dotenv()

ENV = os.environ.get("ENV", "dev")

SUPABASE_API_KEY = os.environ.get("SUPABASE_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
