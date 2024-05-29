import os
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

import datetime
from keras.layers import Input, Dense, Dropout
from keras.models import Model
import ctypes
import numpy as np
import tensorflow as tf


def linkStats(players, stats):
    i=0
    for player in players:
        player.setNormalizedStats(stats[i][0], stats[i][1], stats[i][2], stats[i][3], stats[i][4], stats[i][5], stats[i][6])
        i+=1
        
    return players

def createModel():

    class Data(ctypes.Structure):
        _fields_ = [
            ("stats", ctypes.POINTER(ctypes.POINTER(ctypes.c_float))),
            ("dates", ctypes.POINTER(ctypes.c_char_p)),
            ("numRows", ctypes.c_int),
            ("numRowsToNorm", ctypes.c_int)
        ]
        
    lib = ctypes.CDLL("lib\\libPredictor.so")
    lib.getNormalizedData.argtypes = [ctypes.POINTER(ctypes.c_char)]
    lib.getNormalizedData.restype = Data
    rawData = lib.getNormalizedData(ctypes.c_char_p(datetime.datetime.now().strftime('%Y-%m-%d').encode('utf-8')))

    xTrain = []
    yTrain = []
    for i in range(rawData.numRows):
        xTrain.append(rawData.stats[i][1:9])
        yTrain.append(rawData.stats[i][0])

    xTrain = np.array(xTrain)
    yTrain = np.array(yTrain)

    # Define the input shape
    input_shape = (xTrain.shape[1],)
    inputs = Input(shape=input_shape)

    x = Dense(64, activation='relu')(inputs)
    x = Dropout(0.5)(x)  # Add dropout layer to prevent overfitting
    x = Dense(64, activation='relu')(x)
    outputs = Dense(1, activation='sigmoid')(x)

    model = Model(inputs=inputs, outputs=outputs)
    
    # Use a different optimizer (e.g., RMSprop) and a different learning rate
    optimizer = tf.keras.optimizers.RMSprop(learning_rate=0.001)
    
    # Use a different loss function (e.g., mean squared error)
    # model.compile(loss='mse', optimizer=optimizer)
    model.compile(loss='binary_crossentropy', optimizer="adam", metrics=['accuracy'])

    # Increase the number of epochs and batch size
    model.fit(xTrain, yTrain, epochs=100, batch_size=128, verbose=0)

    print(f"\t\tModel has a loss of {model.evaluate(xTrain, yTrain, verbose=0)[0]:.2f} and an accuracy of {model.evaluate(xTrain, yTrain, verbose=0)[1]*100:.2f}%")
    
    return model

def evalModel(model, threshold=0.40):
    class Data(ctypes.Structure):
        _fields_ = [
            ("stats", ctypes.POINTER(ctypes.POINTER(ctypes.c_float))),
            ("dates", ctypes.POINTER(ctypes.c_char_p)),
            ("numRows", ctypes.c_int),
            ("numRowsToNorm", ctypes.c_int)
        ]
        
    lib = ctypes.CDLL("lib\\libPredictor.so")
    lib.getNormalizedData.argtypes = [ctypes.POINTER(ctypes.c_char)]
    lib.getNormalizedData.restype = Data
    rawData = lib.getNormalizedData(ctypes.c_char_p(datetime.datetime.now().strftime('%Y-%m-%d').encode('utf-8')))

    xTrain = []
    yTrain = []
    for i in range(rawData.numRows):
        xTrain.append(rawData.stats[i][1:9])
        yTrain.append(rawData.stats[i][0])

    xTrain = np.array(xTrain)
    yTrain = np.array(yTrain)

    predictions = model.predict(xTrain, verbose=0)

    correct = 0
    guesses = 0
    for i in range(len(predictions)):
        if predictions[i] >= threshold:
            guesses += 1
            if yTrain[i] == 1:
                correct += 1

    print(f"{correct} / {guesses} for {correct/guesses*100:.2f}% accuracy with threshold {threshold}")


class Predictor:

    @classmethod
    def normalize(cls, players, date=datetime.datetime.now().strftime('%Y-%m-%d')):
        lib = ctypes.CDLL("lib\\libPredictor.so")
        lib.normalize.argtypes = [ctypes.POINTER(ctypes.c_char)]
        lib.normalize.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_float))

        result = lib.normalize(ctypes.c_char_p(date.encode('utf-8')))
        stats = []
        for i in range(len(players)):
            stats.append(result[i])
        players = linkStats(players, stats)
        
        return
    
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
            players[i].setStat(float(probabilities[i]))

    @classmethod
    def predictAI(cls, players):
        print("\tCreating the model...")
        model = createModel()

        print("\tNormalizing the players...")
        normalizedStats = [player.getNormalizedStats() for player in players]
        normalizedStats = np.array(normalizedStats)

        print("\tPredicting the players...")
        predictions = model.predict(normalizedStats, verbose=0)

        for i, player in enumerate(players):
            player.setStat(float(predictions[i]))

    @classmethod
    def predictAITesting(cls):
        print("\tCreating the model...")
        model = createModel()

        print("\tEvaluating the model...")
        evalModel(model, 0.35)

    def __init__(self, player):
        self.__player = player

    