"""Configuración de la aplicación.

Dos fuentes, por precedencia: variables de entorno `AMIGOCOMPORA_*` y luego
`%APPDATA%/Amigocompora/config.toml`.

**En este fichero no caben secretos, por construcción.** El modelo usa
`extra="forbid"`, así que una clave desconocida —`api_key` incluido— hace
fallar el arranque con un error explícito. Y no existe ningún diccionario de
opciones libre por motor: la configuración sensible de los motores se resuelve
exclusivamente contra el keyring del sistema (ver `infra.secrets`).
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from amigocompora.domain.addresses import is_evm_address, is_solana_address
from amigocompora.domain.chains import CHAINS, ChainSpec, chain
from amigocompora.domain.errors import AmigocomporaError
from amigocompora.domain.execution import (
    ANY_CHAIN,
    GasPolicy,
    GasStrategy,
    TriggerKind,
    parse_amount,
)
from amigocompora.domain.modes import DEFAULT_MODE, OperationMode

APP_NAME: Final = "Amigocompora"

#: Hosts a los que se permite hablar por HTTP sin cifrar. Sólo desarrollo local.
LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})


class ConfigError(AmigocomporaError):
    """La configuración es inválida o no se puede leer."""


def config_dir() -> Path:
    """Directorio de configuración. `%APPDATA%/Amigocompora` en Windows."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    return base / APP_NAME


def config_file() -> Path:
    return config_dir() / "config.toml"


def env_files() -> tuple[Path, ...]:
    """Dónde se busca un `.env`, **del más específico al más general**.

    El orden importa y es el de lectura: el primero que defina un nombre gana.
    Así, el `.env` del directorio de configuración del usuario sobrescribe al del
    repositorio, que es lo que se espera —el del repositorio es una plantilla
    compartida y el del usuario es lo que ese usuario decidió.

    Los dos sitios hacen falta por razones distintas:

    - `~/.config/Amigocompora/.env` es el único que existe cuando la aplicación
      está instalada y se lanza desde el menú de inicio: ahí el directorio de
      trabajo es cualquiera y un `.env` relativo no se encontraría nunca.
    - `.env` en el directorio de trabajo es el caso documentado en
      `.env.example` («copia este fichero a `.env`») y el que usa quien trabaja
      sobre el repositorio.

    Lo que **no** se hace es cargar el `.env` con `SettingsConfigDict.env_file`:
    por ahí las claves desconocidas chocan con `extra="forbid"` y abortan el
    arranque. Esa defensa existe para impedir que un secreto acabe en
    `config.toml`, y aplicarla también al `.env` dejaría al `.env` sin poder
    nombrar credenciales, que es justo para lo que sirve. Se cargan al entorno
    del proceso, donde sólo se leen las variables que corresponden a campos
    declarados y las demás se ignoran en paz — que es el comportamiento que ya
    tienen las variables de entorno reales en CI y en contenedores.
    """
    return (config_dir() / ".env", Path(".env"))


def load_env_files() -> tuple[Path, ...]:
    """Vuelca los `.env` al entorno del proceso y devuelve los que existían.

    Nunca sobrescribe una variable ya presente: el entorno real gana, para que un
    contenedor o un `FOO=bar comando` puedan imponerse sobre lo que haya en
    disco sin tener que editar el fichero.
    """
    from dotenv import dotenv_values

    loaded: list[Path] = []
    for path in env_files():
        if not path.is_file():
            continue
        loaded.append(path)
        for name, value in dotenv_values(path).items():
            if value is not None and name not in os.environ:
                os.environ[name] = value
    return tuple(loaded)


