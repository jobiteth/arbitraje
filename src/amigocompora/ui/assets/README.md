# Iconos que viajan con la aplicación

Aquí van los SVG que el paquete lleva dentro —y con él el `.exe`—, en una
carpeta por tipo:

```
assets/networks/   una red por fichero   base.svg, arbitrum-one.svg, …
assets/tokens/     un token por fichero  USDC.svg, ETH.svg, …
assets/brands/     un motor por fichero  uniswap.svg, lifi.svg, …
```

Los nombres de fichero son los **slugs de web3icons**, que es de donde salen:
minúsculas con guiones para redes y marcas, ticker en mayúsculas para tokens.
La traducción desde nuestros nombres (`bsc` → `binance-smart-chain`, `USDC.e` →
`USDC`) vive en `amigocompora/ui/icons.py` y la comparten la aplicación y el
script de descarga.

Para llenarla:

```bash
.venv/Scripts/python.exe tools/fetch_icons.py --dest src/amigocompora/ui/assets
```

`ui/icons.py` mira primero aquí y después en el `assets/` de la raíz del
repositorio, que es el buzón de desarrollo. Dentro de cada raíz manda la carpeta
que se mantiene a mano —`chains/`, `coin/`, `plataform/`— sobre la de web3icons:
una descarga nunca tapa un fichero elegido uno a uno. Sin fichero no se dibuja
ningún logo ajeno: **nunca un icono de relleno**, y el nombre del listado nunca
se sustituye por el logo. Un token o una plataforma sin logo propio enseñan el
genérico —`token.svg`, `plataform.svg`—, que no finge ser una marca; y un fichero
con la extensión cambiada (un PNG con nombre `.svg`) se carga igual, porque manda
el contenido.

Los SVG de web3icons son MIT (© 0xa3k5). Al copiarlos aquí, se conserva su
aviso de licencia.
