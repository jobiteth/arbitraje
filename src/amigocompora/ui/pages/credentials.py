"""Credenciales de la aplicación, de motores y de nodos RPC.

Vive en la pestaña Configuración. Antes estaba en Motores, junto a la activación,
y las claves de nodo se mezclaban con las de motor sin que se viera cuál era cuál.

"""

from __future__ import annotations

import re
from typing import Protocol

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    AUTONOMY_PASSPHRASE_SECRET,
    POLYMARKET_BUILDER_KEY_SECRET,
    POLYMARKET_BUILDER_PASSPHRASE_SECRET,
    POLYMARKET_BUILDER_SECRET_SECRET,
    POLYMARKET_RELAYER_ADDRESS_SECRET,
    POLYMARKET_RELAYER_KEY_SECRET,
    PRIVATE_KEY_SECRET,
    SecretStoreError,
    app_env_var_name,
    app_secret_key,
    env_var_name,
    placeholder_names,
    secret_key,
)
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED, COLOR_WARNING
from amigocompora.ui.widgets import Card

#: Lo que tiene que cumplir el nombre de una credencial de la aplicación para que
#: un `${NOMBRE}` de `config.toml` pueda referenciarla. Es la misma forma que
#: acepta `resolve_placeholders`: un nombre que no la cumpla se podría guardar en
#: el llavero, pero ninguna URL podría llamarlo nunca — una credencial que no se
#: puede usar es peor que no tenerla, porque parece configurada.
NOMBRE_VALIDO = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class _SecretSource(Protocol):
    """Lo mínimo que esta pantalla necesita de un proveedor de secretos.

    Es la pregunta «¿el valor que falta llegaría por el entorno?», y se le hace
    al proveedor de verdad —el que firmará, o el que armará la autonomía— y no a
    `os.environ` por nuestra cuenta. La cartera multi-cartera añade un matiz que
    sólo ella sabe contestar: con una cartera cifrada activa no hay variable que
    sirva, porque una clave cifrada no se lee del entorno.
    """

    def env_lookup(self) -> str | None: ...


def rpc_secret_usage(settings: Settings) -> dict[str, tuple[str, ...]]:
    """Qué credenciales usan los nodos declarados, y en qué nodos.

    La clave de un nodo RPC —Infura, Alchemy, QuickNode o uno propio— viaja
    **dentro** de la URL, así que no es una credencial de motor y no aparecía en
    ninguna parte de la interfaz: había que saber que `${INFURA_API_KEY}` se
    resuelve desde el llavero como `app:INFURA_API_KEY` y guardarla por fuera.

    Se recorren los endpoints **declarados** y no los efectivos: los respaldos
    públicos los añade el propio registro y ninguno lleva marcador, así que
    mirarlos sólo alargaría el recorrido.

    Se devuelve el sitio donde se usa cada una porque es lo que distingue una
    casilla útil de una lista de nombres: «va en el nodo «infura» de ethereum»
    dice qué se rompe si falta.
    """
    usage: dict[str, list[str]] = {}
    for chain_settings in settings.chains:
        for endpoint in chain_settings.endpoints:
            for name in placeholder_names(endpoint.url):
                # `display_name` censura la URL cuando no hay etiqueta: la clave
                # que falta puede estar al lado de otra que sí está puesta.
                place = f"el nodo «{endpoint.display_name}» de {chain_settings.chain}"
                places = usage.setdefault(name, [])
                if place not in places:
                    places.append(place)
    return {name: tuple(places) for name, places in sorted(usage.items())}

