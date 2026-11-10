FROM python:3.12-slim
ENV TZ=Europe/Berlin PYTHONUNBUFFERED=1 NOVA_DATA=/data PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ffmpeg && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir edge-tts pywebpush
WORKDIR /app
COPY server.py config.json workouts.json ./
COPY static ./static
EXPOSE 8080
CMD ["python", "server.py"]
