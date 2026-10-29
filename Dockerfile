FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd --system app \
    && useradd --system --gid app --create-home app

COPY requirements-server.txt ./requirements-server.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-server.txt

COPY . .
RUN mkdir -p /app/data /app/output /app/models \
    && chown -R app:app /app

USER app

EXPOSE 8501

CMD ["python", "-m", "streamlit", "run", "main.py", "--server.address=0.0.0.0", "--server.port=8501"]
