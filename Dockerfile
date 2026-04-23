FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV JARVIS_ENV=production
ENV JARVIS_API_ENABLED=true
ENV JARVIS_API_HOST=0.0.0.0
ENV JARVIS_API_PORT=8000
ENV JARVIS_CONSOLE_LOGGING=true
ENV JARVIS_LOG_LEVEL=INFO

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import json, urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4); data = json.load(response); raise SystemExit(0 if data.get('status') in {'ok', 'configured', 'unknown'} else 1)"

CMD ["python", "-m", "uvicorn", "jarvis.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
