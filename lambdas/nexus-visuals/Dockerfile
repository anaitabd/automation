FROM public.ecr.aws/lambda/python:3.12

RUN dnf install -y \
        freetype fontconfig fontconfig-devel \
        dejavu-sans-fonts dejavu-serif-fonts dejavu-sans-mono-fonts && \
    fc-cache -fv && \
    dnf clean all

RUN pip install --no-cache-dir \
    requests boto3 psycopg2-binary python-dotenv json-repair Pillow \
    --extra-index-url https://pypi.org/simple/

RUN mkdir -p /mnt/scratch

COPY lambdas/nexus_pipeline_utils.py ${LAMBDA_TASK_ROOT}/nexus_pipeline_utils.py
COPY lambdas/nexus-visuals/handler.py ${LAMBDA_TASK_ROOT}/handler.py
COPY lambdas/shared/nova_canvas.py ${LAMBDA_TASK_ROOT}/nova_canvas.py
COPY lambdas/shared/nova_reel.py ${LAMBDA_TASK_ROOT}/nova_reel.py

ENV TMPDIR=/mnt/scratch

ENTRYPOINT ["python"]
CMD ["/var/task/handler.py"]
