# Vodafone ADSL usage to Telegram

Python script that:
- Logs in to `web.vodafone.com.eg`
- Opens ADSL management
- Reads ADSL usage from Vodafone internal API response
- Sends usage summary to your Telegram bot

## 1) Install

```bash
pip install -r requirements.txt
python -m playwright install
```

If browser download is slow, the script defaults to Edge channel:
- `PLAYWRIGHT_BROWSER_CHANNEL=msedge`

## 2) Configure

Copy `.env.example` to `.env` and fill your real values:
- `VODA_MOBILE`
- `VODA_PASSWORD`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## 3) Run

```bash
python vodafone_adsl_to_telegram.py
```

On success, it sends usage to Telegram.

## Notes

- If Vodafone asks for extra verification (OTP/captcha), automated login may fail.
- If login or parsing fails, script sends error message to Telegram when `SEND_ERROR_TO_TELEGRAM=true`.
