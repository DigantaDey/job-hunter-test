FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-autofill.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Assisted Apply's browser: WITH_AUTOFILL=1 bakes Playwright plus a Chromium
# build (and its OS libraries) into the image — roughly 400 MB more. The dev
# compose file passes 1; a plain `docker build` stays minimal.
ARG WITH_AUTOFILL=0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN if [ "$WITH_AUTOFILL" = "1" ]; then \
        pip install --no-cache-dir -r requirements-autofill.txt \
        && playwright install --with-deps chromium \
        && chmod -R 755 /opt/ms-playwright; \
    fi
COPY . .
RUN mkdir -p /opt/ms-playwright && chmod -R 755 /opt/ms-playwright
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
