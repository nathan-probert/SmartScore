import csv
import datetime
import json
from typing import List, Dict
import requests
import Player
import Database
import Predictor


class API:
    
    @classmethod
    def writeToDB(cls, oldPlayers, oldDate, players, ids):
        # check if DB needs to be updated
        date = (datetime.datetime.now()).strftime('%Y-%m-%d')
        data = requests.get("https://x8ki-letl-twmt.n7.xano.io/api:Cmz3Gtkc/export").json()
        if data and data[0]['Date'] == date:
            print("\tChecking if Tim Hortons data needs to be updated...")
            teamData = requests.get(f"https://api-web.nhle.com/v1/schedule/{date}").json()
            teamsNow = {team['name'] for team in Database.getAllTeams(teamData, date)}
            teamsThen = {team['Team'] for team in data if team['Date'] == date}

            if teamsNow != teamsThen:
                print("\tTeams do not match, updating database...")
            else:
                print("\tTeams match, no need to update database...")
                return

        # new players
        dictPlayers = [player.toDict() for player in players]
        date = (datetime.datetime.now()).strftime('%Y-%m-%d')
        for player in dictPlayers:
            player["Date"] = date

            for sublist in ids:
                if int(player["playerID"]) in sublist:
                    player["onTims"] = ids.index(sublist)+1
                    break
            else:
                player["onTims"] = 0


        # old players
        oldDictPlayers = [player.toDict() for player in oldPlayers]
        i = 0
        for player in oldDictPlayers:
            player["Date"] = oldDate
            player["Scored"] = oldPlayers[i].getScored()
            i+=1

        dictPlayers.extend(oldDictPlayers)

        jsonData = json.dumps({"items": dictPlayers})
        jsonData: Dict[str, List[Dict[str, any]]] = json.loads(jsonData)

        response = requests.patch("https://x8ki-letl-twmt.n7.xano.io/api:Cmz3Gtkc/addBulk", json=jsonData)
        # Check the response
        if response.status_code == 200:
            print("\tSuccessfully updated the database. ", end="")
            print("Response:", response.json())
        else:
            print("\tRequest failed with status code:", response.status_code)
            print("\tResponse:", response.text)

    @classmethod
    def linkScoredData(cls, players, scoredIds):
        for player in players:
            if str(player.getId()) in scoredIds:
                player.setScored(1)
            else:
                player.setScored(0)

    @classmethod
    def getPlayers(cls):
        filename = "lib\\data.csv"
        with open(filename, 'r', encoding=Database.getCSVEncoding(filename)) as file:
            reader = csv.reader(file)
            header = next(reader)
            rows = list(reader)

        date = None
        data = []

        for row in reversed(rows):
            if date is None and row[0] != datetime.datetime.now().strftime('%Y-%m-%d'):
                date = row[0]
            if date is not None and row[0] == date:
                data.append(row)
        
        players = []
        for row in data:
            p = Player.Player(None, None, None, None, None, None, None, None)
            p.fromCSV(row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11], row[12], row[13])
            players.append(p)

        scoredIds = [row[3] for row in rows if row[0] == date and row[1] == "1"]
        cls.linkScoredData(players, scoredIds)

        print(f"\tNormalizing the old players")
        players = Predictor.Predictor.normalize(players, date)
        print(f"\tPredicting the old players")
        Predictor.Predictor.predictWeights(players)

        return players, date