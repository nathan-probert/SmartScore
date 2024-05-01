import Player
import ctypes

def linkStats(players, stats):
    i=0
    for player in players:
        player.setNormalizedStats(stats[i][0], stats[i][1], stats[i][2], stats[i][3], stats[i][4], stats[i][5], stats[i][6])
        i+=1
        
    return players

class Predictor:

    @classmethod
    def normalize(cls, players):
        lib = ctypes.CDLL("lib\\libPredictor.so")
        lib.normalize.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_float))
        result = lib.normalize()
        stats = []
        for i in range(len(players)):
            stats.append(result[i])
        players = linkStats(players, stats)
        
        return players
    
    @classmethod
    def predictWeights(cls, players):
        lib = ctypes.CDLL("lib\\libPredictor.so")
        lib.predictWeights.restype = ctypes.POINTER(ctypes.c_float)
        lib.predictWeights.argtypes = [ctypes.POINTER(ctypes.POINTER(ctypes.c_float)), ctypes.c_int]

        normalizedStats = []
        for player in players:
            normalizedStats.append(player.getNormalizedStats())

        # Convert normalizedStats into a list of lists of floats
        normalized_stats_as_floats = [[float(x) for x in player_stats] for player_stats in normalizedStats]

        # Convert the list of lists into a pointer to a pointer to a float
        normalized_stats_ptr = (ctypes.POINTER(ctypes.c_float) * len(normalized_stats_as_floats))(
            *[ctypes.cast((ctypes.c_float * len(player_stats))(*player_stats), ctypes.POINTER(ctypes.c_float)) 
            for player_stats in normalized_stats_as_floats]
        )

        # Call the predictWeights function with the converted argument
        probabilities = lib.predictWeights(normalized_stats_ptr, len(normalized_stats_as_floats))

        for i in range(len(players)):
            players[i].setStat(probabilities[i])

    def __init__(self, player):
        self.__player = player

    