"""Pestaña: Configuración — nodos RPC, APIs de motores y credenciales.

La pestaña son tres subpestañas, y cada credencial vive en **un solo sitio**:

- **Nodos RPC** — los endpoints declarados de cada red y sus respaldos públicos.
  Un nodo se agrega o se edita en su ventana (`ui/node_dialog.py`), que prueba
  antes de guardar; la clave de un nodo —la que viaja dentro de su URL como
  `${NOMBRE}`— se escribe ahí mismo, junto al nodo que la usa.
- **APIs de motores** — las claves y el modelo de cada motor (`ui/pages/apis.py`).
  Antes esto estaba en dos sitios a la vez: una tabla de sólo lectura aquí y una
  casilla por opción en la tarjeta de credenciales. Dos vistas del mismo dato se
  separan en cuanto una cambia, así que ahora hay una.
- **Credenciales** — lo de la aplicación: la clave que firma, la frase de la
  ejecución desatendida y las de Polymarket (`ui/pages/credentials.py`).

Las tablas reparten el ancho como la de motores: cada columna mide su contenido
y el texto largo —la URL— se lleva lo que sobra, en vez de recortarse todas por
igual.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.chains import CHAINS
from amigocompora.infra.config import RpcEndpointSettings, config_file
from amigocompora.infra.config_writer import set_chain_endpoints
from amigocompora.infra.logging import safe_url
from amigocompora.infra.secrets import (
    SecretStoreError,
    app_env_var_name,
    app_secret_key,
    placeholder_names,
)
from amigocompora.ui import icons
from amigocompora.ui.node_dialog import NodeDialog
from amigocompora.ui.pages.apis import ApiKeysPanel
from amigocompora.ui.pages.credentials import CredentialsCard, rpc_secret_usage
from amigocompora.ui.theme import COLOR_MUTED
from amigocompora.ui.widgets import Card, ScrollArea

_ORIGEN_CONFIG = "config.toml"
_ORIGEN_PUBLICO = "respaldo público"

#: Las redes EVM por nombre, en un solo sitio: el desplegable de la ventana y la
#: red preseleccionada del alta tienen que ofrecer el mismo orden.
_EVM_CHAINS = tuple(sorted((s for s in CHAINS.values() if s.is_evm), key=lambda s: s.name))

#: Las columnas de la tabla de nodos, por nombre. Las URL son largas y las demás
#: cortas, así que una sola de ellas se estira.
_COL_CHAIN = 0
_COL_LABEL = 1
_COL_URL = 2
_COL_PRIORITY = 3
_COL_ORIGIN = 4
_COL_KEY = 5
_COL_ACTIONS = 6


class ConfiguracionPage(QWidget):
    #: Se reenvían las señales de las credenciales: quien firma escucha aquí, y
    #: no necesita saber que las credenciales viven en una subpestaña.
    credentials_changed = Signal()

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container

        tabs = QTabWidget()
        self._nodes = NodesPanel(container, self)
        tabs.addTab(self._nodes, "Nodos RPC")
        self._apis = ApiKeysPanel(container, self)
        tabs.addTab(self._apis, "APIs de motores")
        self._credentials = CredentialsCard(container, self)
        self._credentials.credentials_changed.connect(self._on_credentials_changed)
        tabs.addTab(self._credentials, "Credenciales")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(tabs)

    def _on_credentials_changed(self) -> None:
        """Repinta lo que enseña estados de claves y avisa a quien firma.

        Guardar «otra credencial» en la tarjeta puede ser la clave que un nodo
        está esperando: su estado tiene que cambiar sin salir de la pestaña.
        """
        self._apis.refresh()
        self._nodes.refresh_keys()
        self.credentials_changed.emit()


class NodesPanel(QWidget):
    """La tabla de nodos y su ventana: agregar, editar y eliminar."""

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

        lay = ScrollArea.fill(self, spacing=12).body()

        card = Card(
            "Nodos RPC",
            subtitle="los nodos se aplican al arrancar: tras guardar, reinicia la aplicación",
        )

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(
            ["Red", "Etiqueta", "URL", "Prioridad", "Origen", "Clave", ""]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(220)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        # Cada columna mide su contenido; la URL se lleva el ancho que sobra.
        # Repartidas a partes iguales, la URL se recortaba y «Origen» se quedaba
        # con un palmo de hueco: el ancho de la ventana se reparte donde hay
        # texto que crece, no donde no lo hay.
        for columna in (
            _COL_CHAIN,
            _COL_LABEL,
            _COL_PRIORITY,
            _COL_ORIGIN,
            _COL_KEY,
            _COL_ACTIONS,
        ):
            header.setSectionResizeMode(columna, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_URL, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(32)
        card.body().addWidget(self._table, stretch=1)

        botones = QHBoxLayout()
        agregar = QPushButton("Agregar nodo…")
        agregar.setToolTip(
            "Abre la ventana de un nodo nuevo. Sólo se guarda si la prueba "
            "(eth_chainId) responde con la red elegida."
        )
        agregar.clicked.connect(self._on_add_node)
        botones.addWidget(agregar)
        botones.addStretch(1)
        card.body().addLayout(botones)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        card.body().addWidget(self._status)

        lay.addWidget(card)
        self._fill_table()

    # -------------------------------------------------------------- pintado #
    def _fill_table(self) -> None:
        self._table.setRowCount(0)
        for chain_key in sorted(set(self._declared) | set(self._public)):
            nombre = CHAINS[chain_key].name if chain_key in CHAINS else chain_key
            for indice, endpoint in enumerate(self._declared.get(chain_key, [])):
                self._add_row(nombre, chain_key, endpoint, _ORIGEN_CONFIG, indice)
            for endpoint in self._public.get(chain_key, ()):
                self._add_row(nombre, chain_key, endpoint, _ORIGEN_PUBLICO, None)

    def _add_row(
        self,
        nombre: str,
        chain_key: str,
        endpoint: RpcEndpointSettings,
        origen: str,
        indice: int | None,
    ) -> None:
        fila = self._table.rowCount()
        self._table.insertRow(fila)
        celdas = (
            nombre,
            endpoint.label or "—",
            safe_url(endpoint.url),
            str(endpoint.priority),
            origen,
            self._key_state_text(endpoint),
        )
        for columna, texto in enumerate(celdas):
            self._table.setItem(fila, columna, QTableWidgetItem(texto))
        # El icono de la red en la primera celda. El texto no se toca: la columna
        # dice la red y hay filas cuya clave no está en el catálogo (se enseña la
        # clave cruda), así que el icono es un extra que puede faltar.
        red = self._table.item(fila, _COL_CHAIN)
        if red is not None:
            red.setIcon(icons.network_icon(chain_key))
        clave = self._table.item(fila, _COL_KEY)
        if clave is not None:
            clave.setToolTip(self._key_state_tooltip(chain_key, endpoint))
        if indice is not None:
            self._table.setCellWidget(
                fila, _COL_ACTIONS, self._actions_cell(chain_key, endpoint, indice)
            )

    def _actions_cell(
        self, chain_key: str, endpoint: RpcEndpointSettings, indice: int
    ) -> QWidget:
        """Los botones de la fila: editar y eliminar. Sólo en los nodos propios."""
        celda = QWidget()
        caja = QHBoxLayout(celda)
        caja.setContentsMargins(4, 0, 4, 0)
        caja.setSpacing(4)

        editar = QPushButton("✎")
        editar.setObjectName("rowAction")
        editar.setFixedWidth(26)
        editar.setToolTip("Editar este nodo. Si cambias la URL o la clave, habrá que probarlo.")
        editar.clicked.connect(lambda _=False, c=chain_key, i=indice: self._on_edit_node(c, i))
        caja.addWidget(editar)

        eliminar = QPushButton("✕")
        eliminar.setObjectName("rowAction")
        eliminar.setFixedWidth(26)
        eliminar.setToolTip(
            "Eliminar este nodo de config.toml. Su clave, si la tenía, sigue en el "
            "llavero: se borra desde la tarjeta de credenciales."
        )
        eliminar.clicked.connect(lambda _=False, c=chain_key, i=indice: self._on_delete_node(c, i))
        caja.addWidget(eliminar)
        return celda

    def _key_state_text(self, endpoint: RpcEndpointSettings) -> str:
        nombres = placeholder_names(endpoint.url)
        if not nombres:
            return "—"
        return "; ".join(f"{nombre}: {self._estado_clave(nombre)}" for nombre in nombres)

    def _key_state_tooltip(self, chain_key: str, endpoint: RpcEndpointSettings) -> str:
        nombres = placeholder_names(endpoint.url)
        if not nombres:
            return "Este nodo no lleva clave: su URL no referencia ninguna credencial."
        uso = rpc_secret_usage(self._container.settings)
        partes = []
        for nombre in nombres:
            donde = ", ".join(uso.get(nombre, ())) or "ningún nodo de config.toml"
            partes.append(
                f"«{nombre}» va dentro de la URL de {donde}; se guarda como "
                f"«{app_secret_key(nombre)}» y su variable de entorno equivalente "
                f"{app_env_var_name(nombre)} se lee siempre al resolver los nodos. "
                f"Se escribe abriendo la ventana de este nodo (✎)."
            )
        return "\n\n".join(partes)

    def _estado_clave(self, nombre: str) -> str:
        try:
            presente = bool(self._container.secrets.get(app_secret_key(nombre)))
        except SecretStoreError:
            return "llavero no responde"
        return "configurada" if presente else "sin configurar"

    def refresh_keys(self) -> None:
        """Repinta sólo los estados de clave: se guardó (o borró) una credencial."""
        for fila in range(self._table.rowCount()):
            referencia = self._declared_ref(fila)
            if referencia is None:
                continue
            chain_key, indice = referencia
            endpoint = self._declared[chain_key][indice]
            clave = self._table.item(fila, _COL_KEY)
            if clave is not None:
                clave.setText(self._key_state_text(endpoint))

    def _declared_ref(self, fila: int) -> tuple[str, int] | None:
        """El nodo declarado de esa fila, o `None` si la fila es de un respaldo.

        Se recorre el mismo orden con el que `_fill_table` reparte las filas
        —cada red, primero sus declarados y después sus públicos— porque la tabla
        no guarda a qué nodo corresponde cada una: lo sabe su posición.
        """
        posicion = 0
        for chain_key in sorted(set(self._declared) | set(self._public)):
            declarados = self._declared.get(chain_key, [])
            if fila < posicion + len(declarados):
                return chain_key, fila - posicion
            posicion += len(declarados) + len(self._public.get(chain_key, ()))
        return None

    # ------------------------------------------------------------ acciones #
    def _on_add_node(self) -> None:
        self._open_dialog(self._default_chain(), None)

    def _on_edit_node(self, chain_key: str, indice: int) -> None:
        self._open_dialog(chain_key, indice)

    def _default_chain(self) -> str:
        """La red preseleccionada al agregar: la primera del desplegable."""
        return _EVM_CHAINS[0].key

    def _open_dialog(self, chain_key: str, indice: int | None) -> None:
        dialog = NodeDialog(
            self._container,
            self,
            chain_key=chain_key,
            endpoints=self._declared.get(chain_key, []),
            index=indice,
        )
        if dialog.exec() != NodeDialog.DialogCode.Accepted or dialog.endpoints is None:
            return
        self._declared[chain_key] = list(dialog.endpoints)
        self._fill_table()
        verbo = "guardado" if indice is None else "actualizado"
        aviso = " La clave quedó en el llavero." if dialog.key_saved else ""
        self._status.setText(
            f"Nodo {verbo} en config.toml. Reinicia la aplicación para que se use.{aviso}"
        )

    def _on_delete_node(self, chain_key: str, indice: int) -> None:
        declarados = self._declared.get(chain_key, [])
        if not 0 <= indice < len(declarados):
            return
        restantes = [ep for i, ep in enumerate(declarados) if i != indice]
        try:
            set_chain_endpoints(config_file(), chain_key, restantes)
        except (OSError, ValueError) as error:
            self._status.setText(f"No se pudo escribir config.toml: {error}")
            return
        self._declared[chain_key] = restantes
        self._fill_table()
        self._status.setText(
            "Nodo eliminado de config.toml. Su clave, si la tenía, sigue en el llavero."
        )