class CredentialsCard(QWidget):
    #: Se emite cuando se guarda o se borra una credencial. Lo que cambia son las
    #: pestañas que firman: sin cartera no se firma, y sus botones tienen que
    #: enterarse.
    credentials_changed = Signal()

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._secret_fields: dict[str, QLineEdit] = {}
        self._secret_states: dict[str, QLabel] = {}
        self._secret_notes: dict[str, str] = {}
        self._secret_sources: dict[str, _SecretSource] = {
            app_secret_key(PRIVATE_KEY_SECRET): container.keys,
            app_secret_key(AUTONOMY_PASSPHRASE_SECRET): container.passphrase,
        }

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(self._status)
        lay.addWidget(self._build_credentials())
        self._refresh_secrets()

    # --------------------------------------------------------- credenciales #
    def _build_credentials(self) -> Card:
        """La tarjeta donde se escriben las credenciales.

        Se construye **una vez** y sólo se repinta el estado: reconstruirla en
        cada refresco borraría lo que el usuario está escribiendo a medio teclear,
        que es la forma más rápida de que alguien acabe pegando la clave en el
        sitio equivocado.
        """
        card = Card(
            "Credenciales",
            subtitle="van al llavero del sistema, nunca a un fichero del repositorio",
        )
        self._secrets_grid = QGridLayout()
        self._secrets_grid.setHorizontalSpacing(10)
        self._secrets_grid.setVerticalSpacing(6)
        card.body().addLayout(self._secrets_grid)

        note = QLabel(
            "Lo que se escriba aquí se guarda cifrado por el sistema operativo "
            "—Administrador de credenciales en Windows, Llavero en macOS, Secret "
            "Service en Linux— y no se muestra nunca más. Ningún valor se escribe "
            "en config.toml ni en el registro de ejecuciones."
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        card.body().addWidget(note)

        self._credentials_rows: dict[str, tuple[QLabel, QPushButton, QPushButton]] = {}
        self._fill_credentials()
        card.body().addLayout(self._build_custom_secret())
        # Plegada o abierta según haga falta, que es lo que decide si esta tarjeta
        # es la respuesta a una pregunta o un muro de catorce campos.
        card.set_collapsible(expanded=self._falta_la_clave())
        return card

    def _build_custom_secret(self) -> QHBoxLayout:
        """La fila para guardar una credencial que todavía no tiene casilla.

        Las casillas de arriba salen de lo que ya está declarado —el manifiesto de
        un motor, un `${NOMBRE}` escrito en `config.toml`—, y eso deja un hueco con
        forma de huevo y gallina: la clave de un nodo nuevo no tiene dónde ponerse
        hasta que el fichero la menciona, y el fichero no se puede probar hasta que
        la clave está guardada. Aquí se guarda por su nombre, en cualquier orden.

        El valor va enmascarado y el nombre no: el nombre es lo que luego hay que
        escribir en `config.toml`, así que tiene que poder leerse y copiarse.
        """
        fila = QHBoxLayout()
        fila.addWidget(QLabel("Otra credencial:"))

        self._custom_name = QLineEdit()
        self._custom_name.setPlaceholderText("INFURA_API_KEY")
        self._custom_name.setToolTip(
            "El nombre con el que se referencia desde config.toml. Una clave "
            "guardada como INFURA_API_KEY se usa escribiendo "
            "${INFURA_API_KEY} dentro de la URL del nodo."
        )
        fila.addWidget(self._custom_name, stretch=1)

        self._custom_value = QLineEdit()
        self._custom_value.setEchoMode(QLineEdit.EchoMode.Password)
        self._custom_value.setPlaceholderText("valor")
        fila.addWidget(self._custom_value, stretch=2)

        guardar = QPushButton("Guardar")
        guardar.clicked.connect(self._on_save_custom)
        fila.addWidget(guardar)
        return fila

    def _on_save_custom(self) -> None:
        """Guarda la credencial escrita y le crea su casilla, si no la tenía.

        Guardar **no** la pone en uso: para que se use, la URL de un nodo en
        `config.toml` tiene que nombrarla. Se dice aquí, porque una credencial
        guardada que nadie lee es exactamente lo que parece estar funcionando.
        """
        name = self._custom_name.text().strip()
        value = self._custom_value.text().strip()
        if not name or not value:
            self._status.setText(
                "Para guardar una credencial hacen falta las dos cosas: el nombre "
                "con el que se referencia y el valor."
            )
            return
        if not NOMBRE_VALIDO.match(name):
            self._status.setText(
                f"«{name}» no se puede referenciar desde config.toml: el nombre "
                f"empieza por una letra y sigue con letras, números o «_»."
            )
            return

        key = app_secret_key(name)
        if key not in self._secret_fields:
            usage = rpc_secret_usage(self._container.settings)
            self._add_secret_row(
                self._secrets_grid,
                self._secrets_grid.rowCount(),
                key,
                f"Clave de nodo — {name}",
                (
                    f"Va dentro de la URL de {', '.join(usage[name])}."
                    if name in usage
                    else f"Guardada, pero ningún nodo de config.toml la usa todavía: "
                    f"escribe ${{{name}}} dentro de la URL de un endpoint para "
                    f"que se resuelva al arrancar."
                ),
                required=False,
            )

        self._secret_fields[key].setText(value)
        self._custom_value.clear()
        self._custom_name.clear()
        # Se reutiliza el camino de guardado de las demás —mismo llavero, mismo
        # aviso, mismo vaciado del campo— en vez de escribir aquí un segundo.
        self._on_save_secret(key)

    def _falta_la_clave(self) -> bool:
        """Si la cartera está sin configurar, en los términos que hacen falta aquí.

        Un llavero que no responde cuenta como «falta»: con la tarjeta abierta se
        ve la descripción de cada credencial, y ahí está la variable de entorno que
        es la única salida cuando no hay llavero. Cerrarla escondería justo eso.
        """
        try:
            return not self._container.secrets.get(app_secret_key(PRIVATE_KEY_SECRET))
        except SecretStoreError:
            return True

    def _fill_credentials(self) -> None:
        """Crea una fila por credencial: estado, campo, guardar y borrar.

        Las credenciales de la aplicación van primero, y la clave privada la
        primera de todas: es la que hace falta para poder firmar algo, y la que
        hasta ahora no tenía dónde ponerse.
        """
        rows: tuple[tuple[str, str, str, bool], ...] = (
            (
                app_secret_key(PRIVATE_KEY_SECRET),
                "Clave privada de la cartera",
                "Con ella se firma. Se lee sólo en el momento de firmar y no se "
                "guarda en ningún objeto. Es la clave de la cartera heredada —la "
                "del llavero—; las demás carteras, cifradas o en modo observación, "
                "se gestionan en la pestaña Cartera.",
                True,
            ),
            (
                app_secret_key(AUTONOMY_PASSPHRASE_SECRET),
                "Frase de la ejecución desatendida",
                "Sólo hace falta para emitir sin preguntar. Sin ella la autonomía "
                "no se puede armar.",
                False,
            ),
            (
                app_secret_key(POLYMARKET_RELAYER_KEY_SECRET),
                "Polymarket — Clave API del relayer",
                "Clave que da polymarket.com para el relayer de la deposit wallet.",
                False,
            ),
            (
                app_secret_key(POLYMARKET_RELAYER_ADDRESS_SECRET),
                "Polymarket — Dirección",
                "Dirección de la cuenta a la que pertenece la clave del relayer.",
                False,
            ),
            (
                app_secret_key(POLYMARKET_BUILDER_KEY_SECRET),
                "Polymarket — Builder key",
                "Clave pública de la Builder key. Sólo hace falta para desplegar la "
                "deposit wallet (WALLET-CREATE).",
                False,
            ),
            (
                app_secret_key(POLYMARKET_BUILDER_SECRET_SECRET),
                "Polymarket — Builder secreto",
                "Secreto de la Builder key. Firma cada petición de despliegue.",
                False,
            ),
            (
                app_secret_key(POLYMARKET_BUILDER_PASSPHRASE_SECRET),
                "Polymarket — Builder frase de paso",
                "Frase de paso de la Builder key. Va junto al secreto.",
                False,
            ),
        )

        for position, (key, label, note, required) in enumerate(rows):
            self._add_secret_row(self._secrets_grid, position, key, label, note, required=required)

        position = len(rows)

        # Las claves de los nodos RPC. No hay ninguna lista de proveedores escrita
        # aquí: se leen los `${NOMBRE}` que la propia configuración usa en sus
        # endpoints, igual que las de motor se leen del manifiesto. Escribir
        # «Infura» en la interfaz habría dejado fuera a Alchemy, a QuickNode y a
        # cualquier nodo propio, que usan exactamente el mismo mecanismo.
        for name, places in rpc_secret_usage(self._container.settings).items():
            self._add_secret_row(
                self._secrets_grid,
                position,
                app_secret_key(name),
                f"Clave de nodo — {name}",
                f"Va dentro de la URL de {', '.join(places)}. Sin ella ese nodo se "
                f"descarta al arrancar y la red usa los respaldos públicos.",
                required=False,
            )
            position += 1

        # Una credencial por opción declarada en el manifiesto de un motor
        # registrado. Se leen del manifiesto y no de una lista escrita aquí: un
        # motor nuevo publica sus claves y aparecen solas, que es lo que hace que
        # añadir un motor no toque la interfaz.
        for entry in self._container.registry.available():
            manifest = entry.manifest
            for option in manifest.config_options:
                required = option in manifest.required_config
                self._add_secret_row(
                    self._secrets_grid,
                    position,
                    secret_key(manifest.engine_id, option),
                    f"{manifest.name} — {option}",
                    (
                        "Obligatoria: sin ella este motor no arranca."
                        if required
                        else "Opcional: el motor funciona sin ella."
                    ),
                    required=required,
                )
                position += 1

    def _add_secret_row(
        self,
        grid: QGridLayout,
        row: int,
        key: str,
        label: str,
        note: str,
        *,
        required: bool,
    ) -> None:
        title = QLabel(label)
        title.setToolTip(note)
        grid.addWidget(title, row, 0)

        state = QLabel("")
        state.setObjectName("hint")
        grid.addWidget(state, row, 1)

        field = QLineEdit()
        # Enmascarado: la clave privada y una API key se parecen en que ninguna
        # debería quedar legible en una pantalla compartida.
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText("pegar aquí y pulsar Guardar")
        grid.addWidget(field, row, 2)

        save = QPushButton("Guardar")
        save.clicked.connect(lambda _=False, k=key: self._on_save_secret(k))
        grid.addWidget(save, row, 3)

        erase = QPushButton("Borrar")
        erase.setObjectName("secondary")
        erase.clicked.connect(lambda _=False, k=key: self._on_forget_secret(k))
        grid.addWidget(erase, row, 4)

        self._secret_fields[key] = field
        self._secret_states[key] = state
        self._secret_notes[key] = self._where_it_lives(key, note, required=required)

    def _where_it_lives(self, key: str, note: str, *, required: bool) -> str:
        """Dónde vive esta credencial, en las palabras que el usuario necesita.

        Se nombra el **llavero** y también la variable de entorno equivalente
        porque las dos vías existen y sólo una se puede usar desde aquí: quien
        despliegue en un contenedor sin llavero necesita saber el nombre exacto de
        la variable, y quien abra la aplicación en su portátil necesita saber que
        no tiene que tocar el entorno.

        Y se dice **qué hace falta** para que esa variable sirva, porque para las
        dos credenciales de la aplicación no basta con ponerla: el proveedor que
        las lee sólo mira el entorno si `[execution] allow_env_key` está encendido.
        """
        if key.startswith("app:"):
            env = app_env_var_name(key.removeprefix("app:"))
            # La clave privada y la frase las lee un proveedor —el que firma, el
            # que arma la autonomía— que **sólo** mira la variable si la
            # configuración lo autoriza; las de motor las resuelve el registro, y
            # las de los nodos RPC las resuelve el contenedor al arrancar, que
            # miran el entorno siempre. Nombrar la variable sin decir la condición
            # mandaría a un servidor sin llavero a poner una variable que nadie va
            # a leer, que es justo lo que el `require()` de al lado se cuida de no
            # hacer; decir la condición donde no la hay sería el error simétrico.
            condicionada = key in self._secret_sources
            via = (
                f"variable equivalente {env}, que sólo se lee si se enciende "
                f"[execution] allow_env_key"
                if condicionada and not self._container.settings.execution.allow_env_key
                else f"variable equivalente {env}"
            )
        else:
            engine_id, _, option = key.partition(":")
            via = f"variable equivalente {env_var_name(engine_id, option)}"
        kind = "obligatoria" if required else "opcional"
        return f"{note} Es {kind}. Credencial «{key}»; {via}."

    def _servida_por_el_entorno(self, key: str) -> str | None:
        """La variable de entorno que sirve esta credencial, si es que la hay.

        Se pregunta al **proveedor de verdad** —el mismo que usará el caso de uso
        al firmar— y no al entorno por nuestra cuenta: mirar `os.environ` diría
        que hay una clave ahí aunque el respaldo estuviera apagado y el proveedor
        fuera a negarse, que es prometer una firma que no va a ocurrir. Con
        varias carteras hay un matiz más que sólo el proveedor sabe: una cartera
        cifrada no se lee del entorno, así que con ella activa no hay variable
        que ofrecer.

        Sólo devuelve algo cuando el almacén ya ha fallado —es donde se llama—,
        así que un valor aquí significa necesariamente que vino del entorno.
        """
        proveedor = self._secret_sources.get(key)
        if proveedor is None:
            return None
        return proveedor.env_lookup()

    def _refresh_secrets(self) -> None:
        """Repinta el estado de cada credencial. **Nunca el valor.**

        Del valor guardado sólo se publica si existe: ni su longitud, ni sus
        primeros caracteres, ni una pista. Lo que se puede afirmar sin riesgo es
        que está, y eso es lo que se dice.
        """
        for key, state in self._secret_states.items():
            try:
                present = bool(self._container.secrets.get(key))
            except SecretStoreError as error:
                variable = self._servida_por_el_entorno(key)
                if variable is not None:
                    # El llavero no responde, pero la credencial **está** y se
                    # sirve por otra vía. Decir sólo «el llavero no responde»
                    # dejaría al usuario creyendo que no puede firmar cuando sí
                    # puede: la frase es cierta y la conclusión que saca, falsa.
                    state.setText("configurada por el entorno")
                    state.setStyleSheet(f"color: {COLOR_WARNING};")
                    state.setToolTip(
                        f"El llavero del sistema no responde, así que esta credencial "
                        f"se está leyendo de {variable}. Sirve —con ella se firma— y "
                        f"es menos segura que el llavero: cualquiera con acceso al "
                        f"entorno del proceso la ve, y una clave privada no se puede "
                        f"rotar sin cambiar de cartera.\n\n{self._secret_notes[key]}"
                    )
                    continue
                # Un llavero que no responde no es «sin configurar»: es un fallo
                # del sistema, y decir lo primero culparía al usuario.
                #
                # El fallo **se suma** a la descripción en vez de sustituirla.
                # Sustituirla parecía razonable —el error es lo que acaba de
                # pasar— pero deja al usuario sin la única salida que le queda:
                # este es justo el caso en el que el llavero no sirve, así que la
                # variable de entorno que nombra la descripción es lo que hay que
                # usar, y el propio botón de guardar le manda a leerla («usa la
                # variable de entorno que se indica en su descripción»). Con el
                # error encima, esa frase señalaba a un texto que ya no estaba.
                state.setText("el llavero no responde")
                state.setStyleSheet(f"color: {COLOR_DANGER};")
                state.setToolTip(f"{error}\n\n{self._secret_notes[key]}")
                continue
            state.setText("configurada" if present else "sin configurar")
            state.setStyleSheet("" if present else f"color: {COLOR_MUTED};")
            state.setToolTip(self._secret_notes[key])

    def _on_save_secret(self, key: str) -> None:
        value = self._secret_fields[key].text()
        if not value.strip():
            self._status.setText("No hay nada que guardar: el campo está vacío.")
            return
        try:
            self._container.secrets.set(key, value.strip())
        except SecretStoreError as error:
            self._status.setText(
                f"No se pudo guardar «{key}»: {error}. En un equipo sin llavero, "
                f"usa la variable de entorno que se indica en su descripción."
            )
            return
        finally:
            # El campo se vacía **siempre**, incluso si falló: dejar la clave
            # escrita en un widget es dejarla en una pantalla.
            self._secret_fields[key].clear()
        self._status.setText(f"«{key}» guardada en el llavero. No se volverá a mostrar.")
        self._refresh_secrets()
        # Guardar la clave privada cambia si se puede firmar, y eso lo deciden
        # los botones de otras pestañas. Se avisa con una señal en vez de dejar
        # que lo descubran: un «Ejecutar» que sigue apagado después de configurar
        # la cartera enseña a desconfiar del botón.
        self.credentials_changed.emit()

    def _on_forget_secret(self, key: str) -> None:
        try:
            self._container.secrets.delete(key)
        except SecretStoreError as error:
            self._status.setText(f"No se pudo borrar «{key}»: {error}")
            return
        self._status.setText(f"«{key}» borrada del llavero.")
        self._refresh_secrets()
        self.credentials_changed.emit()
