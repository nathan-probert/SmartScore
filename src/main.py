import Database
import Predictor
import API
import Tims

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
    
    print("Updating previous scorers for API...")
    oldPlayers, oldDate = API.API.getPlayers()

    print("Getting players available on Tim Hortons Hockey Challenge Picker...")
    ids = Tims.getPlayers()

    print("Generating API endpoint data...")
    API.API.writeToDB(oldPlayers, oldDate, players, ids)