FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHORTFORM_STUDIO_HOME=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin studio \
    && mkdir -p /app /data \
    && chown -R 10001:10001 /app /data

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 shortform_studio ./shortform_studio

USER 10001:10001
EXPOSE 8765 8766
CMD ["python", "-m", "uvicorn", "shortform_studio.main:app", "--host", "0.0.0.0", "--port", "8765"]
