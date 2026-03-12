FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
        fonts-dejavu-core \
        libsndfile1 \
    && ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "arm64" ]; then \
         curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz \
         | tar -xJ --strip-components=1 -C /usr/local/bin/ --wildcards '*/ffmpeg' '*/ffprobe'; \
       else \
         curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
         | tar -xJ --strip-components=1 -C /usr/local/bin/ --wildcards '*/ffmpeg' '*/ffprobe'; \
       fi \
    && ffmpeg -version \
    && apt-get purge -y --auto-remove xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY lambdas/nexus-shorts/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy shared utilities
COPY lambdas/nexus_pipeline_utils.py /app/nexus_pipeline_utils.py
COPY lambdas/shared/nova_canvas.py   /app/nova_canvas.py
COPY lambdas/shared/nova_reel.py     /app/nova_reel.py
COPY lambdas/shared/motion.py        /app/motion.py

# Copy all shorts module files
COPY lambdas/nexus-shorts/ /app/

ENV TMPDIR=/mnt/scratch
RUN mkdir -p /mnt/scratch

ENTRYPOINT ["python"]
CMD ["/app/handler.py"]

