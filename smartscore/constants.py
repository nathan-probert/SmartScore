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

DAYS_TO_KEEP_HISTORIC_DATA = 7

# Prediction weights
# old weights
# weights = make_predictions_rust.Weights(
#     gpg=0.3,
#     five_gpg=0.4,
#     hgpg=0.3,
#     tgpg=0.0,
#     otga=0.0,
#     hppg_otshga=0.0,
#     is_home=0.0,
# )

# For the start of the season, we will temporiarly increase the weight of hgpg
WEIGHTS = make_predictions_rust.Weights(
    gpg=0.76 - 0.3,
    hgpg=0.06 + 0.3,
    five_gpg=0.0,
    tgpg=0.0,
    otga=0.04,
    is_home=0.06,
    hppg_otshga=0.08,
)
