FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY game ./game
COPY static ./static
COPY server.py .
RUN useradd --create-home --uid 10001 tarot && mkdir /app/data && chown tarot:tarot /app/data
USER tarot
ENV HOST=0.0.0.0 PORT=8080 TAROT_DB=/app/data/room.sqlite3
EXPOSE 8080
VOLUME ["/app/data"]
CMD ["python", "server.py"]
