FROM public.ecr.aws/lambda/python:3.12

# Install font libraries (freetype, fontconfig) required by ffmpeg drawtext filter,
# plus DejaVu fonts as a reliable default font family
RUN dnf install -y tar gzip xz \
        freetype fontconfig fontconfig-devel \
        dejavu-sans-fonts dejavu-serif-fonts dejavu-sans-mono-fonts && \
    fc-cache -fv && \
    ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ]; then \
      curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz \
      | tar -xJ --strip-components=1 -C /usr/local/bin/ --wildcards '*/ffmpeg' '*/ffprobe'; \
    else \
      curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
      | tar -xJ --strip-components=1 -C /usr/local/bin/ --wildcards '*/ffmpeg' '*/ffprobe'; \
    fi && \
    ffmpeg -version && \
    echo "--- Verifying drawtext support ---" && \
    (ffmpeg -filters 2>&1 | grep -i drawtext || echo "WARN: drawtext filter not found in listing (may still work)")

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ARG LAMBDA_DIR=lambdas/nexus-research
COPY ${LAMBDA_DIR}/handler.py ${LAMBDA_TASK_ROOT}/handler.py

CMD ["handler.lambda_handler"]
