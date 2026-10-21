# CineStream TV na nuvem

O site da TV tem tres partes, e so uma delas precisa do PC:

| parte | onde roda | sem o PC |
|---|---|---|
| catalogo (pagina, busca, capas) | GitHub Pages ou o host | sim |
| gateway (manifestos MPEG-TS e segmentos) | host na nuvem | sim |
| Plex (o app da TV) | host na nuvem, opcional | sim |

O **gateway** e o `resolver.py`: ele pergunta ao host de stream qual e a
playlist assinada do titulo, reescreve a playlist com URLs absolutas e
serve os segmentos em MPEG-TS. A TV nao decodifica CMAF, entao o remux para
MPEG-TS continua obrigatorio - o `ffmpeg` faz esse trabalho.

## O que o script instala

```
/opt/cinestream/
├── site/            o site (pagina, player, catalogo, capas)
├── resolver.py      o gateway
├── cache/           catalogo completo (opcional)
├── posters/         capas baixadas em tempo de uso
└── ffmpeg           (o do sistema, se nao passar --ffmpeg)

/etc/cinestream/env         as variaveis do servico
/etc/systemd/system/cinestream-resolver.service
/etc/caddy/Caddyfile        o site em disco + o gateway por tras
```

## Passo a passo

1. **Um host com IP publico.** Qualquer VPS serve. Oracle Cloud Always Free
   (2 VMs ARM, 1 GB cada, de graca), Hetzner (a partir de 4 EUR/mes), ou
   qualquer outro. Um host 1 vCPU / 1 GB / 20 GB roda o gateway com folga -
   ele so remuxa o que a TV pede.

2. **O dominio aponta para o host.** Registro A do dominio = IP publico do
   host. Sem isso o Caddy nao consegue o certificado. Sem dominio serve um
   `duckdns.org` (gratis): `https://nomedojovem.duckdns.org`.

3. **Rodar o script:**

```bash
sudo ./install.sh --domain tv.exemplo.com --email voce@exemplo.com
```

O script instala o python, o ffmpeg e o Caddy, copia o site, escreve o
servico, sobe tudo e confere:

```
gateway : 200
site    : 200
publico : 200
```

4. **A TV passa a abrir** `https://tv.exemplo.com/` - o mesmo caminho de
   antes (`/minatv` virou `/`), e a pagina e o gateway estao na mesma
   origem, entao o navegador antigo da TV nao reclama de nada.

## O site no GitHub Pages e o gateway na nuvem

O catalogo tambem pode ficar no GitHub Pages, que e mais rapido de servir e
nao gasta banda da maquina. A copia do site entao fala com o gateway de
outra maquina, e o navegador precisa da permissao de origem cruzada:

```bash
sudo ./install.sh --domain tv.exemplo.com --email voce@exemplo.com \
     --cors https://aalexaalmeida108-cmd.github.io
```

E no `js/config.js` da copia do site:

```js
var CINE_API = 'https://tv.exemplo.com';
var CINE_POSTER_MODE = 'bundle';
```

`CINE_POSTER_MODE` fica `bundle` (as capas viajam com o site) ou `api` (o
gateway serve a capa em `/poster/<nome>`).

## O catalogo se atualiza sozinho

O catalogo completo vem do host de stream e muda de hora em hora. O
`cron` da caixa empurra a copia enxuta para o repositorio do site a cada 6
horas. Na maquina da nuvem o catalogo em `site/data/` e estatico, entao:

```bash
rsync -a --delete /home/samba/tv-cloud/site/data/ \
      root@tv.exemplo.com:/opt/cinestream/site/data/
```

ou, se o catalogo mora so no GitHub Pages, o gateway le de la
(`RESOLVER_CATALOG_DIR`).

## O gateway em um container

O `Dockerfile` nao existe ainda porque a instalacao com script funciona e
nao depende de daemon de container. Se um dia o host for um container, o
gateway e um `python3 resolver.py` com as mesmas variaveis de ambiente.

## Plex (opcional)

O app Plex da TV e outra parada: ele precisa do Plex Media Server (2,8 GB
de dados aqui) e do servidor publicado na conta
(`aalexaalmeida108@gmail.com`). Para levar:

```bash
rsync -a /home/samba/plex/data/ /home/samba/plex/library/ \
      /home/samba/plex/pms/ root@tv.exemplo.com:/opt/plex/
```

Depois o servidor e publicado no `plex.tv` com o IP publico do host, e o
`plex.tv` precisa alcancar a maquina. Sem o `--min-votes` a biblioteca
inteira entra: sao 3.904 arquivos `.strm`.

## O que nao depende do resolucao

`/check/movie/<id>` responde `ok`, `missing` ou `transient`. `missing` e um
titulo que o host de stream tirou do ar (o site da o 404). `transient` e um
titulo que falhou agora e pode voltar. A distincao importa: antes, uma
demora do host de stream matava metade da biblioteca no cache.

## Log

```bash
journalctl -u cinestream-resolver -f
tail -f /var/log/cinestream/resolver.log
```
