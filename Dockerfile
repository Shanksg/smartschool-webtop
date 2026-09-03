FROM python:3.12-slim

WORKDIR /app

# No Chrome, no Selenium: login is captcha-gated so browser automation is
# useless here. The previous image pulled ~1GB of google-chrome-stable that
# the running code never imported, and used apt-key, which is gone in
# Debian 12+ and broke rebuilds.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /app/config /app/logs

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY smartschool/ ./smartschool/
COPY run.py token_test.py ./

# Schedules and notifier/MQTT settings come from the environment; see
# docker-compose.yml and .env.example.
ENV SCHEDULES="12:00,16:00,20:00" \
    TOKEN_ROTATE_MINUTES="20"

CMD ["python", "run.py"]
