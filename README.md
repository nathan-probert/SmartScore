# SmartScore
![smaller logo](https://github.com/proby-8/tims-picker/assets/109328434/bb96bcb9-db9e-4241-93f2-d30a3224fc4d)

## About this Program

This program offers a set of functions that provide a reliable method for selecting winning bets for DraftKings NHL Goalscorers. The program includes four main functions, described below.

## Function 1: Create Rankings Using a Predefined Formula

This function uses a formula created through extensive empirical testing. It pulls data from the NHL API to create probabilities for each possible player (players playing today), and then outputs the players in order of most likely to score. Additionally, this program will utilize a threshold to determine which players should be bet on, and any players matching this criteria are outputted as well.

## Function 2: Make a Guess Using an AI Formulated Calculation

This function utilizes Python's TensorFlow library to employ a machine learning method for making picks. It analyzes years of data for each player every day, comparing the statistics of those who scored with those who didn't. (A new database with more statistics is currently being created). The program creates a linear estimator using this data, then runs the possible players of the day through this estimator. This process yields a scoring probability for each player, and the program orders the players with the highest probabilities as the best possible picks. These names are then outputed for the user's convenience.

## Function 3: Save Today's Data

This function will update the database with new statistics, and will update yesterday's player's with information on which player's scored (and which didn't). This project is meant to be ran every day, I do this with a Python Cron job, and will periodically updated the data file here.

## Function 4: Empirical Testing

This function generates the best possible weights for each statistic, something that will become increasingly less necessary as the data file is filled (as the weights should begin to settle). This function can also be used to determine a threshold of which player's should be bet on, and which should not. 

## Running this Program

First install all packages within requirements.txt:<br/>
```pip install -r requirements.txt```<br/><br/>
Start the main script:<br/>
```python src/main.py```

## Images
AI Output (small dataset, still WIP):        |  Linear Estimator
:-------------------------:|:-------------------------:
![image](https://github.com/proby-8/tims-picker/assets/109328434/551c9924-d980-4534-bdbe-1055f29501e9)  |  ![image](https://github.com/proby-8/tims-picker/assets/109328434/92117d39-6450-40ee-be60-8d12da7bee04)

---
*Note: This program is intended for informational purposes only and does not facilitate actual betting. Users should exercise their own judgment and discretion when using the provided suggestions for betting purposes.*

*To-Do: Add penalty kill pct*


```commandline
sh build_scripts/deploy.sh
```
