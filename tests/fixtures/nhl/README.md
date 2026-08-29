Frozen NHL API fixtures for mock mode
=====================================

When the ``mock-nhl-api`` feature flag is enabled, deployed Lambdas use
:class:`smartscore.mock_nhl_client.MockNHLClient` instead of hitting the live
NHL API. That client serves the JSON payloads in this directory, mirroring
exactly what the live NHL endpoints would return for a real game day.

The files here are placeholders (dummy data) so the mock can be exercised
before real data is recorded. They are staged into the dev Lambda zip (only
``dev``) by ``build_scripts/deploy.sh`` under ``fixtures/nhl``; prod stays
clean.

Layout (relative to this directory):

- ``schedule/{date}.json``        -> GET /v1/schedule/{date}
- ``score/{date}.json``           -> GET /v1/score/{date}
- ``roster/{team}.json``          -> GET /v1/roster/{team}/current
- ``player/{player_id}.json``     -> GET /v1/player/{player_id}/landing
- ``team/summary_{season}.json``  -> GET /stats/rest/en/team/summary (season)
- ``team/penalty_kill_{season}.json`` -> GET /stats/rest/en/team/penaltykilltime

Record a real game day with:

    poetry run python -m smartscore.scripts.record_nhl_fixtures 2025-06-11

Always record from a real day games were played so the data is realistic.
