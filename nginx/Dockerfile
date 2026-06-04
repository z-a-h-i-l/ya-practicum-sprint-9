FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nginx-extras \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /var/cache/nginx/cdn && \
    chown -R www-data:www-data /var/cache/nginx/cdn

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 8083

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8083/health || exit 1

CMD ["nginx", "-g", "daemon off;"]