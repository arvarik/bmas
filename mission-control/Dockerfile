# bMAS Mission Control — Next.js Dashboard
# Multi-stage build: build → production standalone server

# ── Stage 1: Build ────────────────────────────────────────────────────
FROM node:22.23.2-alpine3.23 AS builder

WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

# Install dependencies first (layer caching)
COPY package.json package-lock.json ./
RUN npm ci

# Copy the source. The runtime config mounts after the image starts.
COPY . .

# Build the Next.js standalone output
RUN npm run build

# ── Stage 2: Production ──────────────────────────────────────────────
FROM node:22.23.2-alpine3.23 AS runner

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# Don't run as root
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copy standalone build output
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

USER nextjs

EXPOSE 9321

ENV PORT=9321
ENV HOSTNAME="0.0.0.0"

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:9321/api/health || exit 1

CMD ["node", "server.js"]
