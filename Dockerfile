# ==============================================================================
# AI CCTV Physical Display Monitoring & Intrusion Detection
# Fully Self-Contained, Airgapped Production Docker Image (Python Base)
# ==============================================================================

FROM python:3.12-slim-bookworm

# Set environment variables for non-interactive installs and python buffering
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8080 \
    APP_HOME=/app \
    PYTHONPATH="/app/src" \
    DATABASE_URL=sqlite:////app/data/cctv_events.db \
    EVENTS_STORAGE_PATH=/app/data/events \
    KNOWN_PERSONS_STORAGE_PATH=/app/data/known_persons.json

WORKDIR ${APP_HOME}

# Install essential native system libraries required by OpenCV, ONNX Runtime, and healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from requirements.txt
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy application source code, configuration, database schemas, and project metadata
COPY src/ ./src/
COPY tests/ ./tests/
COPY config/ ./config/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY pyproject.toml ./
COPY README.md ./

# Install local package in editable mode
RUN pip install --no-cache-dir --no-deps -e .

# Copy all offline AI deep learning models and data files into the image
COPY data/ ./data/
RUN cp /app/data/yolo11n.pt /app/yolo11n.pt 2>/dev/null || true

# Ensure all runtime storage directories exist with write permissions
RUN mkdir -p /app/data/events /app/data/output /app/data/snapshots


# Expose web server and live streaming port
EXPOSE 8080

# Healthcheck to ensure API and video pipeline are responsive
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/api/status || exit 1

# Launch production ASGI server
CMD ["uvicorn", "cctv_poc.web.server:app", "--host", "0.0.0.0", "--port", "8080"]
