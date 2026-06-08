FROM python:3.12-slim
WORKDIR /app
COPY server.py index.html ./
# Pure standard-library app — no pip install needed.
# Railway sets $PORT; server.py binds 0.0.0.0:$PORT when PORT is present.
CMD ["python3", "server.py"]
