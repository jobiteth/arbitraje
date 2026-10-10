"""Pestaña: Configuración → APIs de motores — claves y modelo, por motor.

Antes esto estaba repartido en dos vistas del mismo dato: una tabla de sólo
lectura en Configuración y una casilla por opción —una por motor registrado— en
la tarjeta de credenciales. Dos vistas del mismo dato no son redundancia, son
dos verdades que se separan en cuanto una cambia; aquí queda una sola, y editar
es abrir la fila.

Las claves se guardan en el llavero del sistema —nunca en `config.toml`— como
`engine_id:opción`, y el registro de motores las resuelve al abrir el motor.
Guardar una clave aquí **no** enciende el motor: eso se decide en la pestaña
Motores, y se dice, porque una clave guardada que nadie usa parece estar
funcionando.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.app.registry import RegisteredEngine
from amigocompora.infra.secrets import (
    SecretStoreError,
    env_var_name,
    secret_key,
)
from amigocompora.ui import icons
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED
from amigocompora.ui.widgets import Card, ScrollArea

#: Las columnas de la tabla, por nombre y no por número: con cinco posiciones
#: repartidas por media docena de sitios, buscar «la 3» es como se acaba
#: rellenando la que no era.
_COL_ENGINE = 0
_COL_KIND = 1
_COL_CONFIG = 2
_COL_ACTIVE = 3
_COL_ACTIONS = 4

#: Las opciones que se enseñan **en claro** al editarlas. Todo lo demás va
#: enmascarado por omisión: una opción nueva de un motor nuevo se escribe sin
#: enseñarla, y eso es lo que se quiere cuando el nombre no promete nada. Ésta
#: es la única excepción conocida —el modelo del LLM—, y su nombre se repite
#: aquí porque la lista de «no secretos» es una decisión de la interfaz.
_OPCIONES_EN_CLARO = frozenset({"model"})


class ApiKeysPanel(QWidget):
    """La tabla de motores con sus claves, y la ventana para editarlas."""

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container

        lay = ScrollArea.fill(self, spacing=12).body()

        card = Card(
            "APIs de motores",
            subtitle="las claves van al llavero del sistema, nunca a config.toml",
        )

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Motor", "Ranura", "Configuración", "Estado", ""]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(240)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        for columna in (_COL_ENGINE, _COL_KIND, _COL_ACTIVE, _COL_ACTIONS):
            header.setSectionResizeMode(columna, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_CONFIG, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(32)
        card.body().addWidget(self._table, stretch=1)

        hint = QLabel(
            "Guardar una clave aquí no enciende el motor: eso se decide en la "
            "pestaña Motores. El copiloto usa el motor de IA que esté encendido; "
            "su modelo es la opción «model» de cada fila —en blanco, el de fábrica."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        card.body().addWidget(hint)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        card.body().addWidget(self._status)

        lay.addWidget(card)
        self.refresh()

    # -------------------------------------------------------------- pintado #
    def refresh(self) -> None:
        """Repinta la tabla entera desde el registro: él es la única verdad."""
        reg = self._container.registry
        nombres = self._container.engine_names
        self._table.setRowCount(0)
        catalogo = sorted(
            reg.available(),
            key=lambda entry: nombres.resolve(entry.engine_id, entry.manifest.name).lower(),
        )
        for entry in catalogo:
            manifest = entry.manifest
            fila = self._table.rowCount()
            self._table.insertRow(fila)

            nombre = QTableWidgetItem(nombres.resolve(manifest.engine_id, manifest.name))
            nombre.setIcon(icons.brand_icon(manifest.engine_id))
            self._table.setItem(fila, _COL_ENGINE, nombre)
            self._table.setItem(fila, _COL_KIND, QTableWidgetItem(manifest.kind.label))

            configuracion = QTableWidgetItem(self._config_text(entry))
            configuracion.setToolTip(self._config_tooltip(entry))
            self._table.setItem(fila, _COL_CONFIG, configuracion)

            encendido = reg.is_active(manifest.engine_id)
            estado = QTableWidgetItem("encendido" if encendido else "apagado")
            estado.setToolTip(
                "Encendido: sus claves se resolvieron al arrancarlo desde el registro.",
            )
            if not encendido:
                estado.setToolTip(
                    "Apagado: instalado, pero no se consulta. Se enciende en la pestaña Motores.",
                )
            self._table.setItem(fila, _COL_ACTIVE, estado)

            if manifest.config_options:
                self._table.setCellWidget(fila, _COL_ACTIONS, self._actions_cell(entry))

    def _actions_cell(self, entry: RegisteredEngine) -> QWidget:
        celda = QWidget()
        caja = QHBoxLayout(celda)
        caja.setContentsMargins(4, 0, 4, 0)
        editar = QPushButton("✎")
        editar.setObjectName("rowAction")
        editar.setFixedWidth(26)
        editar.setToolTip(
            "Editar las claves y el modelo de este motor: se guardan en el llavero."
        )
        editar.clicked.connect(lambda _=False, e=entry: self._on_edit(e))
        caja.addWidget(editar)
        return celda

    def _config_text(self, entry: RegisteredEngine) -> str:
        manifest = entry.manifest
        if not manifest.config_options:
            return "—"
        return "; ".join(
            f"{opcion}: {self._option_state(manifest.engine_id, opcion)}"
            for opcion in manifest.config_options
        )

    def _config_tooltip(self, entry: RegisteredEngine) -> str:
        manifest = entry.manifest
        if not manifest.config_options:
            return "Este motor no necesita claves: funciona sin configuración."
        partes = []
        for opcion in manifest.config_options:
            obligatoria = opcion in manifest.required_config
            partes.append(
                f"«{opcion}» ({'obligatoria' if obligatoria else 'opcional'}): "
                f"credencial «{secret_key(manifest.engine_id, opcion)}»; variable "
                f"equivalente {env_var_name(manifest.engine_id, opcion)}, que el "
                f"registro de motores lee del entorno siempre. Se escribe con el "
                f"lápiz de la fila."
            )
        return "\n\n".join(partes)

    def _option_state(self, engine_id: str, opcion: str) -> str:
        try:
            valor = self._container.secrets.get(secret_key(engine_id, opcion))
        except SecretStoreError:
            return "llavero no responde"
        if opcion in _OPCIONES_EN_CLARO:
            return valor or "por omisión del motor"
        return "configurada" if valor else "sin configurar"

    # ------------------------------------------------------------ acciones #
    def _on_edit(self, entry: RegisteredEngine) -> None:
        dialog = ApiKeyDialog(self._container, entry, self)
        if dialog.exec() != ApiKeyDialog.DialogCode.Accepted:
            return
        self.refresh()
        nombre = self._container.engine_names.resolve(entry.engine_id, entry.manifest.name)
        if dialog.saved:
            self._status.setText(
                f"Claves de «{nombre}» guardadas en el llavero. "
                + self._start_hint(entry)
            )
        elif dialog.deleted:
            self._status.setText(f"Claves de «{nombre}» borradas del llavero.")

    def _start_hint(self, entry: RegisteredEngine) -> str:
        """Qué falta para que lo guardado sirva, según lo que el registro ve."""
        faltones = [
            opcion
            for opcion in entry.manifest.required_config
            if self._option_state(entry.manifest.engine_id, opcion) != "configurada"
        ]
        if faltones:
            return (
                f"Todavía falta {' y '.join(faltones)}: sin eso este motor no arranca."
            )
        if not self._container.registry.is_active(entry.engine_id):
            return (
                "Si el motor estaba apagado por falta de clave, enciéndelo en la "
                "pestaña Motores."
            )
        return "El motor ya estaba encendido: lo configurado se aplica al reabrirlo."


class ApiKeyDialog(QDialog):
    """Las opciones de un motor: estado, campo y guardar —o borrar lo guardado.

    Un campo en blanco **no** borra lo que ya estaba: dejarlo en blanco es «no
    tocar esta opción», y para quitar lo guardado está su botón. Confundir las
    dos cosas convertiría cerrar la ventana deprisa en perder una clave.
    """

    def __init__(
        self,
        container: Container,
        entry: RegisteredEngine,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._entry = entry
        manifest = entry.manifest
        nombre = container.engine_names.resolve(manifest.engine_id, manifest.name)

        #: Lo que el panel lee tras `accept()`: qué opciones se guardaron y
        #: cuáles se borraron.
        self.saved: tuple[str, ...] = ()
        self.deleted: tuple[str, ...] = ()

        self.setWindowTitle(f"Configurar «{nombre}»")
        self.setModal(True)
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(18, 16, 18, 16)

        resumen = QLabel(manifest.summary)
        resumen.setObjectName("hint")
        resumen.setWordWrap(True)
        lay.addWidget(resumen)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self._fields: dict[str, QLineEdit] = {}
        self._states: dict[str, QLabel] = {}
        for fila, opcion in enumerate(manifest.config_options):
            obligatoria = opcion in manifest.required_config
            titulo = QLabel(f"{opcion} ({'obligatoria' if obligatoria else 'opcional'}):")
            titulo.setToolTip(
                f"Credencial «{secret_key(manifest.engine_id, opcion)}»; variable "
                f"equivalente {env_var_name(manifest.engine_id, opcion)}, que el "
                f"registro de motores lee del entorno siempre."
            )
            grid.addWidget(titulo, fila, 0)

            estado = QLabel("")
            estado.setObjectName("hint")
            grid.addWidget(estado, fila, 1)

            campo = QLineEdit()
            if opcion in _OPCIONES_EN_CLARO:
                campo.setPlaceholderText("en blanco: el de fábrica del motor")
            else:
                campo.setEchoMode(QLineEdit.EchoMode.Password)
                campo.setPlaceholderText("pegar aquí y pulsar Guardar")
            grid.addWidget(campo, fila, 2)
            self._fields[opcion] = campo
            self._states[opcion] = estado
        lay.addLayout(grid)

        nota = QLabel(
            "Un campo en blanco no borra lo guardado: se conserva lo que ya había. "
            "Para quitar las claves de este motor está «Borrar las guardadas»."
        )
        nota.setObjectName("hint")
        nota.setWordWrap(True)
        lay.addWidget(nota)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(self._status)

        buttons = QDialogButtonBox()
        self._delete_btn = buttons.addButton(
            "Borrar las guardadas", QDialogButtonBox.ButtonRole.ActionRole
        )
        self._delete_btn.setObjectName("secondary")
        guardar = buttons.addButton("Guardar", QDialogButtonBox.ButtonRole.AcceptRole)
        cancelar = buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        cancelar.setObjectName("secondary")
        guardar.clicked.connect(self._on_save)
        self._delete_btn.clicked.connect(self._on_delete)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._refresh_states()

    def _refresh_states(self) -> None:
        """Repinta el estado de cada opción. **Del valor sólo se publica si está.**

        La excepción es el modelo: no es un secreto —viaja en cada petición— y
        quien lo edita necesita ver cuál está en uso, así que su estado es el
        propio valor.
        """
        hay_guardadas = False
        for opcion, estado in self._states.items():
            try:
                valor = self._container.secrets.get(
                    secret_key(self._entry.engine_id, opcion)
                )
            except SecretStoreError:
                estado.setText("el llavero no responde")
                estado.setStyleSheet(f"color: {COLOR_DANGER};")
                continue
            if valor:
                hay_guardadas = True
            if opcion in _OPCIONES_EN_CLARO:
                estado.setText(valor or "sin configurar")
            else:
                estado.setText("configurada" if valor else "sin configurar")
            estado.setStyleSheet("" if valor else f"color: {COLOR_MUTED};")
        self._delete_btn.setEnabled(hay_guardadas)

    def _on_save(self) -> None:
        guardadas: list[str] = []
        for opcion, campo in self._fields.items():
            valor = campo.text().strip()
            if not valor:
                continue
            try:
                self._container.secrets.set(
                    secret_key(self._entry.engine_id, opcion), valor
                )
            except SecretStoreError as error:
                self._status.setText(f"No se pudo guardar «{opcion}»: {error}")
                return
            finally:
                # El campo se vacía **siempre**, incluso si falló: dejar la clave
                # escrita en un widget es dejarla en una pantalla.
                campo.clear()
            guardadas.append(opcion)
        if not guardadas:
            self._status.setText("No hay nada que guardar: los campos están vacíos.")
            return
        self.saved = tuple(guardadas)
        self.accept()

    def _on_delete(self) -> None:
        borradas: list[str] = []
        for opcion in self._fields:
            key = secret_key(self._entry.engine_id, opcion)
            try:
                if self._container.secrets.get(key) is not None:
                    self._container.secrets.delete(key)
                    borradas.append(opcion)
            except SecretStoreError as error:
                self._status.setText(f"No se pudo borrar «{opcion}»: {error}")
                self._refresh_states()
                return
        self._refresh_states()
        self.deleted = tuple(borradas)
        self.accept()
