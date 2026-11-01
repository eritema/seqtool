FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends squid socat ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY squid.conf /etc/squid/squid.conf
USER proxy
ENTRYPOINT ["squid", "-N", "-f", "/etc/squid/squid.conf"]
