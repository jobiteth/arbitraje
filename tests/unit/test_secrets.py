"""Tests del resolutor de credenciales.

El caso que se fija aquí es el que se midió contra el entorno real: una máquina
sin backend de keyring hacía fallar el arranque de `geckoterminal` por una clave
que su manifiesto declara **opcional**. Un almacén caído y una clave ausente no
son lo mismo, y el resolutor tiene que distinguirlo.
"""

from __future__ import annotations

import pytest

from amigocompora.domain.errors import KeyCustodyError, NoWalletError
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.infra.secrets import (
    AUTONOMY_PASSPHRASE_SECRET,
    PRIVATE_KEY_SECRET,
    AutonomyPassphraseProvider,
    InMemorySecretStore,
    MissingSecretError,
    SecretStoreError,
    SpendingKeyProvider,
    app_env_var_name,
    app_secret_key,
    build_config_resolver,
    env_var_name,
    has_placeholders,
    placeholder_names,
    resolve_placeholders,
)

OPTIONAL_ENGINE = EngineManifest(
    engine_id="geckoterminal",
    name="Fuente con clave opcional",
    version="1.0.0",
    kind=EngineKind.DEX_QUOTES,
    summary="Cotiza con o sin API key; con ella sube el límite de peticiones.",
    optional_config=("coingecko_api_key",),
)

REQUIRED_ENGINE = EngineManifest(
    engine_id="claude",
    name="Asistente con clave obligatoria",
    version="1.0.0",
    kind=EngineKind.AI_ADVISOR,
    summary="Sin clave no hay nada que consultar.",
    required_config=("api_key",),
)


class BrokenStore:
    """Almacén que simula un keyring sin backend disponible."""

    __slots__ = ()

    def get(self, key: str) -> str | None:
        raise SecretStoreError(f"no se pudo leer «{key}»: no hay backend de keyring")

    def set(self, key: str, value: str) -> None:
        raise SecretStoreError("no hay backend de keyring")

    def delete(self, key: str) -> None:
        raise SecretStoreError("no hay backend de keyring")


def test_broken_store_does_not_block_engine_with_optional_keys() -> None:
    """El motor arranca: ninguna de sus claves es obligatoria."""
    resolve = build_config_resolver(BrokenStore())
    assert resolve(OPTIONAL_ENGINE) == {}


