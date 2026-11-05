FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DB_PATH=/app/data/league.sqlite3
WORKDIR /app
RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid bot bot && mkdir /app/data && chown bot:bot /app/data
COPY --chown=bot:bot bot.py .
USER bot
CMD ["python", "bot.py"]
