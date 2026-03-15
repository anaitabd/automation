FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    wget \
    fonttools \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Download Montserrat Bold font and verify integrity
RUN wget -q -O /usr/share/fonts/truetype/montserrat-bold.ttf \
    "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Bold.ttf" \
    && echo "7a267b18a994d3c4e9dd64f49f7e75882cf8dea0  /usr/share/fonts/truetype/montserrat-bold.ttf" \
    | sha1sum -c - \
    || { echo "WARNING: Montserrat font checksum failed — removing, Liberation Sans will be used" \
        && rm -f /usr/share/fonts/truetype/montserrat-bold.ttf; }

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "handler.py"]
