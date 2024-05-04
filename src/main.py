import datetime


def showMenu():
    print("Welcome to SmartScore!")
    print("1. Run the predictor")
    print("2. Run the empirical model")
    print("3. Exit")
    choice = int(input("Enter your choice: "))
    return choice

def runPredictor():

    print("Importing modules...")
    import Database
    import Predictor
    import API
    import Tims
    
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

def runEmpirical():
    import ctypes

    threshold = input("Enter the threshold (-1 for all thresholds): ")

    lib = ctypes.CDLL("lib\\libPredictor.so")
    lib.empTest.argtypes = [ctypes.c_float]
    lib.empTest.restype = ctypes.c_int

    result = lib.empTest(ctypes.c_float(float(threshold)))

if __name__ == "__main__":
    choice = showMenu()
    print()

    if choice == 1:
        runPredictor()
    elif choice == 2:
        runEmpirical()
    else:
        exit()
