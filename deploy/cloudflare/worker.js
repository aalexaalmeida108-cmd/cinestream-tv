// CineStream TV - o gateway como Worker + Container da Cloudflare.
//
// O Worker e a porta: ele recebe o pedido do player (que esta no GitHub
// Pages, outra origem) e entrega para o container, que roda o resolver com
// ffmpeg.
//
// Um container so, com nome fixo. O resolver guarda a sessao do titulo
// (a playlist assinada e os segmentos remuxados) na memoria, e os segmentos
// de um titulo precisam chegar na mesma instancia que montou o manifesto.
// Se o container dormir, a sessao se perde - o player percebe e pede o
// manifesto de novo, e o container acorda.
//
// sleepAfter = 10m: o container fica de pe entre um filme e outro e some
// sozinho quando a casa para de assistir.

import { Container } from "@cloudflare/containers";

export class Gateway extends Container {
  defaultPort = 8730;
  sleepAfter = "10m";
  enableInternet = true;

  envVars = {
    RESOLVER_HOST: "0.0.0.0",
    RESOLVER_PORT: "8730",
    RESOLVER_MODE: "proxy",
    RESOLVER_CACHE_TTL: "300",
    RESOLVER_CORS_ORIGIN: "*",
    RESOLVER_HOME: "/opt/cinestream",
    RESOLVER_SITE_DIR: "/opt/cinestream/site",
    RESOLVER_CATALOG_DIR: "/opt/cinestream/site/data",
    RESOLVER_POSTER_DIR: "/tmp/posters",
    RESOLVER_FFMPEG: "/usr/bin/ffmpeg",
  };
}

// As rotas do gateway. Tudo o mais responde 404.
const API = /^\/(tv|tsseg|seg|stream|check|cat|poster|healthz)(\/|$)/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return cors(new Response(null, { status: 204 }));
    }

    if (!API.test(url.pathname)) {
      return cors(new Response("CineStream gateway", { status: 404 }));
    }

    // Uma instancia so. O nome e fixo de proposito: e a sessao do titulo
    // que precisa ser a mesma entre o manifesto e os segmentos.
    const container = env.GATEWAY.getByName("cinestream");
    const response = await container.fetch(request);
    return cors(response);
  },
};

// O site esta no GitHub Pages, que e outra origem. O player antigo da TV
// (Chrome 47) so aceita a resposta com o cabecalho de permissao.
function cors(response) {
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
  headers.set("Access-Control-Allow-Headers", "*");
  headers.set("Access-Control-Expose-Headers", "*");
  headers.set("Access-Control-Max-Age", "600");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
