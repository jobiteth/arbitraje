"""Pestaña: Configuración — nodos RPC, APIs de motores y credenciales.

Aquí se decide **con qué nodos** habla cada red y **con qué claves** se llama a
cada API. Motores se queda sólo con la activación: antes las tres cosas estaban
en la misma pantalla y no se sabía dónde tocar para cambiar un nodo.

Un nodo nuevo sólo se guarda si la prueba (`eth_chainId`) responde con la red
elegida. La clave nunca se escribe en `config.toml`: se guarda en el llavero y la
URL la nombra como `${NOMBRE}`, igual que los nodos que ya estaban declarados.
"""

from __future__ import annotations

import hashlib

from pydantic import ValidationError
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.chains import CHAINS
from amigocompora.domain.errors import AmigocomporaError
from amigocompora.infra.config import RpcEndpointSettings, config_file
from amigocompora.infra.config_writer import set_chain_endpoints
from amigocompora.infra.logging import safe_url
from amigocompora.infra.rpc.probe import probe_node
from amigocompora.infra.secrets import (
    SecretStoreError,
    app_secret_key,
    resolve_placeholders,
    secret_key,
)
from amigocompora.ui import icons
from amigocompora.ui.pages.credentials import NOMBRE_VALIDO, CredentialsCard
from amigocompora.ui.theme import COLOR_MUTED
from amigocompora.ui.widgets import Card, ScrollArea, spawn

_ORIGEN_CONFIG = "config.toml"
_ORIGEN_PUBLICO = "respaldo público"