class RpcEndpointSettings(BaseModel):
    """Un endpoint JSON-RPC. `priority` menor = se intenta antes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    label: str = ""
    priority: int = 100

    @field_validator("url")
    @classmethod
    def _require_tls(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"}:
            raise ValueError(f"el endpoint debe ser http(s), llegó «{parts.scheme or value}»")
        if parts.scheme == "http" and (parts.hostname or "") not in LOCAL_HOSTS:
            raise ValueError(
                f"«{parts.hostname}» no es local: usa https. Una API key de RPC viaja "
                f"en la URL y en claro sería visible para cualquier intermediario."
            )
        return value

    @property
    def display_name(self) -> str:
        from amigocompora.infra.logging import safe_url

        return self.label or safe_url(self.url)


class ChainSettings(BaseModel):
    """Los endpoints RPC de una red, en orden de preferencia.

    Lo que **no** lleva: nombre, símbolo nativo ni decimales. Eso vive en
    `domain.chains` y está medido en la fuente; repetirlo aquí permitiría que un
    `config.toml` declarase que ETH tiene 6 decimales, y a partir de ahí todas
    las cifras de esa red serían falsas sin que nada lo señalara. La
    configuración elige **qué** red usar y con qué nodos hablar; qué es esa red
    lo decide el registro.

    Para usar una red que el registro no conozca hay que añadirla al registro
    —una entrada con sus datos medidos—, que es el punto de extensión previsto y
    el único sitio donde esos datos pueden verificarse.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Clave de la red en `domain.chains`: `"ethereum"`, `"base"`, `"solana"`…
    chain: str
    endpoints: tuple[RpcEndpointSettings, ...] = ()
    #: Si además de los endpoints declarados se usan los públicos medidos
    #: (`infra.rpc.defaults`) como respaldo. Encendido por omisión: la regla del
    #: producto es que la red nunca se quede sin nadie a quien preguntar, y el que
    #: un nodo propio se caiga no debe detener la aplicación. Se apaga cuando se
    #: quiere forzar que **sólo** se hable con los nodos declarados.
    include_public_fallbacks: bool = True

    @field_validator("chain")
    @classmethod
    def _known_chain(cls, value: str) -> str:
        key = value.strip().lower()
        if key not in CHAINS:
            raise ValueError(
                f"«{value}» no es una red conocida. Disponibles: {', '.join(sorted(CHAINS))}."
            )
        return key

    @property
    def spec(self) -> ChainSpec:
        """Los datos de la red: nombre, token nativo, decimales, formato."""
        return chain(self.chain)

    @property
    def is_usable(self) -> bool:
        """Si se puede construir un pool para esta red.

        Se pregunta por los endpoints **efectivos** y no por los declarados:
        declarar `chain = "solana"` sin endpoints y sin nodos públicos medidos
        dejaría un pool vacío, y `RpcPool` aborta en ese caso. Mejor decir que la
        red no es usable —lo que la UI puede explicar— que reventar al construir.
        """
        return bool(self.effective_endpoints())

    def effective_endpoints(self) -> tuple[RpcEndpointSettings, ...]:
        """Los endpoints que se usarán: los declarados y, detrás, los públicos.

        Los declarados van primero y conservan su prioridad; los públicos
        entran después con `FALLBACK_PRIORITY`. El orden importa porque
        `RpcPool` ordena por prioridad y sólo usa la latencia para desempatar:
        así un nodo propio se intenta siempre antes que un respaldo, y entre los
        respaldos decide la latencia medida en vivo.
        """
        from amigocompora.infra.rpc.defaults import fallback_endpoints

        if not self.include_public_fallbacks:
            return self.endpoints
        return self.endpoints + tuple(
            RpcEndpointSettings(
                url=endpoint.url,
                label=endpoint.label,
                priority=endpoint.priority,
            )
            for endpoint in fallback_endpoints(self.chain)
        )


