import make_predictions_rust

from config import ENV

DRAFTKINGS_NHL_ID = 42133
DRAFTKINGS_GOAL_SCORER_CATEGORY = 1190
DRAFTKINGS_PROVIDER_ID = 2

PREDICTIONS_API_URL = (
    "https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/players"
    if ENV == "prod"
    else "https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/players_dev"
)

HISTORY_API_URL = (
    "https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/historic_picks"
    if ENV == "prod"
    else "https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/historic_picks_dev"
)

LAMBDA_API_NAME = f"Api-{ENV}"

# Expected number of players to choose in a game
NUM_EXPECTED_PLAYERS = 3

# Add constant for current pick accuracy
CURRENT_PICK_ACCURACY = "current_pick_accuracy"

# This includes the current day
DAYS_TO_KEEP_HISTORIC_DATA = 8

# Prediction weights
WEIGHTS = make_predictions_rust.Weights(
    gpg=0.190,
    five_gpg=0.060,
    hgpg=0.600,
    tgpg=0.110,
    otga=0.040,
    hppg_otshga=0.000,
    is_home=0.000,
)
