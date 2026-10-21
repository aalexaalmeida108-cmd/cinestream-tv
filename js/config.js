// CineStream TV - onde a copia na nuvem busca cada coisa.
//
// Este arquivo e o unico lugar que muda entre as tres copias do site:
// a da caixa (mesma origem), a do GitHub Pages e a da maquina na nuvem.
// O resto do site e ES5 puro e nao sabe de onde veio nada.
//
// CINE_API          base do gateway (o /tv/... e o /tsseg/...).
//                   '' = mesma origem, o caso normal: a pagina e o gateway
//                   sao servidos pela mesma maquina.
//                   'https://tv.exemplo.com' quando a pagina roda no GitHub
//                   Pages e o video sai de um host na nuvem.
//
// CINE_POSTER_MODE  'bundle' = a capa vem junto com o site (posters/).
//                   'api'    = o gateway serve a capa em /poster/<nome>.
//                   A capa que falta no bundle cai no host de imagens.
//
// CINE_CDN_POSTERS  host de imagens, usado quando a capa nao esta no bundle.
//
// CINE_CATALOG_URL  o catalogo publico, no botao verde do controle.

var CINE_API = '';
var CINE_POSTER_MODE = 'bundle';
var CINE_CDN_POSTERS = 'https://image.tmdb.org/t/p/w342/';
var CINE_CATALOG_URL = 'https://aalexaalmeida108-cmd.github.io/cinestream-br-v2/';
