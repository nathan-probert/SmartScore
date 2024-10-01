Create aws step function with following steps:
 - Get all players
   - potentially use a dedicated lambda for getting players from a team so that can be done in parallel
 - Make predictions
 - Save in database (use Xano over S3 for pricing)