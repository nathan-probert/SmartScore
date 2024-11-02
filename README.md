# SmartScore

## About this Program
You can find more information about this program on the [website](https://nathanprobert.ca/smartscore/help)!

## Calculating the probability
The current method for calculating the probability takes into account a variety of individual statistics:
 - Player's goals per game (GPG)
 - Player's goals per game in the last 5 games (5GPG)
 - Player's goals per game over the last 3 NHL seasons (HGPG)
 - Team's goals per game (TGPG)
 - Other team's goals against per game (OTGA)
 - Home or away (Home)
 - Player's power play goals per game over the last 3 NHL seasons (HPPG)
 - Other team's short handed goals per game (OTSHGA)

## Running this Program

First install all necessary packages:<br/>
```make local-setup```<br/>

Deploy to AWS using:<br />
```sh build_scripts/deploy.sh```<br/><br/>

*Note: This program is intended for informational purposes only and does not facilitate actual betting. Users should exercise their own judgment and discretion when using the provided suggestions for betting purposes.*


