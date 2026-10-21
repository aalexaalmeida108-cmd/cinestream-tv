# CineStream TV - a copia que roda sem o PC

O site da TV (catalogo, busca, capas, player) e o gateway (manifestos
MPEG-TS e segmentos) estao separados porque so o segundo precisa de uma
maquina acordada. Este repositorio e o painel dos dois.

```
/
├── index.html          o catalogo (ES5, controle remoto)
├── player.html         o player (ES5, hls.js)
├── js/config.js        o unico lugar que muda entre as copias
├── data/               o catalogo enxuto (3 MB)
├── posters/            180 capas, que viajam com o site
├── css/tv.css
├── deploy/             o kit do host na nuvem
│   ├── resolver.py       o gateway, com caminhos por ambiente e CORS opcional
│   ├── patch_resolver.py  como o gateway da caixa vira o da nuvem
│   ├── bundle_posters.py  empacota as capas
│   ├── install.sh        instala tudo num host novo
│   ├── Caddyfile         HTTPS, site em disco, gateway por tras
│   ├── set_api.py        aponta o player para um gateway
│   ├── sync_pages.sh     atualiza o catalogo e publica
│   └── README.md         o passo a passo do host
└── .github/workflows/deploy.yml   publica o site e acorda o gateway
```

O endereco do site e `https://aalexaalmeida108-cmd.github.io/cinestream-tv/`
e ele sobe sozinho, sem o PC. O gateway e o processo que assina a playlist
no host de stream e remuxa CMAF para MPEG-TS - esse precisa de uma maquina
com IP publico.

## As tres copias, um arquivo de configuracao

| copia | `CINE_API` | onde roda |
|---|---|---|
| a caixa | `''` | `http://192.168.1.106:8730/site/` |
| o GitHub Pages | o endereco do gateway | `https://aalexaalmeida108-cmd.github.io/cinestream-tv/` |
| o host na nuvem | `''` | `https://tv.exemplo.com/` |

`CINE_API` e a base do gateway. Vazio significa "mesma origem", que e o caso
normal: a pagina e o gateway sao servidos pela mesma maquina, e o navegador
antigo da TV nao tem ninguem para perguntar sobre origem cruzada. Ele so
precisa de um valor quando a pagina esta no GitHub Pages e o gateway em
outro host - e nesse caso o gateway precisa responder com
`Access-Control-Allow-Origin` (`--cors` no `install.sh`).

`CINE_WAKE_URL` e a campainha: se o host na nuvem dormir, o player bate na
porta e tenta o manifesto de novo.

## O que o GitHub segura e o que ele nao segura

| | GitHub Pages | GitHub Actions | Codespaces |
|---|---|---|---|
| site estatico | sim | - | sim |
| processo vivo com URL publico | nao | nao (janelas de 5 em 5 min) | sim, e dorme |
| custo | zero | zero | ~0,18 USD/h |

O gateway precisa de um processo vivo: a playlist e assinada e vale uns 30
minutos, e o remux para MPEG-TS acontece no momento do play. O GitHub nao
tem servidor HTTP publico que fique de pe. O que o GitHub faz e o painel:
o repositorio guarda o site, o Pages publica, e a Action envia o site e o
gateway para a maquina e acorda ela de 20 em 20 minutos.

Para o gateway, o host e o pedaco que precisa de IP publico:

* **Oracle Cloud Always Free** - 1 VM ARM de graca, 24/7, IP publico.
* **VPS de 4 EUR/mes** (Hetzner, Contabo) - o mais simples.
* **Codespaces** - o unico "GitHub de verdade" que roda o gateway, mas e
  pago e a URL muda quando o codespace e renomeado. Serve para teste.

## O que a copia na nuvem mudou

* `js/config.js` - novo. A base do gateway, o modo das capas, a campainha e
  o catalogo publico ficam em um so arquivo.
* `index.html` - a capa vem do pacote (`posters/<nome>`). A capa que nao
  esta no pacote cai no host de imagens, pelo `error` do `<img>`.
* `player.html` - o `sourceUrl` e `CINE_API + '/tv/...'`. Quando o
  manifesto nao carrega, o player acorda o host (se houver campainha) e,
  se nao houver, diz que o gateway nao esta configurado neste endereco.
* O resto e o site da caixa, byte por byte.

## O que o gateway da nuvem mudou

`deploy/patch_resolver.py` le o `resolver.py` da caixa e escreve uma copia
com tres mudancas, e nada mais:

1. Os caminhos vem do ambiente (`RESOLVER_HOME`, `RESOLVER_SITE_DIR`,
   `RESOLVER_CATALOG_DIR`, `RESOLVER_POSTER_DIR`, `RESOLVER_M3U_PATH`,
   `RESOLVER_FFMPEG`).
2. A base publica pode ser fixada (`RESOLVER_PUBLIC_BASE`). Atras de um
   proxy, o `Host` chega sem porta e sem esquema, e a playlist apontaria
   para `http://dominio:8730` - porta que nao esta aberta.
3. O CORS e opcional (`RESOLVER_CORS_ORIGIN`). Vazio = nenhuma resposta de
   CORS, exatamente como na caixa.

O `resolver.py` da caixa nao e tocado.

## Publicar

```bash
cd site
git add -A
git commit -m "CineStream TV"
git push origin main
```

O Pages publica em um minuto. O `deploy/sync_pages.sh` (no cron da caixa, a
cada 6 horas) atualiza o catalogo, pega as capas novas e publica.

Para o host na nuvem:

```bash
sudo ./deploy/install.sh --domain tv.exemplo.com --email voce@exemplo.com
```

E, depois, os segredos da Action no GitHub: `VPS_HOST`, `VPS_USER`,
`VPS_SSH_KEY`, `VPS_PATH`, `PUBLIC_BASE`, `WAKE_URL`. A partir dai o
`git push` publica o site e envia o gateway para a maquina.
