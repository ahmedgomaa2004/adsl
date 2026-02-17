# Vodafone ADSL Usage to Telegram

Python script that:
- Logs in to `web.vodafone.com.eg`
- Opens ADSL management
- Reads ADSL usage from Vodafone internal API response
- Sends usage summary to your Telegram bot

## Local Run

### 1) Install

```bash
pip install -r requirements.txt
python -m playwright install
```

If browser download is slow locally, script defaults to Edge channel:
- `PLAYWRIGHT_BROWSER_CHANNEL=msedge`

### 2) Configure

Copy `.env.example` to `.env` and fill:
- `VODA_MOBILE`
- `VODA_PASSWORD`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 3) Run

```bash
python vodafone_adsl_to_telegram.py
```

## Run Automatically on GitHub (Schedule)

Workflow file:
- `.github/workflows/adsl-scheduler.yml`

Default schedule:
- Every 6 hours (`0 */6 * * *`) in UTC.

You can change the cron line in the workflow. Example:
- Every 2 hours: `0 */2 * * *`
- Every day at 9:30 Cairo time (UTC+2/+3): use equivalent UTC cron.

### Required GitHub Secrets

In your repo:
- `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

Add:
- `VODA_MOBILE`
- `VODA_PASSWORD`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional:
- `VODA_LOGIN_URL`
- `VODA_ADSL_URL`

### Push to GitHub

```bash
git add .
git commit -m "Add GitHub Actions scheduler and secure gitignore"
git push origin main
```

If your default branch is not `main`, push that branch instead.

## Notes

- If Vodafone asks for OTP/captcha, automated login may fail.
- On failure, script sends error message to Telegram when `SEND_ERROR_TO_TELEGRAM=true`.