class WatchPairSettings(BaseModel):
    """Un par que el barrido periódico vigila, por símbolos del catálogo.

    `amount` es la cantidad en unidades humanas del token base (unidades, no
    raw): `"1"` para 1 WETH. La conversión a `TokenAmount` la hace el
    contenedor, que es quien conoce el catálogo y los decimales medidos.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chain: str
    base: str
    quote: str
    amount: Decimal
    min_net_bps: int = Field(default=10, ge=0)

    @field_validator("chain")
    @classmethod
    def _known_chain(cls, value: str) -> str:
        key = value.strip().lower()
        if key not in CHAINS:
            raise ValueError(
                f"«{value}» no es una red conocida. Disponibles: {', '.join(sorted(CHAINS))}."
            )
        return key

    @field_validator("amount")
    @classmethod
    def _positive_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError(f"la cantidad debe ser positiva, llegó {value}")
        return value


class ExecutionGasSettings(BaseModel):
    """Cuánto está dispuesto a pagar el operador por gas.

    Es una traducción del `[execution.gas]` del TOML, no la política: las reglas
    —que con estrategia fija haya que declarar las dos cifras, que el techo no
    sea menor que la propina— viven en `domain.execution.GasPolicy`, que es
    donde se pueden comprobar sin arrancar la aplicación. Aquí sólo se leen los
    valores y se pasan; repetir las reglas en los dos sitios sería tener dos
    versiones de la misma verdad.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: GasStrategy = GasStrategy.AUTO
    #: Factor sobre el techo calculado. Se declara como texto —`multiplier =
    #: "1.2"`— por la misma razón que los importes: un `float` de por medio
    #: convierte «1.2» en algo que no es exactamente 1.2.
    multiplier: str = "1.2"
    fixed_max_fee_per_gas: int | None = None
    fixed_max_priority_fee_per_gas: int | None = None

    def to_policy(self) -> GasPolicy:
        """La política del dominio, ya validada por sus propias reglas."""
        return GasPolicy(
            strategy=self.strategy,
            multiplier=parse_amount(self.multiplier, field_name="execution.gas.multiplier")
            or Decimal("1.2"),
            fixed_max_fee_per_gas=self.fixed_max_fee_per_gas,
            fixed_max_priority_fee_per_gas=self.fixed_max_priority_fee_per_gas,
        )


