import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List

from aws_lambda_powertools import Logger

from config import GMAIL_APP_PASSWORD, GMAIL_EMAIL


def get_html_and_text(picks: List[Dict]) -> (str, str):
    logo_url = "https://raw.githubusercontent.com/nathan-probert/portfolio-site/refs/heads/main/public/images/logo.jpg"
    background = "#141720"
    card_bg = "#212531"
    primary = "#C71E76"
    foreground = "#FFFFFF"
    muted = "#b0b0b8"
    grey = "#2e2f3a"

    # Sort all picks by tims (as int)
    def tims_key(p):
        try:
            return int(p.get("tims", 0))
        except (ValueError, TypeError):
            return 0

    sorted_picks = sorted(picks, key=tims_key)

    # Plain text
    text_body = "Tims | Name | Team | Probability\n"
    text_body += "-" * 40 + "\n"
    for pick in sorted_picks:
        stat = pick.get("stat", "")
        try:
            stat_str = f"{float(stat):.2f}%"
        except (ValueError, TypeError):
            stat_str = str(stat)
        text_body += f"{pick.get('tims', '')} | {pick.get('name', '')} | {pick.get('team_name', '')} | {stat_str}\n"

    # HTML
    html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: {foreground}; background-color: {background}; \
    background: {background}; margin:0; padding:0;">
    <div style="max-width:480px;margin:40px auto;background:{card_bg};border-radius:14px;\
      box-shadow:0 2px 16px #0006;padding:32px;">
      <div style="text-align:center;">
        <img src="{logo_url}" alt="SmartScore logo" style="max-width:120px;margin-bottom:18px;\
          filter:drop-shadow(0 2px 8px #0008);" \
          onerror="this.style.display='none';this.insertAdjacentHTML('afterend', '<div style=\'font-size:1.2em;color:{primary};margin-bottom:18px;\'>SmartScore logo</div>');">
      </div>
      <p style="font-size:1.1em;text-align:center;color:{foreground};">Here are your picks for today:</p>
      <table style="width:100%;border-collapse:collapse;margin:18px 0 18px 0;background:{card_bg};">
        <thead>
          <tr>
            <th style="border-bottom:2px solid {primary};padding:10px 0;text-align:left;color:{primary};\
              font-size:1.1em;">Tims</th>
            <th style="border-bottom:2px solid {primary};padding:10px 0;text-align:left;color:{primary};\
              font-size:1.1em;">Player</th>
            <th style="border-bottom:2px solid {primary};padding:10px 0;text-align:left;color:{primary};\
              font-size:1.1em;">Team</th>
            <th style="border-bottom:2px solid {primary};padding:10px 0;text-align:left;color:{primary};\
              font-size:1.1em;">Probability</th>
          </tr>
        </thead>
        <tbody>
"""
    for pick in sorted_picks:
        stat = pick.get("stat", "")
        try:
            stat_str = f"{float(stat):.2f}%"
        except (ValueError, TypeError):
            stat_str = f"{stat}%" if stat != "" else ""
        html_body += f"""
          <tr>
            <td style=\"padding:10px 0;border-bottom:1px solid {grey};color:{foreground};\">{pick.get('tims', '')}</td>
            <td style=\"padding:10px 0;border-bottom:1px solid {grey};color:{foreground};\">{pick.get('name', '')}</td>
            <td style=\"padding:10px 0;border-bottom:1px solid {grey};color:{foreground};\">{pick.get('team_name', '')}</td>
            <td style=\"padding:10px 0;border-bottom:1px solid {grey};color:{primary};font-weight:bold;\">{stat_str}</td>
          </tr>
"""
    html_body += f"""
        </tbody>
      </table>
      <div style="text-align:center;margin:32px 0 0 0;">
        <a href="https://smartscore.nathanprobert.ca/"
           style="background:{primary};color:#fff;text-decoration:none;padding:12px 28px;border-radius:6px;\
           font-weight:bold;display:inline-block;letter-spacing:0.5px;">
           View More Details</a>
      </div>
      <hr style="margin:32px 0 16px 0;border:none;border-top:1px solid {grey};">
      <p style="font-size:0.95em;color:{muted};text-align:center;">
        This is an automated message from SmartScore.<br>
        <a href="https://smartscore.nathanprobert.ca/settings" style="color:{primary};">
          Change your notification preferences</a>
      </p>
    </div>
  </body>
</html>
"""
    return html_body, text_body


def send_email(email: str, picks: List[Dict]) -> None:
    logger = Logger()
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)

        msg = MIMEMultipart("alternative")
        msg["From"] = GMAIL_EMAIL
        msg["To"] = email
        msg["Subject"] = "Your Picks Update"

        # Use the shared template function
        html_body, text_body = get_html_and_text(picks)

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        server.send_message(msg)
        logger.info(f"Email sent to {email}")

        server.quit()
    except (smtplib.SMTPException, ConnectionError) as e:
        logger.error(f"Failed to send email to {email}: {e}")
