# Slim image (~0.7 GB): Python + Playwright + Chromium headless shell only.
FROM python:3.12-slim
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir playwright==1.56.0 pypdf==5.1.0 tzdata==2025.2 && playwright install --with-deps --only-shell chromium && rm -rf /var/lib/apt/lists/* /root/.cache
WORKDIR /app
COPY agent ./agent
ENV TASK_SECONDS=20
ENV MIN_TASK_SECONDS=4
ENV TIME_STRETCH=2.5
ENV MAX_TURNS=10
ENV MIN_CONFIDENCE=0.7
ENV PAGE_POOL=6
ENV PROBE=0
CMD ["python3", "-m", "agent.main"]
