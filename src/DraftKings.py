import requests

nhlId = "42133"
goalScorerCategory = 1190

def linkPlayerBets(players, playersInfo):
    for player in players:
        player.setBet(0)
        for playerInfo in playersInfo:
            if player.getName() == playerInfo['name']:
                player.setBet(playerInfo['bet'])
                break

    return players
        

def getOdds(subcategoryIds, numTeams):
    playersInfo = []

    id = subcategoryIds[0]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    }
    data = requests.get(f"https://sportsbook.draftkings.com//sites/CA-ON/api/v5/eventgroups/{nhlId}/categories/{goalScorerCategory}/subcategories/{id}?format=json", headers=headers, timeout=5).json()

    for offerCategory in data['eventGroup']['offerCategories']:
        if (offerCategory['offerCategoryId'] == goalScorerCategory):
            teamNum = 0

            while teamNum < numTeams:
                try:
                    subCat = offerCategory['offerSubcategoryDescriptors'][0]['offerSubcategory']['offers'][teamNum]
                except (KeyError, IndexError):
                    # no more games
                    break
                
                for outcome in subCat[0]['outcomes']:
                        
                    playersInfo.append({
                        "name": outcome['participant'],
                        "bet": outcome['oddsAmerican']
                    })
                teamNum += 1
    
    return playersInfo

def appendOdds(players, numTeams):
    url = (f"https://sportsbook.draftkings.com//sites/CA-ON/api/v5/eventgroups/{nhlId}/categories/{goalScorerCategory}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    }
    data = requests.get(url, headers=headers, timeout=5).json()
    if "eventGroup" in data:
        for i in data['eventGroup']['offerCategories']:
            if 'offerSubcategoryDescriptors' in i:
                dk_markets = i['offerSubcategoryDescriptors']
    else:
        print("Currently no Goalscorer bets on Draftkings")
        exit(1)

    subcategoryIds = []
    for i in dk_markets:
        subcategoryIds.append(i['subcategoryId'])

    playersInfo = getOdds(subcategoryIds, numTeams)
    players = linkPlayerBets(players, playersInfo)

    return players

if __name__ == "__main__":
    appendOdds([])