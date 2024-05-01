import Database
import Predictor
import API

if __name__ == "__main__":
    print("Filling in past dates...")
    Database.performBackfilling()

    print("Updating today's games...")
    players = Database.updateToday()

    print("Normalizing the data...")
    players = Predictor.Predictor.normalize(players)

    print("Predicting based on weights...")
    Predictor.Predictor.predictWeights(players)

    print("Sorting players by stat...")
    players.sort(key=lambda x: x.getStat(), reverse=True)

    print("Updating previous scorers in API...")
    oldPlayers, oldDate = API.API.getPlayers()

    print("Generating API endpoint data...")
    API.API.writeToDB(oldPlayers, oldDate, players)