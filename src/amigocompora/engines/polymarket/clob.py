"""Cliente del CLOB de Polymarket: credenciales y publicación de órdenes.

El recinto de Polymarket no liquida con una transacción propia: se le manda una
**orden firmada** y es él quien la ejecuta contra su libro. Eso parte el trabajo
en dos autenticaciones distintas, y las dos hacen falta:

- **Nivel 1 (L1): EIP-712.** Demuestra que quien pide es dueño de la dirección,
  firmando un mensaje con la clave. Sirve para **derivar** las credenciales de
  API. Se firma una vez, y lo que se obtiene son tres cadenas.
- **Nivel 2 (L2): HMAC-SHA256.** Demuestra que quien manda cada petición tiene
  esas credenciales, firmando `marca de tiempo + método + ruta + cuerpo` con el
  secreto. Es lo que viaja en cada petición.

### Las credenciales se derivan, no se guardan

`/auth/derive-api-key` es **idempotente**: para una dirección y un nonce
devuelve siempre las mismas credenciales, y si no existían las crea. Eso permite
pedirlas en el momento de operar y no guardarlas en ningún sitio. Es una
diferencia que importa: un secreto de vida larga en un llavero es un secreto que
alguien puede copiar y usar dentro de un año; uno que se deriva de la clave
privada cada vez no existe cuando no se está operando.

### Este cliente tiene su propio HTTP, y no es un capricho

El `JsonSource` que usan los demás motores no admite **cabeceras por petición**,
y aquí la cabecera *es* la petición: la firma L2 depende de la marca de tiempo,
del método, de la ruta y del cuerpo exacto. Un cliente que no pueda variar las
cabeceras no puede firmar. Se construye con la misma allowlist de hosts que el
resto —sólo `clob.polymarket.com`— así que el mínimo privilegio se mantiene.

### Lo que este módulo no hace

No decide **qué** se firma: eso es `orders.py`, que es puro y se prueba sin red.
Aquí sólo se habla con el recinto. La separación es la que permite comprobar la
construcción de una orden entera sin gastar una petición, y es la razón de que
este fichero sea corto.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_module
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx
import structlog
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.address import to_checksum_address

from amigocompora.domain.errors import ExecutionError, SourceResponseError, SourceUnavailableError
from amigocompora.domain.models import SignedPredictionOrder, SubmittedPredictionOrder
from amigocompora.engines.polymarket.orders import CHAIN_ID, wire_body
from amigocompora.infra.http import build_client

_log = structlog.get_logger(__name__)

HOST: Final = "clob.polymarket.com"
ROOT: Final = f"https://{HOST}"

#: Rutas, medidas del cliente oficial.
CREATE_API_KEY: Final = "/auth/api-key"
DERIVE_API_KEY: Final = "/auth/derive-api-key"
POST_ORDER: Final = "/order"
GET_ORDER: Final = "/data/order/"
GET_ORDER_BOOK: Final = "/book"
GET_BALANCE_ALLOWANCE: Final = "/balance-allowance"

#: Dominio de la firma L1. **No es el mismo** que el de las órdenes: aquél se
#: llama «Polymarket CTF Exchange» y verifica un contrato; éste se llama
#: «ClobAuthDomain», no verifica ningún contrato y sólo existe para que la firma
#: no se pueda reutilizar en otro sitio. Si los dos dominios coincidieran, una
#: firma de autenticación podría valer como firma de una orden.
AUTH_DOMAIN_NAME: Final = "ClobAuthDomain"
AUTH_DOMAIN_VERSION: Final = "1"
AUTH_MESSAGE: Final = "This message attests that I control the given wallet"

AUTH_TYPES: Final[dict[str, list[dict[str, str]]]] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "ClobAuth": [
        {"name": "address", "type": "address"},
        {"name": "timestamp", "type": "string"},
        {"name": "nonce", "type": "uint256"},
        {"name": "message", "type": "string"},
    ],
}

#: Cabeceras del protocolo. Los nombres son literales del recinto.
POLY_ADDRESS: Final = "POLY_ADDRESS"
POLY_SIGNATURE: Final = "POLY_SIGNATURE"
POLY_TIMESTAMP: Final = "POLY_TIMESTAMP"
POLY_NONCE: Final = "POLY_NONCE"
POLY_API_KEY: Final = "POLY_API_KEY"
# El analizador ve la palabra «passphrase» y supone que aquí hay una credencial
# escrita a mano. Es al revés: esto es el **nombre** de una cabecera HTTP, y su
# valor sale de `ClobCredentials`, que no se registra ni se imprime.
POLY_PASSPHRASE: Final = "POLY_PASSPHRASE"  # noqa: S105


@dataclass(frozen=True, slots=True)
class ClobCredentials:
    """Las tres cadenas del nivel 2.

    No se registran ni se imprimen: `__repr__` está sustituido a propósito para
    que un `_log.info("...", creds=creds)` accidental —o un volcado de una
    excepción que las lleve dentro— no escriba el secreto en un fichero de log,
    que es el sitio del que más difícil es borrarlo.
    """

    api_key: str
    api_secret: str
    api_passphrase: str

    def __repr__(self) -> str:
        return f"ClobCredentials(api_key={self.api_key[:6]}…, secreto oculto)"


class ClobClient:
    """Habla con el CLOB. Se construye con una clave y no la guarda como texto.

    Se le pasa la clave privada al construir porque **derivar las credenciales es
    una firma L1**, y hacerlo al vuelo en cada petición costaría una firma por
    llamada. Aun así no se cachea: se guarda sólo mientras vive el objeto, que es
    el ámbito de una operación.
    """

    __slots__ = ("_client", "_creds", "_key", "_signer")

    def __init__(
        self,
        private_key: str,
        *,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = private_key
        self._signer: str = Account.from_key(private_key).address
        self._creds: ClobCredentials | None = None
        # El cliente se puede inyectar para probar sin red. Cuando no se inyecta,
        # se construye con la allowlist del host del recinto y sin ninguna otra
        # salida: este objeto sólo puede hablar con Polymarket.
        self._client = client or build_client(
            allowed_hosts=(HOST,), timeout_seconds=timeout_seconds
        )

    @property
    def signer(self) -> str:
        """La dirección que firma. Pública, y es lo único que se puede enseñar."""
        return self._signer

    @property
    def credentials(self) -> ClobCredentials | None:
        return self._creds

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception as error:
            _log.debug("clob.close_failed", reason=str(error))

    # ----------------------------------------------------------------- #
    # Nivel 1: derivar credenciales
    # ----------------------------------------------------------------- #
    def auth_headers_l1(self, *, nonce: int = 0, timestamp: int | None = None) -> dict[str, str]:
        """Las cabeceras L1, con la firma EIP-712 del mensaje de autenticación.

        Se expone como método público porque es lo que permite **comprobar** el
        camino sin publicar nada: se pueden pedir las cabeceras y contrastarlas
        contra el recinto en una llamada que no mueve dinero.
        """
        marca = int(timestamp if timestamp is not None else time.time())
        mensaje = {
            "types": AUTH_TYPES,
            "primaryType": "ClobAuth",
            "domain": {
                "name": AUTH_DOMAIN_NAME,
                "version": AUTH_DOMAIN_VERSION,
                "chainId": CHAIN_ID,
            },
            "message": {
                "address": to_checksum_address(self._signer),
                "timestamp": str(marca),
                "nonce": nonce,
                "message": AUTH_MESSAGE,
            },
        }
        firmado = Account.sign_message(
            encode_typed_data(full_message=mensaje), self._key
        )
        return {
            POLY_ADDRESS: self._signer,
            POLY_SIGNATURE: "0x" + firmado.signature.hex().removeprefix("0x"),
            POLY_TIMESTAMP: str(marca),
            POLY_NONCE: str(nonce),
        }

    async def derive_credentials(self, *, nonce: int = 0) -> ClobCredentials:
        """Deriva (o crea) las credenciales de nivel 2. Idempotente.

        Se intenta primero **derivar**, que no crea nada, y sólo si el recinto
        dice que no existen se crean. El orden importa: crear primero dejaría una
        credencial nueva en cada ejecución, y una credencial que se crea sin
        necesidad es una credencial más que puede filtrarse.
        """
        if self._creds is not None:
            return self._creds

        headers = self.auth_headers_l1(nonce=nonce)
        try:
            payload = await self._request("GET", DERIVE_API_KEY, headers=headers)
        except SourceResponseError as error:
            _log.info("clob.deriving_failed_creating", reason=str(error)[:200])
            payload = await self._request("POST", CREATE_API_KEY, headers=headers)

        creds = self._parse_credentials(payload)
        self._creds = creds
        _log.info("clob.credentials_ready", signer=self._signer, api_key=creds.api_key[:8] + "…")
        return creds

    @staticmethod
    def _parse_credentials(payload: Any) -> ClobCredentials:
        """Traduce la respuesta, sin dejar pasar una credencial incompleta.

        Se comprueban las tres cadenas y no sólo que la respuesta sea un objeto:
        una credencial a la que le falte la frase produce una petición que el
        recinto rechaza con «no autorizado», que es el mensaje que menos ayuda a
        entender que lo que falta es un campo de la respuesta.
        """
        if not isinstance(payload, Mapping):
            raise SourceResponseError(
                f"el recinto devolvió {type(payload).__name__} donde se esperaban "
                f"las credenciales."
            )
        try:
            creds = ClobCredentials(
                api_key=str(payload["apiKey"]),
                api_secret=str(payload["secret"]),
                api_passphrase=str(payload["passphrase"]),
            )
        except KeyError as error:
            raise SourceResponseError(
                f"las credenciales del recinto llegaron incompletas: falta {error}. "
                f"Sin las tres no se puede firmar una petición de nivel 2."
            ) from error
        if not (creds.api_key and creds.api_secret and creds.api_passphrase):
            raise SourceResponseError("el recinto devolvió alguna credencial vacía.")
        return creds

    # ----------------------------------------------------------------- #
    # Nivel 2: firmar cada petición
    # ----------------------------------------------------------------- #
    def auth_headers_l2(
        self,
        creds: ClobCredentials,
        *,
        method: str,
        path: str,
        body: str = "",
        timestamp: int | None = None,
    ) -> dict[str, str]:
        """Las cabeceras L2, con la firma HMAC de esta petición concreta.

        El mensaje firmado es la concatenación **exacta** de marca de tiempo,
        método en mayúsculas, ruta y cuerpo serializado. Cualquier diferencia
        —un espacio de más, unas comillas simples en vez de dobles, el cuerpo
        reconstruido con otro orden de claves— produce una firma distinta y el
        recinto responde «no autorizado» sin decir qué cambió. Por eso el cuerpo
        llega aquí ya serializado y no como diccionario.
        """
        marca = int(timestamp if timestamp is not None else time.time())
        mensaje = f"{marca}{method.upper()}{path}{body}"
        # El secreto viaja en base64 URL-safe y se decodifica para usarlo como
        # clave del HMAC: se firma con los bytes, no con el texto en base64. Usar
        # el texto tal cual daría una firma válida en apariencia y rechazada
        # siempre por el recinto.
        try:
            clave = base64.urlsafe_b64decode(creds.api_secret)
        except Exception as error:
            raise ExecutionError(
                "el secreto que devolvió el recinto no es base64 válido: no se "
                "puede firmar la petición."
            ) from error
        firma = base64.urlsafe_b64encode(
            hmac_module.new(clave, mensaje.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        return {
            POLY_ADDRESS: self._signer,
            POLY_SIGNATURE: firma,
            POLY_TIMESTAMP: str(marca),
            POLY_API_KEY: creds.api_key,
            POLY_PASSPHRASE: creds.api_passphrase,
        }

    # ----------------------------------------------------------------- #
    # Operaciones
    # ----------------------------------------------------------------- #
    async def submit_order(self, signed: SignedPredictionOrder) -> SubmittedPredictionOrder:
        """Publica una orden firmada y devuelve lo que contestó el recinto.

        El identificador lo asigna **el recinto**, y es lo que se anota: dar por
        hecho que coincide con el resumen que se firmó sería afirmar algo que no
        se ha medido, y el asiento del registro tiene que poder contrastarse
        contra lo que el recinto guardó.

        El **estado** se devuelve sin traducir y sin interpretar. `live` es una
        orden en el libro y `matched` una ya cruzada; cuál de las dos cosas
        ocurrió no lo decide esta capa, y quedarse sólo con el identificador
        obligaría a ir a preguntar más tarde algo que la respuesta ya traía.
        """
        creds = await self.derive_credentials()
        cuerpo = wire_body(signed, owner=creds.api_key)
        headers = self.auth_headers_l2(creds, method="POST", path=POST_ORDER, body=cuerpo)
        headers["Content-Type"] = "application/json"

        payload = await self._request(
            "POST", POST_ORDER, headers=headers, body=cuerpo
        )
        if not isinstance(payload, Mapping):
            raise SourceResponseError(
                f"el recinto devolvió {type(payload).__name__} al publicar la orden."
            )
        if payload.get("success") is False:
            raise ExecutionError(
                f"el recinto rechazó la orden: {payload.get('errorMsg') or payload}"
            )
        order_id = str(payload.get("orderID") or payload.get("orderId") or "")
        if not order_id:
            raise SourceResponseError(
                f"el recinto aceptó la orden pero no devolvió su identificador: {payload}"
            )
        # Un estado ausente no se inventa: se dice que no consta, que es la
        # verdad y es lo que impide que un asiento afirme algo que nadie dijo.
        status = str(payload.get("status") or "sin estado declarado")
        _log.info(
            "clob.order_posted",
            order_id=order_id,
            status=status,
            market=signed.order.market_id,
            outcome=signed.order.outcome_label,
            side=signed.order.side.value,
            price=str(signed.order.price),
            size=str(signed.order.size),
        )
        return SubmittedPredictionOrder(order_id=order_id, status=status)

    async def balance_allowance(
        self, *, token_id: str = "", signature_type: int = 0
    ) -> Mapping[str, Any]:
        """Saldo y permiso que el recinto ve para esta cartera.

        Es la comprobación que dice si una orden se puede liquidar **antes** de
        publicarla. El saldo on-chain y el que el recinto ve pueden diferir —el
        recinto sólo cuenta el permiso concedido a su contrato—, y preguntarle a
        él es la única forma de saber qué va a aceptar.
        """
        creds = await self.derive_credentials()
        params = f"?asset_type=COLLATERAL&signature_type={signature_type}"
        if token_id:
            params += f"&token_id={token_id}"
        headers = self.auth_headers_l2(
            creds, method="GET", path=GET_BALANCE_ALLOWANCE
        )
        payload = await self._request(
            "GET", GET_BALANCE_ALLOWANCE, headers=headers, params=params
        )
        if not isinstance(payload, Mapping):
            raise SourceResponseError(
                f"el recinto devolvió {type(payload).__name__} al pedir el saldo."
            )
        return payload

    async def open_order(self, order_id: str) -> Mapping[str, Any]:
        """Una orden propia por su identificador. Para comprobar que existe."""
        creds = await self.derive_credentials()
        path = f"{GET_ORDER}{order_id}"
        headers = self.auth_headers_l2(creds, method="GET", path=path)
        payload = await self._request("GET", path, headers=headers)
        if not isinstance(payload, Mapping):
            raise SourceResponseError(
                f"el recinto devolvió {type(payload).__name__} al pedir la orden."
            )
        return payload

    async def book(self, token_id: str) -> Mapping[str, Any]:
        """El libro público. Sin credenciales: es lectura abierta."""
        payload = await self._request(
            "GET", GET_ORDER_BOOK, params=f"?token_id={token_id}"
        )
        if not isinstance(payload, Mapping):
            raise SourceResponseError(
                f"el recinto devolvió {type(payload).__name__} al pedir el libro."
            )
        return payload

    # ----------------------------------------------------------------- #
    # Transporte
    # ----------------------------------------------------------------- #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: str | None = None,
        params: str = "",
    ) -> Any:
        """Una petición, con los fallos traducidos a errores del dominio.

        Un 4xx del recinto **no** es un fallo de red: es una respuesta, y su
        cuerpo dice qué pasó —saldo insuficiente, orden rechazada, firma mal—.
        Se incluye recortado en el mensaje porque sin él quien depura se queda
        con un número y una suposición.
        """
        url = f"{ROOT}{path}{params}"
        try:
            respuesta = await self._client.request(
                method,
                url,
                headers=dict(headers or {}),
                content=body.encode("utf-8") if body is not None else None,
            )
        except httpx.HTTPError as error:
            raise SourceUnavailableError(
                f"no se pudo hablar con el recinto de Polymarket: "
                f"{type(error).__name__}. Comprueba la conexión y vuelve a intentarlo."
            ) from error

        if respuesta.status_code >= 500:
            raise SourceUnavailableError(
                f"el recinto de Polymarket respondió {respuesta.status_code}: "
                f"está caído o saturado. Reintentar más tarde tiene sentido."
            )
        if respuesta.status_code >= 400:
            raise SourceResponseError(
                f"el recinto de Polymarket respondió {respuesta.status_code} a "
                f"{method} {path}. Respuesta: {_snippet(respuesta)}"
            )
        if not respuesta.content:
            return {}
        try:
            return respuesta.json()
        except ValueError as error:
            raise SourceResponseError(
                f"el recinto no devolvió JSON válido: {error}"
            ) from error


def _snippet(respuesta: httpx.Response, *, limit: int = 300) -> str:
    """El cuerpo de una respuesta de error, acotado y en una línea.

    Acotado porque esto acaba en un `QLabel` y en un log; en una línea porque un
    JSON con saltos de línea dentro de un mensaje de error descoloca cualquier
    registro que se lea de un vistazo.
    """
    try:
        texto = " ".join(respuesta.text.split())
    except Exception:
        return ""
    return f"{texto[:limit]}{'…' if len(texto) > limit else ''}"


def new_nonce() -> int:
    """Un nonce para las credenciales L1, del azar del sistema.

    No se usa el reloj: dos ejecuciones en el mismo segundo derivarían
    credenciales distintas sin querer, y el objetivo es que las **mismas**
    credenciales se deriven siempre, para no acumular secretos.
    """
    return secrets.randbelow(2**31)
