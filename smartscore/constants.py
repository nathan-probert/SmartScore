import make_predictions_rust

DRAFTKINGS_NHL_ID = 42133
DRAFTKINGS_GOAL_SCORER_CATEGORY = 1190
DRAFTKINGS_PROVIDER_ID = 2

# Expected number of players to choose in a game
NUM_EXPECTED_PLAYERS = 3

# Add constant for current pick accuracy
CURRENT_PICK_ACCURACY = "current_pick_accuracy"

# Prefix for per-season pick accuracy rows, e.g. "season_pick_accuracy_20252026".
# Lifetime row above is left untouched; season rows live alongside it in Metrics-{ENV}.
SEASON_PICK_ACCURACY_PREFIX = "season_pick_accuracy_"

# Month (1-12) at which a new NHL season id starts. Aug-Dec -> f"{year}{year+1}".
SEASON_CUTOFF_MONTH = 8

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
