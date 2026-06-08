FROM python:3.12-slim
WORKDIR /app
COPY server.py backtest.py index.html prices_web.db corp_actions.json ./
# Pure standard-library app — no pip install needed.
# prices_web.db = top-500 liquid NSE stocks, 5y daily (built by ingest.py).
# Railway sets $PORT; server.py binds 0.0.0.0:$PORT when PORT is present.
CMD ["python3", "server.py"]
