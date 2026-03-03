FROM public.ecr.aws/lambda/python:3.12

# Install font libraries required by ffmpeg drawtext filter
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
    dnf clean all

# Install Python dependencies
RUN pip install --no-cache-dir \
    requests boto3 psycopg2-binary python-dotenv json-repair Pillow \
    sentence-transformers numpy scipy scikit-learn \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple/

COPY lambdas/nexus_pipeline_utils.py ${LAMBDA_TASK_ROOT}/nexus_pipeline_utils.py
COPY lambdas/nexus-visuals/handler.py ${LAMBDA_TASK_ROOT}/handler.py

CMD ["handler.lambda_handler"]

