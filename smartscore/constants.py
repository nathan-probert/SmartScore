from config import ENV

DRAFTKINGS_NHL_ID = 42133
DRAFTKINGS_GOAL_SCORER_CATEGORY = 1190
DRAFTKINGS_PROVIDER_ID = 2

DB_URL = (
    "https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/players"
    if ENV == "prod"
    else "https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/players_dev"
)

FACEOFF_API_TEAM_URLS = {
    1: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/new-jersey-devils/line-combinations.json?slug=new-jersey-devils",
    2: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/new-york-islanders/line-combinations.json?slug=new-york-islanders",
    3: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/new-york-rangers/line-combinations.json?slug=new-york-rangers",
    4: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/philadelphia-flyers/line-combinations.json?slug=philadelphia-flyers",
    5: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/pittsburgh-penguins/line-combinations.json?slug=pittsburgh-penguins",
    6: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/boston-bruins/line-combinations.json?slug=boston-bruins",
    7: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/buffalo-sabres/line-combinations.json?slug=buffalo-sabres",
    8: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/montreal-canadiens/line-combinations.json?slug=montreal-canadiens",
    9: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/ottawa-senators/line-combinations.json?slug=ottawa-senators",
    10: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/toronto-maple-leafs/line-combinations.json?slug=toronto-maple-leafs",
    12: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/carolina-hurricanes/line-combinations.json?slug=carolina-hurricanes",
    13: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/florida-panthers/line-combinations.json?slug=florida-panthers",
    14: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/tampa-bay-lightning/line-combinations.json?slug=tampa-bay-lightning",
    15: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/washington-capitals/line-combinations.json?slug=washington-capitals",
    16: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/chicago-blackhawks/line-combinations.json?slug=chicago-blackhawks",
    17: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/detroit-red-wings/line-combinations.json?slug=detroit-red-wings",
    18: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/nashville-predators/line-combinations.json?slug=nashville-predators",
    19: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/st-louis-blues/line-combinations.json?slug=st-louis-blues",
    20: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/calgary-flames/line-combinations.json?slug=calgary-flames",
    21: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/colorado-avalanche/line-combinations.json?slug=colorado-avalanche",
    22: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/edmonton-oilers/line-combinations.json?slug=edmonton-oilers",
    23: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/vancouver-canucks/line-combinations.json?slug=vancouver-canucks",
    24: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/anaheim-ducks/line-combinations.json?slug=anaheim-ducks",
    25: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/dallas-stars/line-combinations.json?slug=dallas-stars",
    26: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/los-angeles-kings/line-combinations.json?slug=los-angeles-kings",
    28: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/san-jose-sharks/line-combinations.json?slug=san-jose-sharks",
    29: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/columbus-blue-jackets/line-combinations.json?slug=columbus-blue-jackets",
    30: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/minnesota-wild/line-combinations.json?slug=minnesota-wild",
    52: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/winnipeg-jets/line-combinations.json?slug=winnipeg-jets",
    54: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/vegas-golden-knights/line-combinations.json?slug=vegas-golden-knights",
    55: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/seattle-kraken/line-combinations.json?slug=seattle-kraken",
    59: "https://www.dailyfaceoff.com/_next/data/ZfwQeh4nYUKWDEymsct39/teams/utah-hockey-club/line-combinations.json?slug=utah-hockey-club",
}
