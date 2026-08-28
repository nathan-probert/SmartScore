import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from email_utility import get_html_and_text

if __name__ == "__main__":
    # Example picks data
    sample_picks = [
        {
            "id": 8475169,
            "name": "Evander Kane",
            "date": "2026-01-29",
            "gpg": 0.173076923076923,
            "hgpg": 0.252941176470588,
            "five_gpg": 0.4,
            "stat": 0.184747323393822,
            "tims": "3",
            "team_name": "Vancouver",
            "tgpg": 2.5849,
            "otga": 3.54716,
            "injury_status": "HEALTHY",
            "injury_desc": "",
            "Scored": None,
        },
        {
            "id": 8475786,
            "name": "Zach Hyman",
            "date": "2026-01-29",
            "gpg": 0.6,
            "hgpg": 0.539473684210526,
            "five_gpg": 0.8,
            "stat": 0.353383749723434,
            "tims": "1",
            "team_name": "Edmonton",
            "tgpg": 3.44444,
            "otga": 3.43137,
            "injury_status": "HEALTHY",
            "injury_desc": "",
            "Scored": None,
        },
        {
            "id": 8476927,
            "name": "Teddy Blueger",
            "date": "2026-01-29",
            "gpg": 0.5,
            "hgpg": 0.100591715976331,
            "five_gpg": 0.6,
            "stat": 0.228578120470047,
            "tims": "2",
            "team_name": "Vancouver",
            "tgpg": 2.5849,
            "otga": 3.54716,
            "injury_status": "HEALTHY",
            "injury_desc": "",
            "Scored": None,
        },
    ]
    html, text = get_html_and_text(sample_picks)
    # Save HTML to file for preview
    with open("preview_email.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Preview email HTML saved to preview_email.html")
