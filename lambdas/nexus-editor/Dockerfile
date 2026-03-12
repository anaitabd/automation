FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        python3 \
        python3-pip \
        python3-boto3 \
        fonts-dejavu-core \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libnss3 \
        libnspr4 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxext6 \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY lambdas/nexus-editor/package.json ./
RUN npm install --omit=dev

COPY lambdas/nexus-editor/render.js ./
COPY lambdas/nexus-editor/src ./src
COPY lambdas/nexus_pipeline_utils.py ./nexus_pipeline_utils.py

ENV TMPDIR=/mnt/scratch

RUN mkdir -p /mnt/scratch

ENTRYPOINT ["node", "render.js"]
