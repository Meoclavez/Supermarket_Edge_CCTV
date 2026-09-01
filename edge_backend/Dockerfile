# Multi-stage Dockerfile with Intel VA-API drivers and Python HailoRT runtime
FROM ubuntu:22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install base dependencies, FFmpeg, and Intel QuickSync / VA-API drivers
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    ffmpeg \
    vainfo \
    intel-media-va-driver-non-free \
    libva-drm2 \
    libva2 \
    libgstreamer1.0-0 \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-libav \
    libgl1 \
    libglib2.0-0 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ /app/app/
COPY models_hef/ /app/models_hef/

# Create a non-root user and add to video/render groups for VA-API
RUN groupadd -r appgroup && useradd -r -g appgroup -G video,render appuser
RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
