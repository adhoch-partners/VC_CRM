FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vc_crm ./vc_crm
RUN useradd --create-home --uid 10001 crm
USER crm

# One worker: the login throttle is in-memory, and 2-10 users do not need more.
# --proxy-headers so the client IP and https scheme come from the host's load balancer.
CMD ["sh", "-c", "uvicorn vc_crm.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
