FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev

FROM node:20-alpine
RUN addgroup -S zalo && adduser -S zalo -G zalo
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY package.json server.js ./
RUN mkdir -p /app/sessions && chown zalo:zalo /app/sessions && chmod 700 /app/sessions
USER zalo
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD wget -qO- http://127.0.0.1:3000/health || exit 1
CMD ["node", "server.js"]
