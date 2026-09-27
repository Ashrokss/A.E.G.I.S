FROM public.ecr.aws/docker/library/node:20-bookworm-slim

WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY src/ ./src/
COPY scripts/ ./scripts/
# migrate.js refuses to start if this directory is missing (see src/db/migrate.js).
COPY migrations/ ./migrations/

# RDS CA bundle for POSTGRES_SSL=true verification; POSTGRES_SSL_CA points here.
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /app/rds-ca.pem
RUN chmod 0644 /app/rds-ca.pem

USER node
EXPOSE 3005
CMD ["node", "src/server.js"]
