# OpenCuria push proxy: stateless, end-to-end encrypted APNs/FCM relay.
#   docker build -t opencuria-push-proxy .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY push_proxy ./push_proxy
RUN useradd --system --uid 10001 --no-create-home push-proxy
USER push-proxy

EXPOSE 8080
CMD ["python", "-m", "push_proxy"]