class ConfiguracionPage(QWidget):
    #: Se reenvían las señales de las credenciales: quien firma escucha aquí, y
    #: no necesita saber que las credenciales viven en una tarjeta de esta pestaña.
    credentials_changed = Signal()

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        #: Los nodos que el usuario declaró en `config.toml`, por red. Se mantienen
        #: aquí porque la configuración cargada al arrancar es inmutable, y tras
        #: guardar un nodo la tabla tiene que mostrarlo sin reiniciar.
        self._declared: dict[str, list[RpcEndpointSettings]] = {
            chain.chain: list(chain.endpoints) for chain in container.settings.chains
        }
        self._public: dict[str, tuple[RpcEndpointSettings, ...]] = {
            chain.chain: (
                chain.effective_endpoints()[len(chain.endpoints) :]
                if chain.include_public_fallbacks
                else ()
            )
            for chain in container.settings.chains
        }
        #: Firma de los campos del formulario cuya prueba respondió bien. Guardar
        #: sólo se habilita si lo que hay escrito coincide con esa firma.
        self._verified: str | None = None
        #: Qué fila de la tabla es cada nodo: (red, índice en `_declared` o None si
        #: es un respaldo público). Sólo los declarados se pueden eliminar.
        self._rows: list[tuple[str, int | None]] = []

        lay = ScrollArea.fill(self, spacing=12).body()
        lay.addWidget(self._build_nodes())
        lay.addWidget(self._build_apis())
        self._credentials = CredentialsCard(container)
        self._credentials.credentials_changed.connect(self._on_credentials_changed)
        lay.addWidget(self._credentials)

        self._fill_nodes_table()
        self._refresh_apis()

    # ----------------------------------------------------------------- nodos #
    def _build_nodes(self) -> Card:
        card = Card(
            "Nodos RPC",
            subtitle="los nodos se aplican al arrancar: tras guardar, reinicia la aplicación",
        )

        self._nodes_table = QTableWidget(0, 5)
        self._nodes_table.setHorizontalHeaderLabels(["Red", "Etiqueta", "URL", "Prioridad", "Origen"])
        self._nodes_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._nodes_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._nodes_table.setMinimumHeight(180)
        self._nodes_table.itemSelectionChanged.connect(self._refresh_delete)
        card.body().addWidget(self._nodes_table, stretch=1)

        self._delete_btn = QPushButton("Eliminar nodo seleccionado")
        self._delete_btn.setObjectName("secondary")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._on_delete_node)
        card.body().addWidget(self._delete_btn)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)

        self._chain = QComboBox()
        for spec in sorted((s for s in CHAINS.values() if s.is_evm), key=lambda s: s.name):
            self._chain.addItem(icons.network_icon(spec.key), spec.name, spec.key)

        self._label = QLineEdit()
        self._label.setPlaceholderText("opcional, p. ej. «mi nodo»")
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://…  la clave va como ${NOMBRE}")
        self._key_name = QLineEdit()
        self._key_name.setPlaceholderText("NOMBRE_DE_LA_CLAVE, opcional")
        self._key_value = QLineEdit()
        self._key_value.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_value.setPlaceholderText("valor de la clave, opcional")
        self._priority = QSpinBox()
        self._priority.setRange(1, 999)
        self._priority.setValue(10)

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
        card.body().addLayout(form)

        for widget in (self._label, self._url, self._key_name, self._key_value):
            widget.textChanged.connect(self._invalidate)
        self._chain.currentIndexChanged.connect(self._invalidate)
        self._priority.valueChanged.connect(self._invalidate)

        botones = QHBoxLayout()
        self._probe_btn = QPushButton("Probar")
        self._probe_btn.setObjectName("secondary")
        self._probe_btn.clicked.connect(self._on_probe)
        self._save_btn = QPushButton("Guardar nodo")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        botones.addWidget(self._probe_btn)
        botones.addWidget(self._save_btn)
        botones.addStretch(1)
        card.body().addLayout(botones)

        self._probe_label = QLabel("")
        self._probe_label.setWordWrap(True)
        card.body().addWidget(self._probe_label)
        self._node_status = QLabel("")
        self._node_status.setWordWrap(True)
        self._node_status.setStyleSheet(f"color: {COLOR_MUTED};")
        card.body().addWidget(self._node_status)
        return card

    def _fill_nodes_table(self) -> None:
        self._nodes_table.setRowCount(0)
        self._rows = []
        for chain_key in sorted(set(self._declared) | set(self._public)):
            nombre = CHAINS[chain_key].name if chain_key in CHAINS else chain_key
            for indice, endpoint in enumerate(self._declared.get(chain_key, [])):
                self._add_row(nombre, endpoint, _ORIGEN_CONFIG, (chain_key, indice))
            for endpoint in self._public.get(chain_key, ()):
                self._add_row(nombre, endpoint, _ORIGEN_PUBLICO, (chain_key, None))
        self._refresh_delete()

    def _add_row(
        self,
        nombre: str,
        endpoint: RpcEndpointSettings,
        origen: str,
        ref: tuple[str, int | None],
    ) -> None:
        fila = self._nodes_table.rowCount()
        self._nodes_table.insertRow(fila)
        celdas = (
            nombre,
            endpoint.label or "—",
            safe_url(endpoint.url),
            str(endpoint.priority),
            origen,
        )
        for columna, texto in enumerate(celdas):
            self._nodes_table.setItem(fila, columna, QTableWidgetItem(texto))
        # El icono de la red en la primera celda. El texto no se toca: la columna
        # dice la red y hay filas cuya clave no está en el catálogo (se enseña la
        # clave cruda), así que el icono es un extra que puede faltar.
        red = self._nodes_table.item(fila, 0)
        if red is not None:
            red.setIcon(icons.network_icon(ref[0]))
        self._rows.append(ref)

    def _refresh_delete(self) -> None:
        fila = self._nodes_table.currentRow()
        declarado = 0 <= fila < len(self._rows) and self._rows[fila][1] is not None
        self._delete_btn.setEnabled(declarado)

    def _on_delete_node(self) -> None:
        fila = self._nodes_table.currentRow()
        if not (0 <= fila < len(self._rows)):
            return
        chain_key, indice = self._rows[fila]
        if indice is None:
            return
        restantes = [ep for i, ep in enumerate(self._declared[chain_key]) if i != indice]
        try:
            set_chain_endpoints(config_file(), chain_key, restantes)
        except (OSError, ValueError) as error:
            self._node_status.setText(f"No se pudo escribir config.toml: {error}")
            return
        self._declared[chain_key] = restantes
        self._fill_nodes_table()
        self._node_status.setText(
            "Nodo eliminado de config.toml. Su clave, si la tenía, sigue en el llavero."
        )

    def _signature(self) -> str:
        """Lo que se ha escrito en el formulario, con la clave en forma de huella.

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

    def _invalidate(self, *_: object) -> None:
        self._verified = None
        self._save_btn.setEnabled(False)
        self._probe_label.setText("Pendiente de prueba: pulsa Probar antes de guardar.")

    def _on_probe(self) -> None:
        chain_key = str(self._chain.currentData())
        url = self._url.text().strip()
        nombre = self._key_name.text().strip()
        valor = self._key_value.text().strip()
        if not url:
            self._probe_label.setText("Escribe la URL del nodo.")
            return
        if nombre and not NOMBRE_VALIDO.match(nombre):
            self._probe_label.setText(
                f"«{nombre}» no vale como nombre: empieza por una letra y sigue con "
                f"letras, números o «_»."
            )
            return
        if nombre and f"${{{nombre}}}" not in url:
            self._probe_label.setText(f"La URL tiene que contener ${{{nombre}}} donde va la clave.")
            return
        if valor and not nombre:
            self._probe_label.setText("Para guardar la clave, escribe también su nombre.")
            return
        try:
            endpoint = RpcEndpointSettings(
                url=url,
                label=self._label.text().strip(),
                priority=self._priority.value(),
            )
        except ValidationError as error:
            self._probe_label.setText(f"URL no válida: {error.errors()[0]['msg']}")
            return

        try:
            if nombre and valor:
                probe_url = url.replace(f"${{{nombre}}}", valor)
            else:
                probe_url = resolve_placeholders(
                    endpoint.url, self._container.secrets, place=f"el nodo de {chain_key}"
                )
        except AmigocomporaError as error:
            self._probe_label.setText(f"No se puede resolver la clave: {error}")
            return

        esperada = CHAINS[chain_key].eip155_id
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

    def _on_save(self) -> None:
        if self._verified != self._signature():
            return
        chain_key = str(self._chain.currentData())
        url = self._url.text().strip()
        nombre = self._key_name.text().strip()
        valor = self._key_value.text().strip()
        endpoint = RpcEndpointSettings(
            url=url,
            label=self._label.text().strip(),
            priority=self._priority.value(),
        )
        existentes = self._declared.get(chain_key, [])
        if any(ep.url == url for ep in existentes):
            self._probe_label.setText("Ya hay un nodo con esa URL en esa red.")
            return

        if nombre and valor:
            try:
                self._container.secrets.set(app_secret_key(nombre), valor)
            except SecretStoreError as error:
                self._probe_label.setText(f"No se pudo guardar la clave en el llavero: {error}")
                return

        restantes = [*existentes, endpoint]
        try:
            set_chain_endpoints(config_file(), chain_key, restantes)
        except (OSError, ValueError) as error:
            self._probe_label.setText(f"No se pudo escribir config.toml: {error}")
            return

        self._declared[chain_key] = restantes
        self._key_value.clear()
        self._verified = None
        self._save_btn.setEnabled(False)
        self._probe_label.setText("")
        self._fill_nodes_table()
        self._node_status.setText(
            "Nodo guardado en config.toml. Reinicia la aplicación para que se use."
        )
        self._refresh_apis()

    # ------------------------------------------------------------------ APIs #
    def _build_apis(self) -> Card:
        card = Card(
            "APIs de motores",
            subtitle="hosts a los que puede llamar cada motor y estado de sus claves",
        )
        self._apis_table = QTableWidget(0, 3)
        self._apis_table.setHorizontalHeaderLabels(["Motor", "Hosts permitidos", "Claves"])
        self._apis_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._apis_table.setMinimumHeight(180)
        card.body().addWidget(self._apis_table, stretch=1)
        return card

    def _refresh_apis(self) -> None:
        self._apis_table.setRowCount(0)
        for entry in self._container.registry.available():
            manifest = entry.manifest
            fila = self._apis_table.rowCount()
            self._apis_table.insertRow(fila)
            hosts = ", ".join(manifest.allowed_hosts) or "ninguno"
            claves = "; ".join(
                f"{opcion}: {self._estado_clave(secret_key(manifest.engine_id, opcion))}"
                for opcion in manifest.config_options
            )
            celdas = (
                self._container.engine_names.resolve(manifest.engine_id, manifest.name),
                hosts,
                claves or "—",
            )
            for columna, texto in enumerate(celdas):
                celda = QTableWidgetItem(texto)
                if columna == 0:
                    celda.setIcon(icons.brand_icon(manifest.engine_id))
                self._apis_table.setItem(fila, columna, celda)

    def _estado_clave(self, key: str) -> str:
        try:
            return "configurada" if self._container.secrets.get(key) else "sin configurar"
        except SecretStoreError:
            return "llavero no responde"

    def _on_credentials_changed(self) -> None:
        self._refresh_apis()
        self.credentials_changed.emit()
