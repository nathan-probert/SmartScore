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
```sh build_scripts/deploy.sh```<br/>
*If you are on windows, ensure Docker is running with the image: "public.ecr.aws/amazonlinux/amazonlinux:2".*<br/>
*Also ensure you have adjusted the path in build_scripts/compile.sh to match your local path.*
<br/><br/>

*Note: This program is intended for informational purposes only and does not facilitate actual betting. Users should exercise their own judgment and discretion when using the provided suggestions for betting purposes.*

<br/>

# ALL CONSIDERED STATISTICS  

- **Goals Per Game (GPG)**: The most telling stat when determining if a player will score a goal. It implicitly considers factors like time on ice, shots per game, shot-to-goal ratio, etc.  
- **Goals Per Game in Last 5 Games (5GPG)**: Captures hot streaks (and cold streaks).  
- **Historic Goals Per Game (HGPG)**: A player's GPG over the last three seasons. Especially useful at the beginning of a new season when current GPG may be skewed.  
- **Team's Goals Per Game (TGPG)**: Useful if a player is traded to a new team.  
- **Other Team's Goals Against (OTGA)**: Captures the opposing team's defensive strength, factoring in defense quality, goalie performance, etc. This is independent of a player's GPG, which reflects their scoring average, whereas OTGA varies with each game.  
- **Home or Away**: Helps identify patterns in a player's goal-scoring performance based on location.  
- **Historic Power Play Goals (HPPG)**: A player's power play goals over the last three seasons (since these are relatively rare, one season alone might be misleading). Can be combined with **Other Team's Shorthanded Goals Against Per Game** for a more comprehensive stat.  
- **Other Team's Shorthanded Goals Against Per Game (OTSHGA)**: Measures how many shorthanded goals a team allows per game. Can be combined with **HPPG** for a more meaningful composite stat. Also implicitly considers other team's penalty minutes, penalty kill percentage, etc.  

### **Stats Already Covered by Others:**  
- **Time on Ice** → Covered by GPG.  
- **Shots Per Game** → Covered by GPG.  
- **Shot Percentage** → Covered by GPG.  
- **Other Team's Penalty Minutes** → Covered by OTSHGA.  

