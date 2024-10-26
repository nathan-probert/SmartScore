def negOdd(odd):
    odd = abs(odd)
    return odd/(odd+100)*100

def posOdd(odd):
    return 100/(odd+100)*100

def oddsToProb(odds):
    if odds < 0:
        return negOdd(odds)
    return posOdd(odds)

odd = oddsToProb(int(input("Enter the odds: ")))
print(f"{round(odd, 2)}%")