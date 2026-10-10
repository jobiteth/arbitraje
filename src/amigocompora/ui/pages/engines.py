"""Pestaña: Motores — encender, apagar y nombrar cada uno.

Cada motor tiene su interruptor. Encendido queda activo en su ranura; apagado
sale de ella. Los motores de una ranura se **suman** —varios a la vez, el
preferido primero—, que es de lo que vive la comparación de precios; por eso
apagar uno no enciende otro, y por eso una ranura puede quedarse vacía a
propósito. Lo que se enciende aquí se guarda en `config.toml` y es lo que
arranca la próxima vez.

El nombre de cada motor también se puede cambiar con el lápiz de su fila: es un
alias de pantalla —el `engine_id`, su configuración y lo que hace no cambian— y
vive en `config.toml` igual que el encendido.

Las credenciales y los nodos RPC viven en la pestaña Configuración: mezclarlos
aquí hacía que «activar un motor» y «dar una clave» parecieran la misma cosa.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.app.registry import RegisteredEngine
from amigocompora.domain.protocols import EngineKind
from amigocompora.infra.config import config_file
from amigocompora.infra.config_writer import set_active_engines, set_engine_names
from amigocompora.ui import icons
from amigocompora.ui.theme import COLOR_MUTED
from amigocompora.ui.widgets import ScrollArea, Switch, spawn

#: Las columnas, por nombre y no por número: con seis posiciones repartidas por
#: media docena de sitios, buscar «la 5» es como se acaba rellenando la que no
#: era.
_COL_ID: Final = 0
_COL_NAME: Final = 1
_COL_KIND: Final = 2
_COL_VERSION: Final = 3
_COL_SOURCE: Final = 4
_COL_ACTIVE: Final = 5


class EnginesPage(QWidget):
    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container

        lay = ScrollArea.fill(self, spacing=10).body()

        # El reparto de la ranura —quién está encendido— es permanente; los
        # avisos de lo que acaba de pasar son otra cosa y van aparte, porque un
        # repintado de la tabla no puede llevarse por delante el mensaje que
        # dice qué pasó.
        self._roster = QLabel("")
        self._roster.setStyleSheet(f"color: {COLOR_MUTED};")
        self._roster.setWordWrap(True)
        lay.addWidget(self._roster)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        lay.addWidget(self._status)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["ID", "Nombre", "Ranura", "Versión", "Origen", "Activo"]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Quince motores no caben en la franja que quedaba cuando la página no se
        # desplazaba: se veía uno. Con este mínimo la tabla se lee entera, y si la
        # ventana es baja la página se desplaza.
        self._table.setMinimumHeight(240)
        # Cada columna mide su contenido y el nombre se lleva el ancho que sobra.
        # Repartidas a partes iguales, «Origen» se recorta —las procedencias son
        # largas— y el interruptor se queda con un palmo de hueco; así el ancho de
        # la ventana se reparte sólo donde hay texto que crece.
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        for columna in (_COL_ID, _COL_KIND, _COL_VERSION, _COL_SOURCE, _COL_ACTIVE):
            header.setSectionResizeMode(columna, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        # Alto de fila con aire para el interruptor, que mide veinte píxeles de
        # alto: pegado al borde, la pista deja de leerse como un mando.
        self._table.verticalHeader().setDefaultSectionSize(34)
        lay.addWidget(self._table, stretch=1)

        refresh_btn = QPushButton("Refrescar")
        refresh_btn.setObjectName("secondary")
        refresh_btn.clicked.connect(self._refresh)
        lay.addWidget(refresh_btn)

        self._refresh()

    # ------------------------------------------------------------- pintado #
    def _refresh(self) -> None:
        """Repinta la tabla entera desde el registro: él es la única verdad.

        No se parchean filas: se reconstruyen. Así un fallo al abrir un motor no
        necesita deshacer nada —el repintado deja cada interruptor donde el
        registro dice que está— y no hay dos estados que puedan separarse.
        """
        reg = self._container.registry
        nombres = self._container.engine_names
        self._table.setRowCount(0)
        catalogo = sorted(
            reg.available(),
            key=lambda entry: nombres.resolve(entry.engine_id, entry.manifest.name).lower(),
        )
        for entry in catalogo:
            r = self._table.rowCount()
            self._table.insertRow(r)

            identificador = QTableWidgetItem(entry.engine_id)
            identificador.setIcon(icons.brand_icon(entry.engine_id))
            blocked = entry.blocked_capabilities(self._container.guard)
            if blocked:
                names = ", ".join(sorted(cap.value for cap in blocked))
                hint = f"bloqueadas en {self._container.guard.mode.label}: {names}"
            else:
                hint = "todas las capacidades disponibles"
            identificador.setToolTip(hint)
            self._table.setItem(r, _COL_ID, identificador)

            self._table.setCellWidget(r, _COL_NAME, self._name_cell(entry))

            self._table.setItem(r, _COL_KIND, QTableWidgetItem(entry.manifest.kind.label))
            self._table.setItem(r, _COL_VERSION, QTableWidgetItem(entry.manifest.version))
            origen = QTableWidgetItem(_origin_label(entry))
            # La procedencia cruda —`entry-point:lifi`, `manual`— va en el
            # tooltip: la fila enseña la respuesta y el detalle queda a un
            # segundo de distancia para quien lo necesite.
            origen.setToolTip(entry.source)
            self._table.setItem(r, _COL_SOURCE, origen)

            self._table.setCellWidget(r, _COL_ACTIVE, self._switch_cell(entry))

        self._roster.setText(
            "Activos — "
            + "  ·  ".join(
                f"{kind.label}: "
                + (", ".join(e.manifest.engine_id for e in reg.active_stack(kind)) or "ninguno")
                for kind in EngineKind
            )
        )

    def _name_cell(self, entry: RegisteredEngine) -> QWidget:
        """La celda del nombre: el alias —o el de fábrica— y el lápiz que lo edita."""
        nombres = self._container.engine_names
        mostrado = nombres.resolve(entry.engine_id, entry.manifest.name)
        celda = QWidget()
        caja = QHBoxLayout(celda)
        caja.setContentsMargins(8, 0, 4, 0)
        caja.setSpacing(6)
        etiqueta = QLabel(mostrado)
        if mostrado != entry.manifest.name:
            etiqueta.setToolTip(f"Nombre de pantalla. El original es «{entry.manifest.name}».")
        caja.addWidget(etiqueta, stretch=1)
        editar = QPushButton("✎")
        editar.setObjectName("rowAction")
        editar.setFixedWidth(26)
        editar.setToolTip(
            "Renombrar este motor. Es un alias de pantalla: el id, su "
            "configuración y lo que hace no cambian."
        )
        editar.clicked.connect(lambda _=False, e=entry: self._on_rename(e))
        caja.addWidget(editar)
        return celda

    def _switch_cell(self, entry: RegisteredEngine) -> QWidget:
        """La celda del interruptor, centrado en su columna."""
        reg = self._container.registry
        celda = QWidget()
        caja = QHBoxLayout(celda)
        caja.setContentsMargins(8, 0, 8, 0)
        interruptor = Switch()
        encendido = reg.is_active(entry.engine_id)
        interruptor.setChecked(encendido)
        interruptor.setToolTip(self._switch_hint(entry, encendido=encendido))
        interruptor.clicked.connect(lambda _=False, e=entry, s=interruptor: self._on_toggle(s, e))
        caja.addWidget(interruptor, 0, Qt.AlignmentFlag.AlignCenter)
        return celda

    def _switch_hint(self, entry: RegisteredEngine, *, encendido: bool) -> str:
        ranura = entry.manifest.kind.label
        etiqueta = self._container.engine_names.resolve(entry.engine_id, entry.manifest.name)
        if encendido:
            cuantos = len(self._container.registry.active_stack(entry.manifest.kind))
            compania = (
                f" Su ranura tiene {cuantos} motores activos." if cuantos > 1 else ""
            )
            return (
                f"«{etiqueta}» está encendido en «{ranura}».{compania} Púlsalo para "
                f"apagarlo: sale de su ranura y deja de consultarse."
            )
        return (
            f"«{etiqueta}» está apagado: instalado, pero no se consulta. Púlsalo "
            f"para encenderlo: se suma a «{ranura}» sin apagar los demás."
        )

    # ------------------------------------------------------------ acciones #
    def _on_toggle(self, interruptor: Switch, entry: RegisteredEngine) -> None:
        """El clic del interruptor: abre o retira el motor, y lo deja dicho.

        La tabla se apaga mientras dura —abrir un motor puede tardar— y se
        repinta al final pase lo que pase: el registro es la única verdad, así
        que un fallo deja el interruptor donde estaba sin deshacer nada a mano.
        Lo que se enciende aquí se guarda en `config.toml` para el próximo
        arranque; si esa escritura falla, se dice, porque el encendido de esta
        sesión es real y darlo por guardado no lo sería.
        """
        encender = interruptor.isChecked()
        etiqueta = self._container.engine_names.resolve(entry.engine_id, entry.manifest.name)
        self._set_message(f"{'Encendiendo' if encender else 'Apagando'} «{etiqueta}»…")
        self._table.setEnabled(False)
        spawn(self._do_toggle(entry, encender, etiqueta))

    async def _do_toggle(
        self, entry: RegisteredEngine, encender: bool, etiqueta: str
    ) -> None:
        reg = self._container.registry
        try:
            if encender:
                await reg.add(entry.engine_id)
            else:
                await reg.remove(entry.engine_id)
        except Exception as error:
            self._set_message(
                f"No se pudo {'encender' if encender else 'apagar'} «{etiqueta}»: {error}"
            )
        else:
            aviso = self._save_slot(entry.manifest.kind)
            self._set_message(self._toggled_message(entry, encender, etiqueta) + aviso)
        finally:
            self._table.setEnabled(True)
            self._refresh()

    def _toggled_message(self, entry: RegisteredEngine, encender: bool, etiqueta: str) -> str:
        ranura = entry.manifest.kind.label
        ids = [
            e.manifest.engine_id
            for e in self._container.registry.active_stack(entry.manifest.kind)
        ]
        if encender:
            if len(ids) > 1:
                return (
                    f"«{etiqueta}» encendido. La ranura «{ranura}» queda con "
                    f"{len(ids)} motores: {', '.join(ids)}."
                )
            return f"«{etiqueta}» encendido, como único motor de «{ranura}»."
        if not ids:
            return f"«{etiqueta}» apagado: la ranura «{ranura}» queda sin motores."
        return f"«{etiqueta}» apagado. En «{ranura}» siguen: {', '.join(ids)}."

    def _save_slot(self, kind: EngineKind) -> str:
        """Deja escrita la ranura en `config.toml` y devuelve el aviso, si falla.

        Se guarda lo que la ranura tiene activo **ahora**, tal como lo ve el
        registro —y no lo que la operación creía hacer—, para que el archivo y
        el estado vivo no puedan contar cosas distintas. Una ranura vacía se
        escribe vacía, que en la carga significa «apagada a propósito».
        """
        ids = [e.manifest.engine_id for e in self._container.registry.active_stack(kind)]
        try:
            set_active_engines(config_file(), kind.value, ids)
        except Exception as error:
            return f" No se pudo guardar en config.toml ({error}): vale para esta sesión."
        return ""

    def _on_rename(self, entry: RegisteredEngine) -> None:
        """Cambia el alias de pantalla, con el diálogo y con el archivo.

        El diálogo llega con el alias vigente —vacío si no tiene— para que
        borrar el texto devuelva el nombre de fábrica: sin eso, un alias no se
        podría quitar, sólo cambiar por otro.
        """
        nombres = self._container.engine_names
        titulo, aceptado = ask_engine_name(
            self,
            original=entry.manifest.name,
            engine_id=entry.engine_id,
            current=nombres.resolve(entry.engine_id, ""),
        )
        if not aceptado:
            return
        nombres.set(entry.engine_id, titulo)
        aviso = ""
        try:
            set_engine_names(config_file(), nombres.as_dict())
        except Exception as error:
            aviso = f" No se pudo guardar en config.toml ({error}): vale para esta sesión."
        self._refresh()
        nuevo = nombres.resolve(entry.engine_id, entry.manifest.name)
        if nuevo == entry.manifest.name:
            self._set_message(
                f"«{entry.engine_id}» recupera su nombre original, «{nuevo}».{aviso}"
            )
        else:
            self._set_message(f"«{entry.engine_id}» ahora se muestra como «{nuevo}».{aviso}")

    def _set_message(self, texto: str) -> None:
        self._status.setText(texto)
        self._status.setVisible(bool(texto))


def _origin_label(entry: RegisteredEngine) -> str:
    """De dónde salió el motor, en corto.

    El texto crudo —`entry-point:lifi`, `manual`— dice la verdad pero en jerga;
    la fila enseña la respuesta a la pregunta que se le hace al mirarla —¿es de
    casa o un paquete de fuera?— y el crudo queda en su tooltip.
    """
    if entry.source.startswith("entry-point:"):
        return f"paquete: {entry.source.split(':', 1)[1]}"
    return "incluido"


def ask_engine_name(
    parent: QWidget, *, original: str, engine_id: str, current: str
) -> tuple[str, bool]:
    """El diálogo del alias: pide el nombre y devuelve «texto, aceptado».

    Es una función aparte y no dos líneas dentro de `_on_rename` para que se
    pueda sustituir en las pruebas: lo que hay que medir es lo que viene después
    —normalizar, guardar, decidir qué se cuenta—, y un modal de verdad
    bloquearía la prueba que lo mide.
    """
    return QInputDialog.getText(
        parent,
        "Renombrar motor",
        f"Nombre de pantalla para «{original}» ({engine_id}).\n"
        "Déjalo vacío para volver al nombre original:",
        text=current,
    )
