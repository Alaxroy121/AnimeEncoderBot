# ============================================
# AnimeEncoderBot - Docker Image
# GPU-accelerated video encoding + AI upscaling
# ============================================

FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04

LABEL maintainer="AnimeEncoderBot"
LABEL description="Telegram bot for GPU-accelerated video encoding and AI anime upscaling"

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ── System dependencies ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3-pip \
    python3.10-venv \
    ffmpeg \
    wget \
    unzip \
    libvulkan1 \
    vulkan-tools \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ── Install Real-ESRGAN ──
RUN mkdir -p /opt/realesrgan && \
    wget -q https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip \
    -O /tmp/realesrgan.zip && \
    unzip /tmp/realesrgan.zip -d /opt/realesrgan && \
    chmod +x /opt/realesrgan/realesrgan-ncnn-vulkan && \
    ln -sf /opt/realesrgan/realesrgan-ncnn-vulkan /usr/local/bin/realesrgan-ncnn-vulkan && \
    rm /tmp/realesrgan.zip

# ── Working directory ──
WORKDIR /app

# ── Python dependencies ──
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# ── Copy bot code ──
COPY . .

# ── Create downloads directory ──
RUN mkdir -p /app/downloads

# ── Environment ──
ENV REALESRGAN_PATH=/usr/local/bin/realesrgan-ncnn-vulkan
ENV DOWNLOAD_DIR=/app/downloads

# ── Run the bot ──
CMD ["python3", "bot.py"]
