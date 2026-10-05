FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STORE_PATH=/data/reviews.json \
    PORT=8000 \
    HOST=0.0.0.0

WORKDIR /opt/interlock-review
COPY app ./app
COPY tests ./tests
COPY scripts ./scripts
RUN chmod +x ./scripts/verify.sh

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

CMD ["python", "-m", "app.server"]
