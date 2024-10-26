import datetime

def showMenu():
    print("Welcome to SmartScore!")
    print("1. Run the predictor")
    print("2. Run the empirical model")
    print("3. Experiment with ai model")
    print("4. Predict today's goal scorers")
    print("5. Predict today's game outcomes")
    print("6. Exit")
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
    Predictor.Predictor.normalize(players)

    print("Predicting based on weights...")
    Predictor.Predictor.predictWeights(players)

    # print("Predicting based on AI...")
    # Predictor.Predictor.predictAI(players)

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

    threshold = input("Enter the threshold (0 < threshold < 1), (-1 for all thresholds): ")
    threshold = float(threshold)
    if ((0 >= threshold or threshold >= 1) and threshold != -1):
        print("Invalid threshold, using default value 0.5.")
        threshold = 0.5

    date = datetime.datetime.now().strftime('%Y-%m-%d')

    lib = ctypes.CDLL("lib\\libPredictor.so")
    lib.empTest.argtypes = [ctypes.c_float, ctypes.POINTER(ctypes.c_char)]
    lib.empTest.restype = ctypes.c_int

    result = lib.empTest(ctypes.c_float(float(threshold)), ctypes.c_char_p(date.encode('utf-8')))

def experimentAI():
    print("Importing modules...")
    import Predictor
    
    Predictor.Predictor.predictAITesting()

def predictToday():
    print("Enter 0 for weights, 1 for AI: ", end="")
    choice = int(input())

    print("Importing modules...")
    import Database
    import Predictor

    Database.performBackfilling()

    print("Getting players...")
    players = Database.updateToday()

    print("Normalizing the data...")
    Predictor.Predictor.normalize(players)

    if choice == 0:
        print("Predicting based on weights...")
        Predictor.Predictor.predictWeights(players)
    else:
        print("Predicting based on AI...")
        Predictor.Predictor.predictAI(players)

    print("Sorting players by stat...")
    players.sort(key=lambda x: x.getStat(), reverse=True)

    for player in players:
        print(player)

def predictGame():
    import GamePredictor
    
    GamePredictor.predictGame()

if __name__ == "__main__":
    choice = showMenu()
    print()

    if choice == 1:
        runPredictor()
    elif choice == 2:
        runEmpirical()
    elif choice == 3:
        experimentAI()
    elif choice == 4:
        predictToday()
    elif choice == 5:
        predictGame()
    else:
        exit()
