# ─────────────────────────────────────────────────────────────────────
# LiteLLM — Model Router Container
# ─────────────────────────────────────────────────────────────────────
# Extends the official LiteLLM image with the bMAS config generator.
# At startup, entrypoint.sh generates a LiteLLM-native config.yaml
# from bmas.yaml, then launches the proxy.
# ─────────────────────────────────────────────────────────────────────

FROM ghcr.io/berriai/litellm:main-stable

# Copy config generator and entrypoint
COPY generate_config.py /app/generate_config.py
COPY entrypoint.sh /app/entrypoint.sh

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
