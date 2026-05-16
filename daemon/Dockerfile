# bMAS Daemon — Python FastAPI Application
FROM python:3.13-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ src/
COPY tests/ tests/

# Source package is the Python path
ENV PYTHONPATH=/app/src

EXPOSE 9000

# Health check — verifies the /health endpoint returns status=healthy
# (not just HTTP 200, which also covers "degraded" states)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "\
import urllib.request, json, sys; \
r = urllib.request.urlopen('http://localhost:9000/health'); \
d = json.loads(r.read()); \
sys.exit(0 if d.get('status') == 'healthy' else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000", "--workers", "1"]
