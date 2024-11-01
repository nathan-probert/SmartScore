from config import ENV

DRAFTKINGS_NHL_ID = 42133
DRAFTKINGS_GOAL_SCORER_CATEGORY = 1190
DRAFTKINGS_PROVIDER_ID = 2

DB_URL = (
    "https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/players"
    if ENV == "prod"
    else "https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/players_dev"
)
