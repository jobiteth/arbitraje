"""La pestaña de motores: las credenciales, y lo que de ellas se enseña.

Esta pestaña no existía en las pruebas y es la que custodia la clave que firma.
Lo que se comprueba aquí es lo que se puede afirmar de un secreto sin repetirlo:
que se guarda donde tiene que guardarse, que el campo se vacía **siempre**, que
el estado dice «configurada» y no el valor, y que una máquina sin llavero —que es
el caso de cualquier servidor, y el de esta aplicación desplegada— sigue diciendo
al usuario dónde poner la credencial.

Esa última es la que motivó la prueba: el llavero que no responde sustituía la
ayuda por el error, y con ella se perdía el nombre de la variable de entorno, que
es lo único que funciona en una máquina así. El propio mensaje del botón de
guardar manda a leer esa ayuda («usa la variable de entorno que se indica en su
descripción»), así que sin ella la frase señalaba a un texto que ya no estaba.

Ninguna prueba de este fichero firma, emite ni sale a la red.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

from amigocompora.app.container import Container, build_container
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    AUTONOMY_PASSPHRASE_SECRET,
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    SecretStoreError,
    app_env_var_name,
    app_secret_key,
)
from amigocompora.ui.pages.engines import EnginesPage

#: Clave de desarrollo **publicada** (la primera de Hardhat). No es un secreto:
#: que sea conocida es justo lo que la hace útil aquí.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

#: La credencial de la clave privada, tal y como se llama en el almacén.
CLAVE_PRIVADA = app_secret_key(PRIVATE_KEY_SECRET)


class SinLlavero:
    """Un almacén que se comporta como un servidor sin Secret Service.

    No es un caso raro: es **esta** máquina, y cualquier contenedor. La pestaña
    tiene que seguir siendo útil en ella, no sólo no romperse.
    """

    __slots__ = ()

    def get(self, key: str) -> str | None:
        raise SecretStoreError(f"no se pudo leer «{key}»: no hay backend de keyring")

    def set(self, key: str, value: str) -> None:
        raise SecretStoreError("no hay backend de keyring")

    def delete(self, key: str) -> None:
        raise SecretStoreError("no hay backend de keyring")


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@asynccontextmanager
async def _pagina(
    store: object,
    *,
    ajustes: dict[str, object] | None = None,
) -> AsyncIterator[tuple[Container, EnginesPage]]:
    """La pestaña montada sobre un contenedor de verdad.

    El contenedor es el de verdad y no un doble porque lo que se mide incluye
    **dónde acaba guardándose** la credencial, y eso lo decide el contenedor.

    `ajustes` se pasa explícito aunque el `conftest` ya deje la configuración en
    un directorio temporal y vacío: lo que decide si el respaldo del entorno está
    encendido es una línea de configuración, y una prueba sobre custodia no debe
    depender de que el valor por omisión siga siendo el que es hoy.
    """
    container = await build_container(
        Settings.model_validate(ajustes if ajustes is not None else _APAGADO),
        secret_store=store,  # type: ignore[arg-type]
        configure_logs=False,
    )
    try:
        yield container, EnginesPage(container)
    finally:
        await container.aclose()


#: Sin respaldo por entorno: el caso por omisión, dicho en voz alta.
_APAGADO: dict[str, object] = {"execution": {"allow_env_key": False}}


def _textos(pagina: EnginesPage) -> list[str]:
    """Todo el texto visible de la pestaña: rótulos y campos.

    La ayuda emergente no entra: es lo que se lee a propósito al pasar por encima,
    y es donde vive el nombre de la variable de entorno.
    """
    textos = [item.text() for item in pagina.findChildren(QLabel)]
    textos.extend(campo.text() for campo in pagina.findChildren(QLineEdit))
    return textos


async def test_la_clave_privada_tiene_su_sitio_en_la_pantalla() -> None:
    """La credencial que firma se pide aquí, y con su nombre del almacén."""
    async with _pagina(InMemorySecretStore()) as (_, pagina):
        assert CLAVE_PRIVADA in pagina._secret_fields
        assert app_secret_key(AUTONOMY_PASSPHRASE_SECRET) in pagina._secret_fields
        assert pagina._secret_fields[CLAVE_PRIVADA].echoMode() == QLineEdit.EchoMode.Password


async def test_el_estado_dice_si_esta_y_nunca_cual_es() -> None:
    """Con la clave puesta, el rótulo lo afirma sin dejarla ver por ningún lado."""
    store = InMemorySecretStore()
    store.set(CLAVE_PRIVADA, CLAVE_DE_DESARROLLO)
    async with _pagina(store) as (_, pagina):
        assert pagina._secret_states[CLAVE_PRIVADA].text() == "configurada"
        assert pagina._secret_fields[CLAVE_PRIVADA].text() == ""
        assert not any(CLAVE_DE_DESARROLLO in texto for texto in _textos(pagina))
        # Ni un trozo: la mitad de una clave privada sigue siendo media clave.
        assert not any(CLAVE_DE_DESARROLLO[2:20] in texto for texto in _textos(pagina))


async def test_sin_configurar_es_distinto_de_configurada() -> None:
    """Lo que no está se dice que no está, para no prometer una cartera que falta."""
    async with _pagina(InMemorySecretStore()) as (_, pagina):
        assert pagina._secret_states[CLAVE_PRIVADA].text() == "sin configurar"


async def test_guardar_escribe_en_el_llavero_y_vacia_el_campo() -> None:
    """El campo se vacía al guardar: una clave escrita en un widget es una clave en pantalla."""
    store = InMemorySecretStore()
    async with _pagina(store) as (_, pagina):
        avisos: list[int] = []
        pagina.credentials_changed.connect(lambda: avisos.append(1))
        pagina._secret_fields[CLAVE_PRIVADA].setText(CLAVE_DE_DESARROLLO)
        pagina._on_save_secret(CLAVE_PRIVADA)

        assert store.get(CLAVE_PRIVADA) == CLAVE_DE_DESARROLLO
        assert pagina._secret_fields[CLAVE_PRIVADA].text() == ""
        assert pagina._secret_states[CLAVE_PRIVADA].text() == "configurada"
        assert avisos == [1], "las otras pestañas tienen que enterarse de que ya se puede firmar"
        assert CLAVE_DE_DESARROLLO not in pagina._status.text()
        assert not any(CLAVE_DE_DESARROLLO in texto for texto in _textos(pagina))


async def test_guardar_un_campo_vacio_no_escribe_nada() -> None:
    """Un guardar en blanco no puede borrar la credencial que ya estaba."""
    store = InMemorySecretStore()
    store.set(CLAVE_PRIVADA, CLAVE_DE_DESARROLLO)
    async with _pagina(store) as (_, pagina):
        pagina._on_save_secret(CLAVE_PRIVADA)
        assert store.get(CLAVE_PRIVADA) == CLAVE_DE_DESARROLLO
        assert "vacío" in pagina._status.text()


async def test_borrar_quita_la_credencial_y_avisa() -> None:
    """Borrar deja la pestaña diciendo que ya no está, y avisa a las demás."""
    store = InMemorySecretStore()
    store.set(CLAVE_PRIVADA, CLAVE_DE_DESARROLLO)
    async with _pagina(store) as (_, pagina):
        avisos: list[int] = []
        pagina.credentials_changed.connect(lambda: avisos.append(1))
        pagina._on_forget_secret(CLAVE_PRIVADA)

        assert store.get(CLAVE_PRIVADA) is None
        assert pagina._secret_states[CLAVE_PRIVADA].text() == "sin configurar"
        assert avisos == [1]


async def test_sin_llavero_la_ayuda_sigue_diciendo_donde_vive() -> None:
    """El fallo del llavero se suma a la ayuda; no la sustituye.

    Es la regresión: con el error encima se perdía el nombre de la variable de
    entorno, que es la única vía que queda en una máquina sin llavero —y el
    mensaje de guardar remite a esa ayuda para encontrarla.
    """
    async with _pagina(SinLlavero()) as (_, pagina):
        estado = pagina._secret_states[CLAVE_PRIVADA]
        assert estado.text() == "el llavero no responde"
        ayuda = estado.toolTip()
        assert "keyring" in ayuda, "el diagnóstico sigue estando"
        assert app_env_var_name(PRIVATE_KEY_SECRET) in ayuda, (
            "la salida —la variable de entorno— tiene que seguir a la vista"
        )


async def test_sin_llavero_guardar_lo_dice_sin_romper_la_pantalla() -> None:
    """Guardar contra un llavero roto se cuenta, y el campo se vacía igual."""
    async with _pagina(SinLlavero()) as (_, pagina):
        pagina._secret_fields[CLAVE_PRIVADA].setText(CLAVE_DE_DESARROLLO)
        pagina._on_save_secret(CLAVE_PRIVADA)
        assert "No se pudo guardar" in pagina._status.text()
        assert pagina._secret_fields[CLAVE_PRIVADA].text() == ""
        assert CLAVE_DE_DESARROLLO not in pagina._status.text()


# --------------------------------------------------------------------------- #
# Cuando el llavero no está, pero la credencial sí
# --------------------------------------------------------------------------- #
async def test_si_el_entorno_la_sirve_se_dice_que_esta_y_de_donde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un llavero roto no siempre significa «no puedo firmar».

    Con `allow_env_key` y la variable puesta, la aplicación **sí** firma —el
    proveedor la lee al firmar—, así que dejar el rótulo en «el llavero no
    responde» era cierto y engañoso a la vez: la conclusión que saca quien lo lee
    es que no hay con qué operar. Se dice que está y de dónde sale, que es lo que
    permite juzgar el riesgo; el valor sigue sin aparecer.
    """
    variable = app_env_var_name(PRIVATE_KEY_SECRET)
    monkeypatch.setenv(variable, CLAVE_DE_DESARROLLO)

    async with _pagina(
        SinLlavero(), ajustes={"execution": {"allow_env_key": True}}
    ) as (_, pagina):
        estado = pagina._secret_states[CLAVE_PRIVADA]

        assert estado.text() == "configurada por el entorno"
        assert variable in estado.toolTip()
        assert CLAVE_DE_DESARROLLO not in estado.toolTip()
        assert not any(CLAVE_DE_DESARROLLO in texto for texto in _textos(pagina))


