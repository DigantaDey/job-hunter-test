FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-autofill.txt requirements-laya.txt ./
# Warm-up script copied early so the Laya weight layer below stays cached across
# normal code changes (COPY . . would invalidate it on every edit).
COPY scripts/warm_laya.py scripts/warm_laya.py
RUN pip install --no-cache-dir -r requirements.txt
# Assisted Apply's browser: WITH_AUTOFILL=1 bakes Playwright plus a Chromium
# build (and its OS libraries) into the image — roughly 400 MB more. The dev
# compose file and plain `docker build` include it; pass WITH_AUTOFILL=0 to opt out.
ARG WITH_AUTOFILL=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN if [ "$WITH_AUTOFILL" = "1" ]; then \
        pip install --no-cache-dir -r requirements-autofill.txt \
        && python -m playwright install --with-deps chromium \
        && chmod -R 755 /opt/ms-playwright; \
    fi
# Local decision engine (Laya): WITH_LAYA=1 bakes the optional typed-decision
# stack (CPU torch + transformers); LAYA_WARMUP=1 (the default whenever
# WITH_LAYA=1) also pre-downloads its Hugging Face checkpoints into $HF_HOME so
# decisions run offline. Opt in with --build-arg WITH_LAYA=1.
ARG WITH_LAYA=0
ARG LAYA_WARMUP=1
ENV HF_HOME=/opt/laya-cache
RUN if [ "$WITH_LAYA" = "1" ]; then \
        pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
        && pip install --no-cache-dir -r requirements-laya.txt \
        && if [ "$LAYA_WARMUP" = "1" ]; then python scripts/warm_laya.py; fi \
        && chmod -R 777 /opt/laya-cache; \
    fi
COPY . .
RUN mkdir -p /opt/ms-playwright && chmod -R 755 /opt/ms-playwright
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
