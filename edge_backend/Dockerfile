# Edge AI CCTV backend image.
#
# GPU: no CUDA base image is needed. On x86_64 requirements.txt installs
# onnxruntime-gpu[cuda,cudnn], whose nvidia-* pip wheels carry the CUDA and
# cuDNN libraries; only the host's NVIDIA driver is required, passed in by the
# NVIDIA Container Toolkit (see docker-compose.gpu.yml). Without it the same
# image runs on the CPU provider. aarch64 builds get the plain onnxruntime.
#
# Build context: edge_backend/.  The in-image layout mirrors the repo
# (/opt/edge-cctv/edge_backend/app, /opt/edge-cctv/storage) because app.config
# resolves the storage root relative to the source tree.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

# FFmpeg / GStreamer for capture; Intel VA-API driver only exists on amd64.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv \
        ffmpeg vainfo libva-drm2 libva2 \
        libgstreamer1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-libav \
        libgl1 libglib2.0-0 curl ca-certificates \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        apt-get install -y --no-install-recommends intel-media-va-driver-non-free; fi \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs into a venv (Ubuntu 24.04's system
# Python refuses pip installs, PEP 668).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /opt/edge-cctv/edge_backend

COPY requirements.txt .
RUN uv venv --python /usr/bin/python3 /opt/venv \
    && uv pip install --no-cache -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY models/ ./models/
COPY go2rtc.yaml entrypoint.sh ./

# Non-root user in the video/render groups for V4L2 and VA-API. GPU access is
# granted by the NVIDIA runtime, not by group membership.
# The base image has no `render` group; its GID will not match the host's, so
# compose may also need `group_add` with the host's render GID for VA-API.
RUN groupadd -f -r render && groupadd -r appgroup && useradd -r -g appgroup -G video,render appuser \
    && mkdir -p /opt/edge-cctv/storage \
    && chmod +x entrypoint.sh \
    && chown -R appuser:appgroup /opt/edge-cctv/storage
USER appuser

EXPOSE 8000
CMD ["./entrypoint.sh"]
