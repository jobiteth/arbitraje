"""La ventana de un nodo RPC: se agrega o se edita, y sólo se guarda lo probado.

Antes esto era un formulario fijo al final de la tarjeta de nodos: ocupaba su
alto entero las cien veces que no se estaba añadiendo nada y no servía para
editar un nodo ya guardado —corregir una URL exigía borrar el nodo y volver a
escribirlo—. En una ventana, en cambio, el mismo formulario sirve para las dos
cosas y sólo está delante cuando se está usando.

### Lo que se exige probar, y lo que no

Un nodo **nuevo** sólo se guarda si la prueba (`eth_chainId`) respondió con la
red elegida: la regla de siempre, y la que evita escribir en `config.toml` una
URL que no contesta. Al editar se afina: si lo único que cambió es la etiqueta o
la prioridad, no hay nada nuevo que probar —la URL ya se probó el día que se
guardó— y exigir la prueba otra vez sería pedir un gesto que no comprueba nada.
Sí se exige si cambió la URL o si se escribió una clave nueva, que son las dos
cosas que la prueba mide de verdad.

La clave nunca se escribe en `config.toml`: se guarda en el llavero y la URL la
nombra como `${NOMBRE}`. Si el nodo ya tenía una clave guardada y el valor se
deja vacío, la clave se conserva; para reemplazarla se escribe la nueva.
"""

from __future__ import annotations

import hashlib

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.chains import CHAINS
from amigocompora.domain.errors import AmigocomporaError
from amigocompora.infra.config import RpcEndpointSettings, config_file
from amigocompora.infra.config_writer import set_chain_endpoints
from amigocompora.infra.rpc.probe import probe_node
from amigocompora.infra.secrets import (
    SecretStoreError,
    app_env_var_name,
    app_secret_key,
    placeholder_names,
    resolve_placeholders,
)
from amigocompora.ui import icons
from amigocompora.ui.pages.credentials import NOMBRE_VALIDO
from amigocompora.ui.widgets import spawn