def test_broken_store_still_allows_env_fallback() -> None:
    """El fallback por entorno se sigue consultando tras fallar el almacén."""
    var = env_var_name(OPTIONAL_ENGINE.engine_id, "coingecko_api_key")
    resolve = build_config_resolver(BrokenStore())
    assert resolve(OPTIONAL_ENGINE) == {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(var, "clave-de-ci")
        assert resolve(OPTIONAL_ENGINE) == {"coingecko_api_key": "clave-de-ci"}


def test_missing_required_key_with_working_store_is_not_a_store_error() -> None:
    """Sin clave obligatoria se devuelve vacío: `validate_config` da el mensaje.

    No se lanza desde aquí porque con un almacén sano la causa es exactamente
    «el usuario no la ha configurado», que es lo que `validate_config` sabe
    explicar mejor.
    """
    resolve = build_config_resolver(InMemorySecretStore())
    assert resolve(REQUIRED_ENGINE) == {}


def test_missing_required_key_with_broken_store_blames_the_store() -> None:
    """Un fallo del sistema no se reporta como un fallo del usuario.

    Si el almacén no responde no se puede afirmar que falte la clave: puede
    estar guardada y ser el keyring el que no contesta. El error tiene que decir
    eso, y nombrar la clave que quedó sin resolver.
    """
    resolve = build_config_resolver(BrokenStore())
    with pytest.raises(SecretStoreError) as caught:
        resolve(REQUIRED_ENGINE)
    message = str(caught.value)
    assert "claude" in message
    assert "api_key" in message
    assert "no está disponible" in message


def test_required_key_resolved_from_env_survives_broken_store() -> None:
    """Con la clave obligatoria en el entorno, el almacén caído es irrelevante."""
    var = env_var_name(REQUIRED_ENGINE.engine_id, "api_key")
    resolve = build_config_resolver(BrokenStore())
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(var, "clave-de-ci")
        assert resolve(REQUIRED_ENGINE) == {"api_key": "clave-de-ci"}


def test_env_fallback_can_be_disabled() -> None:
    var = env_var_name(REQUIRED_ENGINE.engine_id, "api_key")
    resolve = build_config_resolver(BrokenStore(), allow_env_fallback=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(var, "clave-de-ci")
        with pytest.raises(SecretStoreError):
            resolve(REQUIRED_ENGINE)


# --------------------------------------------------------------------------- #
# `SpendingKeyProvider` — la custodia de la clave que firma
# --------------------------------------------------------------------------- #
def test_private_key_comes_from_the_store() -> None:
    store = InMemorySecretStore()
    store.set(app_secret_key(PRIVATE_KEY_SECRET), "0x" + "11" * 32)
    provider = SpendingKeyProvider(store)
    assert provider.require() == "0x" + "11" * 32
    assert provider.available() is True


def test_private_key_does_not_fall_back_to_env_by_default() -> None:
    """La asimetría que justifica todo este tipo.

    Sin backend de keyring, una clave privada en el entorno del proceso la ve
    cualquiera que pueda leer ese entorno — y a diferencia de una clave de API,
    no se rota: se cambia de cartera y con ella los fondos. Por eso el fallback
    viene apagado y hay que pedirlo.
    """
    provider = SpendingKeyProvider(InMemorySecretStore())
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(app_env_var_name(PRIVATE_KEY_SECRET), "0x" + "22" * 32)
        assert provider.available() is False
        with pytest.raises(NoWalletError):
            provider.require()


def test_private_key_falls_back_to_env_when_explicitly_allowed() -> None:
    provider = SpendingKeyProvider(InMemorySecretStore(), allow_env_fallback=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(app_env_var_name(PRIVATE_KEY_SECRET), "0x" + "33" * 32)
        assert provider.require() == "0x" + "33" * 32


def test_missing_key_is_not_a_custody_error() -> None:
    """«No hay clave» y «no la leo de ahí» son mensajes distintos al usuario."""
    provider = SpendingKeyProvider(InMemorySecretStore())
    assert provider.get() is None
    with pytest.raises(NoWalletError) as caught:
        provider.require()
    assert app_secret_key(PRIVATE_KEY_SECRET) in str(caught.value)


def test_broken_store_with_env_fallback_off_raises_custody_error() -> None:
    provider = SpendingKeyProvider(BrokenStore())
    with pytest.raises(KeyCustodyError):
        provider.get()


def test_the_key_is_never_echoed_by_the_provider() -> None:
    """El `repr()` del proveedor no lleva la clave, y sus errores tampoco.

    `require()` **devuelve** la clave —es su trabajo— así que lo que se fija aquí
    es lo otro: que el objeto no la lleve pegada en su representación. Eso es lo
    que acaba en un log cuando alguien registra el contenedor entero para
    depurar, y es el camino por el que una clave se filtra sin que nadie la
    escriba a propósito.
    """
    secret = "0x" + "44" * 32
    store = InMemorySecretStore()
    store.set(app_secret_key(PRIVATE_KEY_SECRET), secret)
    provider = SpendingKeyProvider(store)
    assert secret not in repr(provider)
    assert secret not in str(provider)
    assert secret not in repr(SpendingKeyProvider(InMemorySecretStore()))


def test_the_address_is_derived_without_handing_out_the_key() -> None:
    """Lo que la interfaz necesita mostrar: la dirección, nunca la clave.

    Se usa una clave de desarrollo **publicada** (la primera de Hardhat), cuya
    dirección es conocida de antemano: comprobar la dirección contra un valor
    calculado por el propio código no demostraría nada, porque un error de
    derivación se llevaría por delante las dos partes de la comparación.
    """
    store = InMemorySecretStore()
    store.set(
        app_secret_key(PRIVATE_KEY_SECRET),
        "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    )
    assert SpendingKeyProvider(store).address() == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def test_without_a_wallet_there_is_no_address_and_no_error() -> None:
    """`address()` se llama para pintar la pantalla, así que no puede lanzar."""
    assert SpendingKeyProvider(InMemorySecretStore()).address() is None
    assert SpendingKeyProvider(BrokenStore()).address() is None


# --------------------------------------------------------------------------- #
# `AutonomyPassphraseProvider` — la frase que habilita la ejecución desatendida
# --------------------------------------------------------------------------- #
def test_passphrase_comes_from_the_store() -> None:
    store = InMemorySecretStore()
    store.set(app_secret_key(AUTONOMY_PASSPHRASE_SECRET), "frase-del-operador")
    assert AutonomyPassphraseProvider(store).get() == "frase-del-operador"


def test_passphrase_does_not_fall_back_to_env_by_default() -> None:
    provider = AutonomyPassphraseProvider(InMemorySecretStore())
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(app_env_var_name(AUTONOMY_PASSPHRASE_SECRET), "desde-el-entorno")
        assert provider.get() is None


def test_passphrase_falls_back_to_env_when_explicitly_allowed() -> None:
    provider = AutonomyPassphraseProvider(InMemorySecretStore(), allow_env_fallback=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(app_env_var_name(AUTONOMY_PASSPHRASE_SECRET), "desde-el-entorno")
        assert provider.get() == "desde-el-entorno"


def test_the_store_wins_over_the_environment_for_the_passphrase() -> None:
    """La misma precedencia que la clave: el sitio bueno gana cuando existe."""
    store = InMemorySecretStore()
    store.set(app_secret_key(AUTONOMY_PASSPHRASE_SECRET), "la-del-llavero")
    provider = AutonomyPassphraseProvider(store, allow_env_fallback=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(app_env_var_name(AUTONOMY_PASSPHRASE_SECRET), "la-del-entorno")
        assert provider.get() == "la-del-llavero"


def test_a_broken_store_means_no_autonomy_and_never_raises() -> None:
    """La asimetría con la clave privada, y es deliberada.

    Quedarse sin clave **impide firmar**, así que ahí un almacén roto detiene la
    operación con un `KeyCustodyError` — lo hace
    `test_broken_store_with_env_fallback_off_raises_custody_error`. Quedarse sin
    frase sólo impide firmar *sin preguntar*, así que el fallo cae del lado de
    preguntar: se devuelve `None` y no se revienta el arranque de una aplicación
    que sigue sirviendo para todo lo demás.
    """
    assert AutonomyPassphraseProvider(BrokenStore()).get() is None


def test_a_broken_store_still_honours_the_explicit_env_fallback() -> None:
    """Y decirlo así es lo honesto: el respaldo también cubre el llavero roto.

    Es la misma precedencia que la clave privada —el respaldo rellena la
    ausencia, venga de donde venga—, y es justo el caso que lo justifica: una
    máquina sin backend de keyring es indistinguible de un llavero roto, y sin
    esto la ejecución desatendida no funcionaría en ninguna de las dos.
    """
    provider = AutonomyPassphraseProvider(BrokenStore(), allow_env_fallback=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(app_env_var_name(AUTONOMY_PASSPHRASE_SECRET), "del-entorno")
        assert provider.get() == "del-entorno"


def test_an_empty_passphrase_is_not_autonomy() -> None:
    """Una cadena vacía en el llavero no habilita nada."""
    store = InMemorySecretStore()
    store.set(app_secret_key(AUTONOMY_PASSPHRASE_SECRET), "")
    assert AutonomyPassphraseProvider(store).get() is None


def test_the_passphrase_is_never_echoed_by_the_provider() -> None:
    store = InMemorySecretStore()
    store.set(app_secret_key(AUTONOMY_PASSPHRASE_SECRET), "no-debe-salir")
    provider = AutonomyPassphraseProvider(store)
    assert "no-debe-salir" not in repr(provider)
    assert "no-debe-salir" not in str(provider)


# --------------------------------------------------------------------------- #
# `${MARCADOR}` — para que una clave de RPC no viva en config.toml
# --------------------------------------------------------------------------- #
def test_placeholder_is_replaced_from_the_store() -> None:
    store = InMemorySecretStore()
    store.set(app_secret_key("INFURA_API_KEY"), "abc123")
    url = "https://base-mainnet.infura.io/v3/${INFURA_API_KEY}"
    assert resolve_placeholders(url, store) == "https://base-mainnet.infura.io/v3/abc123"


def test_placeholder_falls_back_to_env() -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(app_env_var_name("INFURA_API_KEY"), "desde-entorno")
        resolved = resolve_placeholders("https://x/${INFURA_API_KEY}", InMemorySecretStore())
    assert resolved == "https://x/desde-entorno"


def test_a_missing_secret_is_an_error_and_not_an_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dejar la URL a medias produce un fallo incomprensible más tarde.

    Si el marcador no se resolviera y quedara `${INFURA_API_KEY}` dentro de la
    URL, el síntoma sería un error de DNS; si se sustituyera por vacío, un 401
    sin explicación. El error tiene que nombrar el secreto y dónde configurarlo.

    Se borra la variable del entorno a propósito: el `.env` del repositorio
    define esa misma clave —es justo la que se usa para el RPC— y sin borrarla el
    marcador se resolvería y la prueba no comprobaría nada.
    """
    monkeypatch.delenv(app_env_var_name("INFURA_API_KEY"), raising=False)
    with pytest.raises(MissingSecretError) as caught:
        resolve_placeholders("https://x/${INFURA_API_KEY}", InMemorySecretStore())
    assert "INFURA_API_KEY" in str(caught.value)
    assert app_env_var_name("INFURA_API_KEY") in str(caught.value)


def test_text_without_placeholders_is_untouched() -> None:
    url = "https://mainnet.example.org/rpc"
    assert resolve_placeholders(url, InMemorySecretStore()) == url
    assert has_placeholders(url) is False


def test_los_nombres_referenciados_se_pueden_leer_sin_resolverlos() -> None:
    """La interfaz necesita el **nombre** para ofrecer la casilla, no el valor.

    Es lo que permite que la pantalla de credenciales pida la clave de un nodo
    sin tener escrita en ninguna parte la lista de proveedores de RPC.
    """
    assert placeholder_names("https://x/${INFURA_API_KEY}") == ("INFURA_API_KEY",)
    assert placeholder_names("https://mainnet.example.org/rpc") == ()


def test_un_nombre_repetido_se_cuenta_una_vez() -> None:
    """Dos marcadores iguales son una credencial, no dos casillas."""
    assert placeholder_names("https://${K}.x/${K}") == ("K",)


def test_los_nombres_salen_en_el_orden_en_que_aparecen() -> None:
    """Para que dos casillas no se intercambien de sitio entre dos arranques."""
    assert placeholder_names("https://${UNO}/${DOS}") == ("UNO", "DOS")


def test_placeholder_in_a_broken_store_does_not_swallow_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un almacén caído no puede convertirse en un secreto inventado."""
    monkeypatch.delenv(app_env_var_name("INFURA_API_KEY"), raising=False)
    with pytest.raises(MissingSecretError):
        resolve_placeholders("https://x/${INFURA_API_KEY}", BrokenStore())
