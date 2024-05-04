import datetime
import requests

class Player:

    teamStats = {}

    # make one constructor (prob bottom one, have the top one be a different method and call the bottom one)

    def __init__(self, name, id, teamName, teamAbbr, teamId, otherTeamId, isHomeTeam, teamData):
        if name is None:
            return # this is a dummy player that should be filled by the other constructor
        self.__name = name
        self.__id = id
        self.__teamName = teamName
        self.__teamAbbr = teamAbbr
        self.__teamId = teamId
        self.__otherTeamId = otherTeamId
        self.__isHomeTeam = isHomeTeam

        self.__teamData = teamData
        self.__playerData = requests.get(f"https://api-web.nhle.com/v1/player/{id}/landing").json()
        self.populateStats()

    def fromCSV(self, name, id, teamName, bet, GPG, last5GPG, HGPG, PPG, OTPM, TGPG, OTGA, isHomeTeam):
        self.__name = str(name)
        self.__id = int(id)
        self.__teamName = str(teamName)
        self.__bet = str(bet)
        self.__GPG = float(GPG)
        self.__5GPG = float(last5GPG)
        self.__HGPG = float(HGPG)
        self.__PPG = int(round(float(PPG)))
        self.__OTPM = int(OTPM)
        self.__TGPG = float(TGPG)
        self.__OTGA = float(OTGA)
        self.__isHomeTeam = int(isHomeTeam)

    def populateStats(self):
        self.setGPG()
        self.set5GPG()
        self.setHGPGandHPPG()
        self.setTeamStats()

    @classmethod
    def initTeamStats(cls):
        data = requests.get(f"https://api.nhle.com/stats/rest/en/team/summary?cayenneExp=seasonId=20232024%20and%20gameTypeId=2").json()
        data2 = requests.get(f"https://api.nhle.com/stats/rest/en/team/penaltykilltime?cayenneExp=seasonId=20232024%20and%20gameTypeId=2").json()

        for team in data["data"]:
            cls.teamStats[team['teamId']] = {
                                        "gpg" : team["goalsForPerGame"],
                                        "ga"  : team['goalsAgainstPerGame']
                                    }

        for team in data2["data"]:
            cls.teamStats[team['teamId']].update({
                                        "pm" : team["timeOnIceShorthanded"]
                                    })

    def setGPG(self):
        for player in self.__teamData["skaters"]:
            if self.__id == player['playerId']:
                if player['gamesPlayed'] > 0:
                    self.__GPG = player['goals'] / player['gamesPlayed']
                    return
                else:
                    self.__GPG = 0.0
                    return
        self.__GPG = 0.0
        return
    
    def set5GPG(self):
        goals = 0
        try:
            for gameData in self.__playerData['last5Games']:
                goals += gameData['goals']
        except KeyError:
            self.__5GPG = 0.0
            return
        
        self.__5GPG = goals / 5
        return
    
    def getSeasons(self):
        curYear = datetime.datetime.now().year
        year1 = str(curYear-1) + str(curYear)
        year2 = str(curYear-2) + str(curYear-1)
        year3 = str(curYear-3) + str(curYear-2)
        return [int(year1), int(year2), int(year3)]

    def setHGPGandHPPG(self):
        goals = 0
        games = 0
        acceptableSeasons = self.getSeasons()
        self.__PPG = 0.0

        for seasonData in self.__playerData['seasonTotals']:
            # ignore other leagues
            if ((seasonData['season'] in acceptableSeasons) and (seasonData['leagueAbbrev'] == "NHL")):

                # runs twice (regular season and playoffs)
                if seasonData['season'] == acceptableSeasons[0]:
                    self.__PPG += seasonData['powerPlayGoals']

                goals += seasonData['goals']
                games += seasonData['gamesPlayed']

        if games == 0:
            self.__HGPG = 0.0
            return
        
        self.__HGPG = goals / games
        return
    
    def setTeamStats(self):
        self.__TGPG = Player.teamStats[self.__teamId]['gpg']        
        self.__OTGA = Player.teamStats[self.__otherTeamId]['ga']
        self.__OTPM = Player.teamStats[self.__otherTeamId]['pm']

    def toCSV(self):
        csv_format = "{},{},{},{},{},{},{},{},{},{},{},{},{}".format(
            " ",
            "{:s}".format(self.__name),
            "{:d}".format(self.__id),
            "{:s}".format(self.__teamName),
            "{:s}".format(str(self.__bet)),
            "{:f}".format(self.__GPG),
            "{:f}".format(self.__5GPG),
            "{:f}".format(self.__HGPG),
            "{:f}".format(self.__PPG),
            "{:d}".format(self.__OTPM),
            "{:f}".format(self.__TGPG),
            "{:f}".format(self.__OTGA),
            "{:d}".format(self.__isHomeTeam)
        )
        return csv_format

    @classmethod
    def printHeader ( cls ):
        name_padding = 23
        team_padding = 15
        stat_padding = 10

        print("\t{:<{}} {:<{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}}".format(
            "Player Name", name_padding,
            "Team Name", team_padding,
            "Bet", stat_padding,
            "Stat", stat_padding,
            "GPG", stat_padding,
            "5GPG", stat_padding,
            "HGPG", stat_padding,
            "HPPG", stat_padding,
            "OTPM", stat_padding,
            "TGPG", stat_padding,
            "OTGA", stat_padding,
            "isHome", stat_padding
        ))
        print("")         

    def __str__ (self):
        name_padding = 30
        team_padding = 15
        stat_padding = 10

        return "{:<{}} {:<{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}}".format(
            self.__name, name_padding, 
            self.__teamName, team_padding, 
            "{:s}".format(str(self.__bet)), stat_padding, 
            "{:.2f}".format(float(self.getStat())), stat_padding, 
            "{:.2f}".format(self.__GPG), stat_padding, 
            "{:.2f}".format(self.__5GPG), stat_padding, 
            "{:.2f}".format(self.__HGPG), stat_padding, 
            "{:.2f}".format(self.__PPG), stat_padding, 
            "{:d}".format(self.__OTPM), stat_padding,
            "{:.2f}".format(self.__TGPG), stat_padding, 
            "{:.2f}".format(self.__OTGA), stat_padding, 
            "{:d}".format(self.__isHomeTeam), stat_padding
        )
    
    def toDict(self):
        return {
            "Name": self.__name,
            "Team": self.__teamName,
            "playerID": self.__id,
            "Bet": self.__bet,
            "Stat": self.getStat(),
            "GPG": self.__GPG,
            "Last_5_GPG": self.__5GPG,
            "HGPG": self.__HGPG,
            "PPG": self.__PPG,
            "OTPM": self.__OTPM,
            "TGPG": self.__TGPG,
            "OTGA": self.__OTGA,
            "Home_1": self.__isHomeTeam
        }
    
    def getName(self):
        return self.__name
    
    def getId(self):
        return self.__id

    def setBet(self, bet):
        self.__bet = bet

    def setNormalizedStats(self, GPG, last5GPG, HGPG, PPG, OTPM, TGPG, OTGA):
        self.__NGPG = GPG
        self.__N5GPG = last5GPG
        self.__NHGPG = HGPG
        self.__NPPG = PPG
        self.__NOTPM = OTPM
        self.__NTGPG = TGPG
        self.__NOTGA = OTGA
        self.__NHOME = self.__isHomeTeam

    def getNormalizedStats(self):
        return [self.__NGPG, self.__N5GPG, self.__NHGPG, self.__NPPG, self.__NOTPM, self.__NTGPG, self.__NOTGA, self.__NHOME]

    def setStat(self, stat):
        self.__stat = stat

    def getStat(self):
        return self.__stat
        if hasattr(self, '__stat'):
            return self.__stat
        else:
            return ""
    
    def setTims(self, onTims):
        self.__tims = onTims
    
    def getTims(self):
        return self.__tims

    def setScored(self, scored):
        self.__scored = scored
    
    def getScored(self):
        if hasattr(self, '__scored'):
            return self.__scored
        else:
            return ""