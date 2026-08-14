# Legalix Mandat Bot — Render deployment

## Files
- `Legalix_Mandat_Bot.py` — production bot
- `Dockerfile` — Playwright/Chromium runtime
- `requirements.txt` — Python dependencies
- `render.yaml` — Render Background Worker + persistent disk
- `.dockerignore`

## Deploy
1. Upload these files to a GitHub repository.
2. In Render choose **New → Blueprint** and select the repository.
3. Render reads `render.yaml` and creates the worker.
4. The SQLite database is stored at `/var/data/mandat_bot.sqlite3` on the persistent disk.

## Local run
```bash
pip install -r requirements.txt
python Legalix_Mandat_Bot.py
```

## Important
The bot token is embedded in the Python file as requested. For production, rotate the token in BotFather because it was exposed during development.
