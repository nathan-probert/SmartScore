import requests

def getPlayers():
    response = requests.get("https://api.hockeychallengehelper.com/api/picks?").json()
    allPlayers = response['playerLists']

    groupNum = 1
    ids = []
    while groupNum <= 3:
        ids.append([player['nhlPlayerId'] for player in allPlayers[groupNum-1]['players']])
        groupNum += 1

    return ids

if __name__ == "__main__":
    ids = getPlayers()
