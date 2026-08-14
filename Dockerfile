FROM mcr.microsoft.com/playwright/python:v1.61.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY Legalix_Mandat_Bot.py .
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV DB_PATH=/var/data/mandat_bot.sqlite3
CMD ["python", "Legalix_Mandat_Bot.py"]