class PinnedExecutionSettings(BaseModel):
    """Una operación fija: par, importe y cadencia, sin depender de un diferencial.

    Es el caso «compra 250 USDC de WETH cada hora», que no necesita que el
    mercado ofrezca nada: ocurre cuando toca. Por eso no lleva umbral.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chain: str
    base: str
    quote: str
    amount: Decimal
    every_minutes: int = Field(gt=0)

    @field_validator("chain")
    @classmethod
    def _known_chain(cls, value: str) -> str:
        key = value.strip().lower()
        if key not in CHAINS:
            raise ValueError(
                f"«{value}» no es una red conocida. Disponibles: {', '.join(sorted(CHAINS))}."
            )
        return key

    @field_validator("amount")
    @classmethod
    def _positive_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError(f"la cantidad debe ser positiva, llegó {value}")
        return value


class ExecutionSettings(BaseModel):
    """Qué puede ejecutarse, cuánto y disparado por qué. **Sin secretos.**

    Los dos secretos que hacen posible operar —la clave privada y la frase de
    autonomía— no caben aquí: este fichero usa `extra="forbid"`, así que una
    clave escrita por error no se ignora en silencio, aborta el arranque con un
    mensaje que dice dónde va. Y `Settings` documenta por qué.

    Lo que **sí** vive aquí son los topes. Se declaran en la unidad en la que el
    usuario piensa —el token de referencia del par, la stablecoin—, y no en el
    token que se entrega: «5 000 por operación» tiene que querer decir lo mismo
    para un par de WETH que para uno de un token de 18 decimales que nadie
    conoce.

    `enabled = false` por omisión. Ejecutar es un acto explícito en las tres
    formas: hay que encender esto **y** estar en modo `EJECUCIÓN` **y**, si no
    se quiere confirmar cada operación, tener armada la autonomía con su frase.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    trigger: TriggerKind = TriggerKind.MANUAL

    #: Redes, tokens y motores con los que se puede operar. **Vacío significa
    #: «nada permitido»**, igual que en `ExecutionLimits`: con esto moviendo
    #: dinero, el fallo tiene que caer del lado de no operar. Un motor que sólo
    #: cotiza no debería poder construir lo que se firma, y `allowed_engines` es
    #: lo que lo hace explícito.
    allowed_chains: tuple[str, ...] = ()
    allowed_tokens: tuple[str, ...] = ()
    allowed_engines: tuple[str, ...] = ()

    #: Autoriza leer los secretos de ejecución del entorno cuando el llavero no
    #: los tiene. **Apagado por omisión**, y es la única opción de este bloque
    #: que no habla de dinero sino de custodia: una API key filtrada se rota en
    #: un minuto, una clave privada filtrada vacía la cartera y no hay rotación
    #: que lo arregle. Por eso el respaldo no se activa por descuido —hay que
    #: escribir esta línea— y por eso `SpendingKeyProvider` nace negándose.
    #:
    #: No se exige que `enabled` esté activo para declararlo: encender el
    #: respaldo y la ejecución son dos actos distintos, y obligar al orden
    #: convertiría «voy a prepararlo» en «no puedo dejarlo listo». Con
    #: `enabled = false` no se firma nada, así que la combinación es inerte.
    allow_env_key: bool = False

    #: Topes en unidades del token de referencia. Como **texto** y entre
    #: comillas: `max_quote_per_trade = "5000"`. Un `float` de TOML —`5000.0`—
    #: no se acepta, porque un tope de gasto que se hubiera redondeado en algún
    #: punto es un tope que no se cumple.
    max_quote_per_trade: str | None = None
    max_quote_per_day: str | None = None

    max_executions_per_cycle: int = Field(default=1, ge=0)
    slippage_bps: int = Field(default=50, ge=0, le=10_000)

    gas: ExecutionGasSettings = ExecutionGasSettings()
    pinned: tuple[PinnedExecutionSettings, ...] = ()

    @field_validator("allowed_chains")
    @classmethod
    def _known_chains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # El comodín no es una red: es «cualquiera», y por eso se excluye de la
        # comprobación de nombres —igual que `allowed_tokens = ["*"]`— sin
        # dejar de pasar por la validación del resto de la lista.
        unknown = sorted(
            {item.strip().lower() for item in value} - set(CHAINS) - {ANY_CHAIN}
        )
        if unknown:
            raise ValueError(
                f"redes desconocidas en execution.allowed_chains: {', '.join(unknown)}. "
                f"Disponibles: {', '.join(sorted(CHAINS))}."
            )
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        """Una configuración que no puede funcionar se dice al cargarla, no al operar.

        Los dos casos que se rechazan aquí fallan **en silencio** en tiempo de
        ejecución, que es la peor forma de fallar: con `enabled = true` y las
        listas vacías, cada operación moriría con «no está en la lista blanca
        (ninguno)», un mensaje que describe el síntoma y no la causa; y un
        `trigger = "pinned"` sin operaciones fijas declaradas dejaría una tarea
        programada que no hace nada, para siempre.
        """
        if self.enabled:
            missing = [
                name
                for name, values in (
                    ("allowed_chains", self.allowed_chains),
                    ("allowed_tokens", self.allowed_tokens),
                    ("allowed_engines", self.allowed_engines),
                )
                if not [item for item in values if item.strip()]
            ]
            if missing:
                raise ValueError(
                    f"execution.enabled está activo pero {', '.join(missing)} está vacío, "
                    f"y vacío significa «nada permitido»: no podría ejecutarse ninguna "
                    f"operación. Declara al menos un valor en cada lista, o deja "
                    f"enabled = false."
                )

        pinned_trigger = self.trigger is TriggerKind.PINNED
        if pinned_trigger and not self.pinned:
            raise ValueError(
                'execution.trigger = "pinned" pero no hay ninguna operación fija '
                "declarada: no hay nada que ejecutar. Añade una tabla "
                "[[execution.pinned]] con su par, importe y cadencia."
            )
        if self.pinned and not pinned_trigger:
            raise ValueError(
                f"hay {len(self.pinned)} operación(es) fija(s) declarada(s) pero "
                f'execution.trigger = "{self.trigger}", así que no se ejecutarían '
                f'nunca. Pon trigger = "pinned" o quita las tablas [[execution.pinned]].'
            )
        return self


