import datetime
import requests
import csv
import concurrent.futures
import Player
import DraftKings

def getCSVEncoding(filename):
    """ Returns the csv encoding of the file.

    This function tries to open a file with different encodings and returns the encoding that works.
    It should be latin1, but sometimes it switches to utf-8.
    If none of the encodings work, it raises an exception.
    """
    encodings = ['latin1', 'utf-8']
    
    for encoding in encodings:
        try:
            with open(filename, 'r', encoding=encoding) as file:
                _ = file.read()
            return encoding
        except UnicodeDecodeError:
            continue
    
    raise Exception("Could not determine the encoding of the file. Please manually save the csv file and try again.")

def getPlayers(gameData):
    """ Returns a list of players who played in the game.

    This function extracts the player information from the game data and returns a list of players.
    """
    players = []
    
    # Extract players from both away and home teams
    for team in ['awayTeam', 'homeTeam']:
        for playerType in ['forwards', 'defense']:
            for player in gameData[team][playerType]:
                player_info = {
                    'playerId': player['playerId'],
                    'goals': player['goals']
                }
                players.append(player_info)
    
    return players

def findPlayer(id, players):
    """ Returns the player with the given id.
    
    This function searches for the player with the given id in the list of players.
    If the player is found, it returns the player information. Otherwise, it returns None.
    """
    for player in players:
        if str(player['playerId']) == str(id):
            return player
    return None

def updatePrevGoals(filename, date, players):
    # get all rows of data
    with open(filename, 'r', encoding=getCSVEncoding(filename)) as file:
        reader = csv.reader(file)
        header = next(reader)
        rows = list(reader)

    # go through line-by-line, updating 'scored' column and removing player's who didn't play to maintain data integrity
    with open(filename, 'w', encoding=getCSVEncoding(filename), newline='') as file:
        writer = csv.writer(file)
        writer.writerow(header)

        for row in rows:
            if row[0] == date:
                player = findPlayer(row[3], players)

                # skip if player didn't play or already updated
                if player and row[1] not in {0,1}:
                    row[1] = 0
                    if player['goals'] > 0:
                        row[1] = 1
                    writer.writerow(row)
            else:
                writer.writerow(row)

def performBackfilling():
    """ Updates the goal scorers in the database for the previous day's games.
    
    This function gets the previous day's date and retrieves the game data for that date.
    It then updates the goal scorers in the database for the players who played in the games.
    """

    # get all dates with no scored column
    filename = "lib\\data.csv"
    with open(filename, 'r', encoding=getCSVEncoding(filename)) as file:
        reader = csv.reader(file)
        header = next(reader)
        rows = list(reader)

    datesToFix = {row[0] for row in rows if row[1].strip() not in {"0", "1"}}
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    if today in datesToFix:
        datesToFix.remove(today)

    for date in list(datesToFix):
        # get games
        data = requests.get(f"https://api-web.nhle.com/v1/score/{date}").json()

        # get players who actually played
        players = []
        for game in data['games']:
            gameData = requests.get(f"https://api-web.nhle.com/v1/gamecenter/{game['id']}/boxscore").json()
            players += getPlayers(gameData['playerByGameStats'])

        # update goal scorers in database
        filename = "lib\\data.csv"
        updatePrevGoals(filename, date, players)

def getAllTeams(data, date, onlyCurTeams=True):
    teams = []
    for day in data['gameWeek']:
        if day['date'] == date:
            for game in day['games']:
                
                # ensure game hasn't started (otherwise could skew data)
                if (game['gameState'] == 'FUT'):
                    teamInfoHome = {
                        'name': (game["homeTeam"]["placeName"]["default"]),
                        'abbr': (game["homeTeam"]["abbrev"]),
                        'id': (game['homeTeam']['id']),
                        'otherId': (game['awayTeam']['id']),
                        'home': 1
                    }
                    teamInfoAway = {
                        'name': (game["awayTeam"]["placeName"]["default"]),
                        'abbr': (game["awayTeam"]["abbrev"]),
                        'id': (game['awayTeam']['id']),
                        'otherId': (game['homeTeam']['id']),
                        'home': 0
                    }

                    teams.append(teamInfoHome)
                    teams.append(teamInfoAway)
                elif not onlyCurTeams:
                    if (game['gameState'] == 'FUT'):
                        teamInfoHome = {
                            'name': (game["homeTeam"]["placeName"]["default"]),
                            'abbr': (game["homeTeam"]["abbrev"]),
                            'id': (game['homeTeam']['id']),
                            'otherId': (game['awayTeam']['id']),
                            'home': 1
                        }
                        teamInfoAway = {
                            'name': (game["awayTeam"]["placeName"]["default"]),
                            'abbr': (game["awayTeam"]["abbrev"]),
                            'id': (game['awayTeam']['id']),
                            'otherId': (game['homeTeam']['id']),
                            'home': 0
                        }

                        teams.append(teamInfoHome)
                        teams.append(teamInfoAway)
    
    return teams

def getPlayersFromTeam(team):
    print(f"\tGetting players from {team['name']}...")
    teamData = requests.get(f"https://api-web.nhle.com/v1/club-stats/{team['abbr']}/20232024/2").json()

    players = []
    data = requests.get(f"https://api-web.nhle.com/v1/roster/{team['abbr']}/20232024").json()
    types = ['forwards', 'defensemen']
    for playerType in types:
        for player in data[playerType]:
            name = player['firstName']['default'] + " " + player['lastName']['default']
            id = player['id']
            
            players.append(Player.Player(name, id, team['name'], team['abbr'], team['id'], team['otherId'], team['home'], teamData))

    return players

def getAllPlayers(date):
    data = requests.get(f"https://api-web.nhle.com/v1/schedule/{date}").json()
    
    # get all teams playing today
    teams = getAllTeams(data, date)
    Player.Player.initTeamStats()
    
    # use threading to get each team's roster
    players = []
    with concurrent.futures.ThreadPoolExecutor(max_workers = 8) as executor:
        results = [executor.submit(getPlayersFromTeam, team) for team in teams]
        for result in concurrent.futures.as_completed(results):
            players += result.result()

    return players, len(teams)

def writeCSV(players, date):
    filename = "lib\\data.csv"
    with open(filename, 'a', encoding=getCSVEncoding(filename), newline='') as file:
        for player in players:
            file.write(f"{date},{player.toCSV()}\n")


def updateToday():
    date = (datetime.datetime.now()).strftime('%Y-%m-%d')

    # check if date is already filled
    filename = "lib\\data.csv"
    with open (filename, 'r', encoding=getCSVEncoding(filename)) as file:
        reader = csv.reader(file)
        header = next(reader)
        rows = list(reader)
        if date == rows[-1][0]:
            players = []
            for row in rows:
                if date == row[0]:
                    p = Player.Player(None, None, None, None, None, None, None, None)
                    p.fromCSV(row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11], row[12], row[13])
                    players.append(p)
            return players
                    
    players, numTeams = getAllPlayers(date)
    players = DraftKings.appendOdds(players, numTeams)

    # write to csv
    writeCSV(players, date)

    return players