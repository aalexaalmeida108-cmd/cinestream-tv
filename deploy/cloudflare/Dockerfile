# CineStream TV - o gateway na Cloudflare.
#
# A imagem e construida pela Action, com o contexto no racine do
# repositorio (por isso os caminhos de COPY comecam em deploy/ e site/).
#
#   docker build -t registry.cloudflare.com/<CONTA>/cinestream-gateway:latest \
#       -f deploy/cloudflare/Dockerfile .

FROM python:3.12-slim

# ffmpeg e o que converte o CMAF do CDN em MPEG-TS, que e o unico formato
# que os players da TV decodificam.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/cinestream

COPY deploy/resolver.py /opt/cinestream/resolver.py
COPY site /opt/cinestream/site

ENV RESOLVER_HOME=/opt/cinestream \
    RESOLVER_SITE_DIR=/opt/cinestream/site \
    RESOLVER_CATALOG_DIR=/opt/cinestream/site/data \
    RESOLVER_POSTER_DIR=/tmp/posters \
    RESOLVER_FFMPEG=/usr/bin/ffmpeg \
    RESOLVER_HOST=0.0.0.0 \
    RESOLVER_PORT=8730 \
    RESOLVER_MODE=proxy \
    RESOLVER_CACHE_TTL=300 \
    RESOLVER_CORS_ORIGIN=*

EXPOSE 8730

CMD ["python3", "-u", "/opt/cinestream/resolver.py"]
