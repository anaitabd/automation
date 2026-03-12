FROM public.ecr.aws/lambda/python:3.12

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

RUN pip install --no-cache-dir \
    requests boto3 psycopg2-binary python-dotenv json-repair \
    librosa numpy scipy scikit-learn Pillow \
    --extra-index-url https://pypi.org/simple/

RUN mkdir -p /mnt/scratch

COPY lambdas/nexus_pipeline_utils.py ${LAMBDA_TASK_ROOT}/nexus_pipeline_utils.py
COPY lambdas/nexus-editor/handler.py ${LAMBDA_TASK_ROOT}/handler.py
COPY lambdas/shared/nova_canvas.py ${LAMBDA_TASK_ROOT}/nova_canvas.py

ENV TMPDIR=/mnt/scratch

# Override the Lambda RIC entrypoint — run as a standalone Python script.
# Step Functions passes inputs via ECS ContainerOverrides environment variables;
# lambda_handler() already reads them with os.environ fallbacks.
ENTRYPOINT ["python"]
CMD ["/var/task/handler.py"]