class UiSettings(BaseModel):
    """Preferencias de **presentación**: cambian lo que se enseña, no lo que se hace.

    Ninguna opción de aquí firma, permite ni desbloquea nada: son vistas. Y
    viven en la configuración —con `extra="forbid"` como el resto del
    modelo— porque decidirlas es del usuario: una clave mal escrita se dice al
    arrancar en vez de perderse en silencio, y no hace falta tocar código para
    cambiar lo que se ve.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Quita de la tabla de rutas las que **no se pueden firmar** —las de
    #: motores que sólo cotizan, como GeckoTerminal o DexScreener—. Apagado por
    #: omisión: la cifra es un precio real y sirve para comparar contra las
    #: rutas firmables, que es para lo que la fila se queda. Quien las tenga
    #: por ruido lo enciende y la tabla se queda con lo que tiene constructor
    #: detrás; la marca ámbar «· sólo cotiza» sigue siendo la defensa de quien
    #: las deja encendidas.
    hide_quote_only_routes: bool = False


class Settings(BaseSettings):
    """Configuración efectiva. Inmutable una vez cargada."""

    model_config = SettingsConfigDict(
        env_prefix="AMIGOCOMPORA_",
        env_nested_delimiter="__",
        # Impide por diseño que un secreto acabe en config.toml: cualquier
        # clave no declarada aquí aborta el arranque.
        extra="forbid",
        frozen=True,
    )

    #: Modo al arrancar. Por defecto el menos capaz; subir es acto del usuario.
    mode: OperationMode = DEFAULT_MODE

    log_level: str = "INFO"
    log_json: bool = False

    request_timeout_seconds: float = Field(default=10.0, gt=0)
    #: TTL de la caché de cotizaciones. Corto: un precio viejo engaña más que
    #: la ausencia de precio.
    quote_cache_ttl_seconds: float = Field(default=5.0, ge=0)
    rpc_failure_threshold: int = Field(default=3, ge=1)
    rpc_cooldown_seconds: float = Field(default=30.0, gt=0)

    chains: tuple[ChainSettings, ...] = ()

    #: Motores de cada ranura: `{"dex_quotes": "geckoterminal"}` deja uno, y
    #: `{"dex_quotes": ["uniswap_v3", "uniswap_v4"]}` **apila** los dos —el
    #: primero manda y los demás siguen cotizando—. Hace falta la lista porque
    #: las ranuras admiten varios motores a la vez y elegir uno no debe borrar
    #: los precios del otro: V3 y V4 son pools distintos con precios distintos, y
    #: verlos juntos es el producto. Si falta una ranura y hay un único motor
    #: instalado para ella, se autoactiva.
    active_engines: dict[str, str | tuple[str, ...]] = Field(default_factory=dict)

    #: Direcciones **de sólo lectura** que el usuario quiere observar. La
    #: aplicación no guarda claves privadas y no puede operar con ellas.
    watch_addresses: tuple[str, ...] = ()

    #: Pares que el barrido periódico vigila. Cada uno se cotiza en el motor DEX
    #: activo y, si el diferencial neto supera su umbral, se publica una alerta.
    watch_pairs: tuple[WatchPairSettings, ...] = ()

    #: Qué se enseña en las tablas. Sólo presentación: ninguna opción de aquí
    #: cambia lo que la aplicación puede hacer.
    ui: UiSettings = UiSettings()

    #: Qué se ejecuta sin preguntar, cuánto y disparado por qué. Apagado por
    #: omisión: firmar y emitir es siempre un acto explícito del usuario.
    execution: ExecutionSettings = ExecutionSettings()

    @field_validator("watch_addresses")
    @classmethod
    def _validate_addresses(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # Se aceptan las dos formas que el producto cubre hoy. La lista no
        # declara la red de cada dirección, así que la validación tiene que ser
        # «una de las dos» y no «EVM»: exigir `0x` dejaría fuera cualquier
        # cartera de Solana, que es una red de primera clase en el resto del
        # sistema.
        invalid = [
            address
            for address in value
            if not (is_evm_address(address) or is_solana_address(address))
        ]
        if invalid:
            raise ValueError(
                f"direcciones mal formadas: {', '.join(invalid)}. Se espera una "
                f"dirección EVM (0x + 40 hex) o un pubkey de Solana en base58."
            )
        return value

    @model_validator(mode="after")
    def _validate_chains(self) -> Self:
        seen = [settings.chain for settings in self.chains]
        duplicated = {key for key in seen if seen.count(key) > 1}
        if duplicated:
            raise ValueError(f"red repetida en la configuración: {', '.join(sorted(duplicated))}")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedencia: argumentos explícitos > entorno del proceso > TOML.
        #
        # No aparece `dotenv_settings`, y es deliberado. Por esa vía las claves
        # del `.env` se comparan contra los campos declarados y una que no
        # corresponda choca con `extra="forbid"` y **aborta el arranque**: un
        # `.env` con `AMIGOCOMPORA_INFURA_API_KEY` dejaba la aplicación sin
        # arrancar. Esa defensa existe para impedir que un secreto acabe en
        # `config.toml`, y aplicarla al `.env` lo dejaría sin poder nombrar
        # credenciales, que es justo para lo que sirve.
        #
        # En su lugar el `.env` se vuelca al entorno del proceso antes de
        # construir (`load_env_files`), así que entra por `env_settings` y hereda
        # su comportamiento: sólo se leen las variables que corresponden a campos
        # declarados y las demás se ignoran — exactamente lo que ya pasa con las
        # variables de entorno reales en CI y en contenedores, y lo que
        # `.env.example` documentaba desde el principio sin que se cumpliera.
        #
        # El fichero TOML se resuelve **aquí**, al construir, y no en el
        # `SettingsConfigDict`. Puesto en el diccionario del modelo, la ruta se
        # calculaba una sola vez al importar el módulo, con lo que `APPDATA` —o
        # cualquier parche de los tests hecho después— no tenía ningún efecto y
        # la configuración leída era siempre la de la máquina que importó el
        # módulo primero. Eso hacía que la suite dependiera de si quien la
        # ejecuta tiene o no un `config.toml` propio: en CI no hay ninguno y
        # pasa; en la máquina de quien lo tiene, no.
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_file()),
        )

    def chain_settings(self, chain_key: str) -> ChainSettings | None:
        """Los endpoints configurados para una red, o `None` si no hay."""
        return next((item for item in self.chains if item.chain == chain_key), None)

    @property
    def usable_chains(self) -> tuple[ChainSettings, ...]:
        return tuple(item for item in self.chains if item.is_usable)


def load_settings(**overrides: Any) -> Settings:
    """Carga la configuración, traduciendo los fallos a `ConfigError`.

    Un `config.toml` inválido debe producir un mensaje que diga qué corregir y
    en qué fichero, no un volcado de pydantic en una ventana de Qt.
    """
    load_env_files()
    try:
        return Settings(**overrides)
    except Exception as error:
        raise ConfigError(
            f"configuración inválida en {config_file()}:\n{error}\n\n"
            f"Recuerda: las API keys no van en este fichero, van en el "
            f"administrador de credenciales del sistema."
        ) from error
