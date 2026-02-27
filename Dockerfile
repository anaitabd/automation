FROM public.ecr.aws/lambda/python:3.12

RUN dnf install -y tar gzip xz && \
    curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz \
    | tar -xJ --strip-components=1 -C /usr/local/bin/ --wildcards '*/ffmpeg' '*/ffprobe' || true

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ARG LAMBDA_DIR=lambdas/nexus-research
COPY ${LAMBDA_DIR}/handler.py ${LAMBDA_TASK_ROOT}/handler.py

CMD ["handler.lambda_handler"]
