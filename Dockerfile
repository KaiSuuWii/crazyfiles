FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py .
COPY holiday_bot.py .

CMD ["python", "-u", "scraper.py"]