class NodeDialog(QDialog):
    """Agregar o editar un nodo de una red, con prueba antes de guardar.

    El panel la abre y, si el diálogo termina aceptado, lee `endpoints` —la lista
    que quedó escrita— para repintar su tabla sin volver a leer el archivo.
    """

    def __init__(
        self,
        container: Container,
        parent: QWidget | None = None,
        *,
        chain_key: str,
        endpoints: list[RpcEndpointSettings],
        index: int | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._chain_key = chain_key
        self._existentes = list(endpoints)
        self._index = index
        endpoint = self._existentes[index] if index is not None else None

        #: Lo que el panel lee tras `accept()`: la lista que quedó escrita.
        self.endpoints: tuple[RpcEndpointSettings, ...] | None = None
        #: Si en esta operación se guardó una clave nueva en el llavero.
        self.key_saved = False
        #: Firma de los campos cuya prueba respondió bien. Guardar sólo se
        #: habilita si lo que hay escrito coincide con esa firma.
        self._verified: str | None = None

        nombre_red = CHAINS[chain_key].name if chain_key in CHAINS else chain_key
        editing = endpoint is not None
        self.setWindowTitle(f"{'Editar' if editing else 'Agregar'} nodo — {nombre_red}")
        self.setModal(True)
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(18, 16, 18, 16)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)

        self._chain = QComboBox()
        for spec in sorted((s for s in CHAINS.values() if s.is_evm), key=lambda s: s.name):
            self._chain.addItem(icons.network_icon(spec.key), spec.name, spec.key)
        posicion = self._chain.findData(chain_key)
        if posicion >= 0:
            self._chain.setCurrentIndex(posicion)
        if editing:
            # La red se fija al editar: cambiarla sería borrar de una red y crear
            # en otra, y ese gesto ya tiene su camino —eliminar y agregar— donde
            # la prueba vuelve a exigirse entera.
            self._chain.setEnabled(False)
            self._chain.setToolTip(
                "La red de un nodo guardado no se cambia desde aquí: elimínalo y "
                "agrégalo en la otra red, que es cuando la prueba se exige de nuevo."
            )

        self._label = QLineEdit(endpoint.label if endpoint else "")
        self._label.setPlaceholderText("opcional, p. ej. «mi nodo»")
        self._url = QLineEdit(endpoint.url if endpoint else "")
        self._url.setPlaceholderText("https://…  la clave va como ${NOMBRE}")

        self._key_name = QLineEdit()
        self._key_name.setPlaceholderText("NOMBRE_DE_LA_CLAVE, opcional")
        self._key_value = QLineEdit()
        self._key_value.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_value.setPlaceholderText("valor de la clave, opcional")

        self._priority = QSpinBox()
        self._priority.setRange(1, 999)
        self._priority.setValue(endpoint.priority if endpoint else 10)

        campos = (
            ("Red:", self._chain),
            ("Etiqueta:", self._label),
            ("URL:", self._url),
            ("Nombre de la clave:", self._key_name),
            ("Valor de la clave:", self._key_value),
            ("Prioridad:", self._priority),
        )
        for fila, (texto, widget) in enumerate(campos):
            form.addWidget(QLabel(texto), fila, 0)
            form.addWidget(widget, fila, 1)
        lay.addLayout(form)

        self._key_note = QLabel("")
        self._key_note.setObjectName("hint")
        self._key_note.setWordWrap(True)
        lay.addWidget(self._key_note)

        self._probe_label = QLabel("")
        self._probe_label.setWordWrap(True)
        lay.addWidget(self._probe_label)

        buttons = QDialogButtonBox()
        self._probe_btn = buttons.addButton("Probar", QDialogButtonBox.ButtonRole.ActionRole)
        self._probe_btn.setObjectName("secondary")
        self._save_btn = buttons.addButton("Guardar nodo", QDialogButtonBox.ButtonRole.AcceptRole)
        cancelar = buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        cancelar.setObjectName("secondary")
        self._save_btn.clicked.connect(self._on_save)
        self._probe_btn.clicked.connect(self._on_probe)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        for widget in (self._label, self._key_value):
            widget.textChanged.connect(self._invalidate)
        self._key_name.textChanged.connect(self._invalidate)
        self._url.textChanged.connect(self._on_url_changed)
        self._priority.valueChanged.connect(self._invalidate)

        self._refresh_key_note()
        self._invalidate()

    # ------------------------------------------------------------------ #
    # Estado de los campos
    # ------------------------------------------------------------------ #
    def _on_url_changed(self, texto: str) -> None:
        """Rellena el nombre de la clave desde el `${NOMBRE}` de la URL.

        Se rellena sólo si el campo está vacío: adivinar encima de lo que alguien
        está escribiendo es la forma rápida de que acabe guardando un nombre que
        no era el suyo.
        """
        if not self._key_name.text().strip():
            nombres = placeholder_names(texto)
            if nombres:
                self._key_name.setText(nombres[0])
        self._invalidate()

    def _signature(self) -> str:
        """Lo escrito, con la clave en forma de huella.

        La clave no se guarda en ningún atributo: sólo su SHA-256, para saber si
        lo escrito ahora es lo mismo que se probó.
        """
        huella = hashlib.sha256(self._key_value.text().strip().encode()).hexdigest()
        return "|".join(
            (
                str(self._chain.currentData()),
                self._label.text().strip(),
                self._url.text().strip(),
                self._key_name.text().strip(),
                str(self._priority.value()),
                huella,
            )
        )

    def _needs_probe(self) -> bool:
        """Si hay algo nuevo que sólo la prueba puede demostrar.

        Al editar, etiqueta y prioridad no se prueban: la URL ya estaba probada.
        La URL nueva o una clave nueva sí — son justo lo que la prueba mide.
        """
        if self._index is None:
            return True
        original = self._existentes[self._index]
        if self._url.text().strip() != original.url:
            return True
        return bool(self._key_value.text().strip())

    def _invalidate(self, *_: object) -> None:
        self._verified = None
        self._refresh_key_note()
        if self._needs_probe():
            self._probe_label.setText("Pendiente de prueba: pulsa Probar antes de guardar.")
        else:
            self._probe_label.setText("")
        self._save_btn.setEnabled(not self._needs_probe())

    def _refresh_key_note(self) -> None:
        """La nota de la clave: dónde vive, si ya había una, y qué la lee."""
        nombre = self._key_name.text().strip()
        if not nombre:
            self._key_note.setText("")
            return
        if not NOMBRE_VALIDO.match(nombre):
            self._key_note.setText(
                f"«{nombre}» no vale como nombre: empieza por una letra y sigue con "
                f"letras, números o «_»."
            )
            return
        guardada = self._hay_clave_guardada(nombre)
        env = app_env_var_name(nombre)
        if guardada:
            estado = (
                f"Ya hay una clave guardada como «{app_secret_key(nombre)}». Déjala en "
                f"blanco para conservarla, o escribe otra para reemplazarla."
            )
        else:
            estado = f"Se guardará como «{app_secret_key(nombre)}» en el llavero del sistema."
        self._key_note.setText(
            f"{estado} Variable de entorno equivalente {env}, que se lee siempre al "
            f"resolver los nodos."
        )

    def _hay_clave_guardada(self, nombre: str) -> bool:
        try:
            return bool(self._container.secrets.get(app_secret_key(nombre)))
        except SecretStoreError:
            return False

    # ------------------------------------------------------------------ #
    # Probar
    # ------------------------------------------------------------------ #
    def _validado(self) -> RpcEndpointSettings | None:
        """Los campos convertidos en endpoint, o `None` con el motivo ya dicho."""
        url = self._url.text().strip()
        nombre = self._key_name.text().strip()
        valor = self._key_value.text().strip()
        if not url:
            self._probe_label.setText("Escribe la URL del nodo.")
            return None
        if nombre and not NOMBRE_VALIDO.match(nombre):
            self._probe_label.setText(
                f"«{nombre}» no vale como nombre: empieza por una letra y sigue con "
                f"letras, números o «_»."
            )
            return None
        if nombre and f"${{{nombre}}}" not in url:
            self._probe_label.setText(
                f"La URL tiene que contener ${{{nombre}}} donde va la clave."
            )
            return None
        if valor and not nombre:
            self._probe_label.setText("Para guardar la clave, escribe también su nombre.")
            return None
        try:
            return RpcEndpointSettings(
                url=url, label=self._label.text().strip(), priority=self._priority.value()
            )
        except ValidationError as error:
            self._probe_label.setText(f"URL no válida: {error.errors()[0]['msg']}")
        return None

    def _on_probe(self) -> None:
        endpoint = self._validado()
        if endpoint is None:
            return
        nombre = self._key_name.text().strip()
        valor = self._key_value.text().strip()
        try:
            if nombre and valor:
                probe_url = endpoint.url.replace(f"${{{nombre}}}", valor)
            else:
                probe_url = resolve_placeholders(
                    endpoint.url, self._container.secrets, place=f"el nodo de {self._chain_key}"
                )
        except AmigocomporaError as error:
            self._probe_label.setText(f"No se puede resolver la clave: {error}")
            return

        esperada = CHAINS[str(self._chain.currentData())].eip155_id
        firma = self._signature()
        self._probe_btn.setEnabled(False)
        self._probe_label.setText("Probando el nodo…")
        spawn(self._do_probe(firma, probe_url, esperada))

    async def _do_probe(self, firma: str, url: str, esperada: int | None) -> None:
        try:
            resultado = await probe_node(url, esperada)
        finally:
            self._probe_btn.setEnabled(True)
        if firma != self._signature():
            return
        if resultado.ok:
            self._verified = firma
            self._probe_label.setText(f"✓ {resultado.detail} ({resultado.latency_ms} ms)")
        else:
            self._verified = None
            self._probe_label.setText(f"✗ {resultado.detail}")
        self._save_btn.setEnabled(self._verified == self._signature())

    # ------------------------------------------------------------------ #
    # Guardar
    # ------------------------------------------------------------------ #
    def _on_save(self) -> None:
        if self._needs_probe() and self._verified != self._signature():
            return
        endpoint = self._validado()
        if endpoint is None:
            return
        chain_key = str(self._chain.currentData())
        nombre = self._key_name.text().strip()
        valor = self._key_value.text().strip()

        repetidos = [
            existing
            for indice, existing in enumerate(self._existentes)
            if existing.url == endpoint.url and indice != self._index
        ]
        if repetidos:
            self._probe_label.setText("Ya hay un nodo con esa URL en esa red.")
            return

        if nombre and valor:
            try:
                self._container.secrets.set(app_secret_key(nombre), valor)
            except SecretStoreError as error:
                self._probe_label.setText(f"No se pudo guardar la clave en el llavero: {error}")
                return
            self.key_saved = True

        restantes = list(self._existentes)
        if self._index is None:
            restantes.append(endpoint)
        else:
            restantes[self._index] = endpoint
        try:
            set_chain_endpoints(config_file(), chain_key, restantes)
        except (OSError, ValueError) as error:
            self._probe_label.setText(f"No se pudo escribir config.toml: {error}")
            return

        self.endpoints = tuple(restantes)
        self.accept()
