FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
# yt-dlp[deno] brings the Deno version yt-dlp needs (2.9.8+) to solve YouTube's player challenges; without it a
# signed-in YouTube download lists no video formats ("n challenge solving failed", "Requested format is not available")
RUN pip install --no-cache-dir -r requirements.txt

# Models baked into the image so a fresh deploy can render right away:
# YuNet face detector (webcam finder) and the faster-whisper "small" model used by moments.py.
RUN python -c "from huggingface_hub import hf_hub_download as d; import os; \
p=os.path.expanduser('~/.cache/gtavi-claudecode/models'); os.makedirs(p, exist_ok=True); \
d('opencv/face_detection_yunet','face_detection_yunet_2023mar.onnx', local_dir=p)" \
 && python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

# Headless Chromium for auto-submitting post links on Content Rewards (integrations/browser_submit.py).
# Debian's own chromium package (Playwright's bundled browser doesn't support this Debian release); Playwright
# drives it through CHROMIUM_PATH. Build with WITH_BROWSER=0 to leave it out.
ARG WITH_BROWSER=1
RUN if [ "$WITH_BROWSER" = "1" ]; then apt-get update \
      && apt-get install -y --no-install-recommends chromium fonts-liberation \
      && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir "playwright>=1.49"; fi
ENV CHROMIUM_PATH=/usr/bin/chromium

COPY app ./app
COPY vendor ./vendor

ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
