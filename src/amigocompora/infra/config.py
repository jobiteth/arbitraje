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
        return bool(self.endpoints)


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


class Settings(BaseSettings):
    """Configuración efectiva. Inmutable una vez cargada."""

    model_config = SettingsConfigDict(
        env_prefix="AMIGOCOMPORA_",
        env_nested_delimiter="__",
        toml_file=config_file(),
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

    #: Motor a activar por ranura: `{"dex_quotes": "geckoterminal"}`. Si falta
    #: una ranura y hay un único motor instalado para ella, se autoactiva.
    active_engines: dict[str, str] = Field(default_factory=dict)

    #: Direcciones **de sólo lectura** que el usuario quiere observar. La
    #: aplicación no guarda claves privadas y no puede operar con ellas.
    watch_addresses: tuple[str, ...] = ()

    #: Pares que el barrido periódico vigila. Cada uno se cotiza en el motor DEX
    #: activo y, si el diferencial neto supera su umbral, se publica una alerta.
    watch_pairs: tuple[WatchPairSettings, ...] = ()

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
        # Precedencia: argumentos explícitos > entorno > TOML.
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
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
    try:
        return Settings(**overrides)
    except Exception as error:
        raise ConfigError(
            f"configuración inválida en {config_file()}:\n{error}\n\n"
            f"Recuerda: las API keys no van en este fichero, van en el "
            f"administrador de credenciales del sistema."
        ) from error
