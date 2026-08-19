FROM python:3.13.14-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -r requirements.txt

COPY api_server.py .

RUN addgroup --system --gid 1001 bmas \
    && adduser --system --uid 1001 --ingroup bmas bmas \
    && mkdir -p /var/lib/bmas-agent/traces /var/lib/bmas-agent/activations \
    && chown -R bmas:bmas /var/lib/bmas-agent

USER bmas

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACE_SPOOL_DIR=/var/lib/bmas-agent/traces \
    ACTIVATION_CACHE_DIR=/var/lib/bmas-agent/activations

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python3 -c "import json,urllib.request,sys; data=json.loads(urllib.request.urlopen('http://127.0.0.1:8000/health').read()); sys.exit(0 if data.get('status') == 'healthy' else 1)"

CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
