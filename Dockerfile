FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHORTFORM_STUDIO_HOME=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg fontconfig fonts-noto-cjk \
    && mkdir -p /usr/share/fonts/truetype/korean-shorts \
    && curl -fsSL -o /usr/share/fonts/truetype/korean-shorts/BlackHanSans-Regular.ttf https://github.com/google/fonts/raw/main/ofl/blackhansans/BlackHanSans-Regular.ttf \
    && curl -fsSL -o /usr/share/fonts/truetype/korean-shorts/DoHyeon-Regular.ttf https://github.com/google/fonts/raw/main/ofl/dohyeon/DoHyeon-Regular.ttf \
    && curl -fsSL -o /usr/share/fonts/truetype/korean-shorts/Jua-Regular.ttf https://github.com/google/fonts/raw/main/ofl/jua/Jua-Regular.ttf \
    && curl -fsSL -o /usr/share/fonts/truetype/korean-shorts/GothicA1-ExtraBold.ttf https://github.com/google/fonts/raw/main/ofl/gothica1/GothicA1-ExtraBold.ttf \
    && curl -fsSL -o /usr/share/fonts/truetype/korean-shorts/Sunflower-Bold.ttf https://github.com/google/fonts/raw/main/ofl/sunflower/Sunflower-Bold.ttf \
    && curl -fsSL -o /usr/share/fonts/truetype/korean-shorts/Pretendard-Bold.otf https://github.com/orioncactus/pretendard/raw/main/packages/pretendard/dist/public/static/Pretendard-Bold.otf \
    && curl -fsSL -o /usr/share/fonts/truetype/korean-shorts/Pretendard-ExtraBold.otf https://github.com/orioncactus/pretendard/raw/main/packages/pretendard/dist/public/static/Pretendard-ExtraBold.otf \
    && fc-cache -fv \
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