async def test_con_el_respaldo_encendido_pero_sin_variable_no_se_promete_nada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El contrapunto, y el que evita el mensaje bonito pero falso.

    Autorizar el respaldo no es tener la credencial: sin la variable puesta no hay
    clave, y decir «configurada» porque la puerta está abierta sería prometer una
    firma que va a fallar.
    """
    monkeypatch.delenv(app_env_var_name(PRIVATE_KEY_SECRET), raising=False)
    monkeypatch.delenv(app_env_var_name(AUTONOMY_PASSPHRASE_SECRET), raising=False)

    async with _pagina(
        SinLlavero(), ajustes={"execution": {"allow_env_key": True}}
    ) as (_, pagina):
        assert pagina._secret_states[CLAVE_PRIVADA].text() == "el llavero no responde"


async def test_con_el_respaldo_apagado_la_variable_no_se_ofrece_a_secas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y si el respaldo está apagado, la variable se nombra **con su condición**.

    Es la promesa de custodia: `allow_env_key` apagado significa que una clave
    privada en el entorno **no** se usa. Nombrarla a secas —como se hacía— mandaba
    a un servidor sin llavero a poner una variable que nadie iba a leer, y el
    botón de guardar remite a esa misma ayuda para encontrarla. El `require()` de
    al lado ya se cuidaba de no hacerlo; la pantalla no.
    """
    monkeypatch.setenv(app_env_var_name(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)

    async with _pagina(SinLlavero()) as (_, pagina):
        estado = pagina._secret_states[CLAVE_PRIVADA]
        ayuda = estado.toolTip()

        assert estado.text() == "el llavero no responde"
        assert app_env_var_name(PRIVATE_KEY_SECRET) in ayuda, "el nombre sigue estando"
        assert "allow_env_key" in ayuda, (
            "y con él, qué hace falta para que esa variable sirva de algo"
        )


async def test_con_el_respaldo_encendido_la_variable_se_ofrece_sin_condiciones() -> None:
    """El contrapunto: si la puerta está abierta, la variable es una vía de verdad.

    Condicionar la frase cuando el respaldo **sí** está encendido sería el error
    simétrico: mandaría a leer una configuración que ya está puesta.
    """
    async with _pagina(
        InMemorySecretStore(), ajustes={"execution": {"allow_env_key": True}}
    ) as (_, pagina):
        ayuda = pagina._secret_states[CLAVE_PRIVADA].toolTip()
        assert app_env_var_name(PRIVATE_KEY_SECRET) in ayuda
        assert "allow_env_key" not in ayuda


async def test_una_credencial_de_motor_nombra_su_variable_sin_condiciones() -> None:
    """Las de motor no llevan la condición: ésas sí se leen del entorno siempre.

    El registro de motores resuelve sus claves con el respaldo encendido por
    omisión —es la única vía en un despliegue sin llavero y no firma nada—, así
    que condicionarlas sería contar un requisito que no existe.
    """
    async with _pagina(SinLlavero()) as (container, pagina):
        de_motor = [k for k in pagina._secret_notes if not k.startswith("app:")]
        assert de_motor, "el catálogo trae motores con credenciales declaradas"
        assert all("allow_env_key" not in pagina._secret_notes[k] for k in de_motor)
        assert container.settings.execution.allow_env_key is False


# --------------------------------------------------------------------------- #
# Las claves de los nodos RPC
# --------------------------------------------------------------------------- #
#: Configuración con un nodo propio cuya clave viaja dentro de la URL. Es el caso
#: de Infura, de Alchemy y de cualquier proveedor de RPC: la clave no es una
#: credencial de motor y no tenía ninguna casilla donde escribirse.
_CON_NODO_PROPIO: dict[str, object] = {
    "execution": {"allow_env_key": False},
    "chains": [
        {
            "chain": "ethereum",
            "endpoints": [
                {
                    "url": "https://mainnet.infura.io/v3/${INFURA_API_KEY}",
                    "label": "infura",
                    "priority": 10,
                }
            ],
        }
    ],
}


def test_los_nombres_de_un_endpoint_salen_de_la_configuracion() -> None:
    """La lista de proveedores no está escrita en la interfaz, se lee del fichero.

    Es la misma regla que ya cumplen las credenciales de motor, que salen del
    manifiesto: escribir «Infura» en la pantalla habría dejado fuera a Alchemy, a
    QuickNode y a cualquier nodo propio, que usan este mismo mecanismo.
    """
    from amigocompora.ui.pages.engines import rpc_secret_usage

    uso = rpc_secret_usage(Settings.model_validate(_CON_NODO_PROPIO))

    assert list(uso) == ["INFURA_API_KEY"]
    assert uso["INFURA_API_KEY"] == ("el nodo «infura» de ethereum",)


def test_un_nodo_sin_marcador_no_pide_ninguna_credencial() -> None:
    """Un nodo público no lleva clave: ofrecerle una casilla sería inventar un paso."""
    from amigocompora.ui.pages.engines import rpc_secret_usage

    ajustes = Settings.model_validate(
        {"chains": [{"chain": "ethereum", "endpoints": [{"url": "https://rpc.libre/eth"}]}]}
    )
    assert rpc_secret_usage(ajustes) == {}


async def test_la_clave_del_nodo_tiene_su_casilla() -> None:
    """Y la casilla dice en qué nodo se usa, que es lo que se rompe si falta."""
    async with _pagina(InMemorySecretStore(), ajustes=_CON_NODO_PROPIO) as (_, pagina):
        clave = app_secret_key("INFURA_API_KEY")

        assert clave in pagina._secret_fields, "la clave del nodo se puede escribir"
        assert pagina._secret_states[clave].text() == "sin configurar"
        assert "infura" in pagina._secret_notes[clave]


async def test_la_clave_del_nodo_no_arrastra_la_condicion_de_la_firma() -> None:
    """Su variable de entorno se lee siempre: la resuelve el contenedor al arrancar.

    `allow_env_key` gobierna a los proveedores que firman, no a los marcadores de
    `config.toml`. Contar aquí ese requisito mandaría a encender una opción que no
    tiene nada que ver con esta credencial.
    """
    async with _pagina(InMemorySecretStore(), ajustes=_CON_NODO_PROPIO) as (container, pagina):
        nota = pagina._secret_notes[app_secret_key("INFURA_API_KEY")]

        assert app_env_var_name("INFURA_API_KEY") in nota
        assert "allow_env_key" not in nota
        assert container.settings.execution.allow_env_key is False


async def test_se_puede_guardar_una_clave_que_el_fichero_aun_no_menciona() -> None:
    """El huevo y la gallina: la clave se guarda antes de que la URL la nombre.

    Sin esto, la casilla sólo aparece cuando `config.toml` ya tiene el marcador, y
    el marcador no se puede probar hasta que la clave está guardada.
    """
    async with _pagina(InMemorySecretStore()) as (container, pagina):
        pagina._custom_name.setText("ALCHEMY_API_KEY")
        pagina._custom_value.setText("clave-de-prueba")
        pagina._on_save_custom()

        clave = app_secret_key("ALCHEMY_API_KEY")
        assert container.secrets.get(clave) == "clave-de-prueba"
        assert pagina._secret_states[clave].text() == "configurada"
        assert pagina._custom_value.text() == "", "el valor no se queda en la pantalla"


async def test_guardar_una_clave_sin_uso_dice_que_nadie_la_usa_todavia() -> None:
    """Guardada no es en uso, y la diferencia tiene que leerse.

    Una credencial que nadie lee es justo lo que parece estar funcionando: el
    estado diría «configurada» y el nodo seguiría siendo el público.
    """
    async with _pagina(InMemorySecretStore()) as (_, pagina):
        pagina._custom_name.setText("ALCHEMY_API_KEY")
        pagina._custom_value.setText("clave-de-prueba")
        pagina._on_save_custom()

        nota = pagina._secret_notes[app_secret_key("ALCHEMY_API_KEY")]
        assert "ningún nodo de config.toml la usa todavía" in nota
        assert "${ALCHEMY_API_KEY}" in nota, "y se dice exactamente qué escribir"


async def test_un_nombre_que_no_se_puede_referenciar_no_se_guarda() -> None:
    """Un nombre que ningún `${...}` puede nombrar daría una credencial inservible.

    Y peor que inservible: con aspecto de configurada. Se rechaza antes de tocar
    el llavero y se dice qué forma tiene que tener.
    """
    async with _pagina(InMemorySecretStore()) as (container, pagina):
        pagina._custom_name.setText("mi clave!")
        pagina._custom_value.setText("clave-de-prueba")
        pagina._on_save_custom()

        assert container.secrets.get(app_secret_key("mi clave!")) is None
        assert "config.toml" in pagina._status.text()


async def test_guardar_sin_nombre_no_escribe_nada() -> None:
    """Un valor sin nombre no se puede ni guardar ni leer después."""
    async with _pagina(InMemorySecretStore()) as (_, pagina):
        pagina._custom_value.setText("clave-de-prueba")
        pagina._on_save_custom()

        assert "nombre" in pagina._status.text()
        assert pagina._custom_value.text() == "clave-de-prueba", "no se pierde lo escrito"
