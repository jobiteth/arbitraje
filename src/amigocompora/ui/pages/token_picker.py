"""El selector de tokens: un modal con buscador, saldos e importación por dirección.

### Por qué un modal y no un desplegable

Elegir el token es **el** gesto de esta pantalla, y un `QComboBox` no puede
enseñar lo único que hace falta para elegirlo bien: cuánto tienes de cada uno y
cuánto vale. Con cuatro tokens del catálogo la diferencia no se notaba; en cuanto
la lista incluye lo que trae la cartera —que en Solana son todos los mints de la
cuenta— un desplegable de cien entradas sin saldos es una lista donde se elige a
ciegas.

Aquí se ve lo que se tiene, ordenado por lo que pesa, y buscar por nombre,
símbolo o **dirección** funciona sobre la lista entera.

### La importación por dirección se delega, no se hace aquí

Un token que no está en la lista se pega, y para traerlo hay que **leer su
contrato** —símbolo y decimales, que no se piden a mano—. Eso es una petición de
red, y una petición de red desde dentro de un `exec()` modal no avanza: el bucle
anidado que abre `QDialog.exec()` no es el de `qasync`, así que una tarea lanzada
ahí dentro se queda esperando a que el diálogo se cierre.

Por eso el diálogo devuelve la **dirección a importar** y quien lo abrió hace el
trabajo con su propio camino asíncrono, que ya existe y ya sabe decirlo: es el
mismo `_do_lookup_token` que usa el botón «+ Token…». Cuando termina, la lista se
reabre con el token ya dentro y seleccionado.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
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
from amigocompora.domain.chains import CHAINS
from amigocompora.domain.models import Token
from amigocompora.ui import icons
from amigocompora.ui.theme import COLOR_MUTED, COLOR_WARNING
from amigocompora.ui.wallet_state import WalletBalances
from amigocompora.ui.widgets import format_amount, tokens_for_chain

#: Cuántos caracteres hexadecimales tiene una dirección EVM sin el `0x`.
_EVM_BODY: int = 40


def looks_like_address(text: str) -> bool:
    """Si lo pegado es una dirección de contrato y no un nombre.

    Se comprueba la **forma** y no la red: esto sólo decide si se ofrece importar,
    y ofrecerlo de más no hace daño —la lectura del contrato rechazará lo que no
    sea un contrato—, mientras que no ofrecerlo cuando el usuario acaba de pegar
    una dirección sí: es exactamente el gesto que se está intentando hacer.
    """
    body = text.strip().removeprefix("0x").removeprefix("0X")
    return len(body) >= 32 and all(c in "0123456789abcdefABCDEF" for c in body)


class TokenPickerDialog(QDialog):
    """Elige un token de la red: con su saldo, su valor y buscador.

    Devuelve una de dos cosas, y nunca las dos: un `Token` ya elegido
    (`chosen()`) o una dirección que hay que importar (`import_address()`).
    """

    def __init__(
        self,
        container: Container,
        chain_key: str,
        balances: WalletBalances,
        *,
        selected: Token | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._chain_key = chain_key
        self._balances = balances
        self._selected = selected
        self._chosen: Token | None = None
        self._to_import: str | None = None

        self.setWindowTitle(f"Elegir token — {CHAINS[chain_key].name}")
        self.setModal(True)
        self.setMinimumSize(520, 460)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Buscar por nombre, símbolo o dirección…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._repaint)
        lay.addWidget(self._search)

        #: El aviso de lo que se puede hacer con lo pegado. Va **encima** de la
        #: lista y no al lado del botón: quien pega una dirección mira arriba.
        self._notice = QLabel("")
        self._notice.setObjectName("hint")
        self._notice.setWordWrap(True)
        self._notice.setVisible(False)
        lay.addWidget(self._notice)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Token", "Saldo", "Valor"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        cabecera = self._table.horizontalHeader()
        cabecera.setSectionResizeMode(0, QHeaderView.Stretch)
        cabecera.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        cabecera.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_selection)
        self._table.itemDoubleClicked.connect(lambda _item: self._accept())
        lay.addWidget(self._table, 1)

        fila = QHBoxLayout()
        self._import_btn = QPushButton("Importar token")
        self._import_btn.setObjectName("secondary")
        self._import_btn.setVisible(False)
        self._import_btn.clicked.connect(self._on_import)
        fila.addWidget(self._import_btn)
        fila.addStretch()
        lay.addLayout(fila)

        caja = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        caja.button(QDialogButtonBox.Ok).setText("Elegir")
        caja.button(QDialogButtonBox.Cancel).setText("Cancelar")
        caja.accepted.connect(self._accept)
        caja.rejected.connect(self.reject)
        self._buttons = caja
        lay.addWidget(caja)

        self._repaint()

    # ------------------------------------------------------------------ #
    # Lo que devuelve
    # ------------------------------------------------------------------ #
    def chosen(self) -> Token | None:
        """El token elegido, o `None` si se cerró sin elegir."""
        return self._chosen

    def import_address(self) -> str | None:
        """La dirección que se pidió importar, o `None`.

        Es una respuesta **distinta** de «un token»: todavía no hay token, hay una
        dirección que puede no ser un contrato. Quien la reciba tiene que leerla de
        la cadena antes de que exista nada que enseñar, así que no puede
        confundirse con una elección ya hecha.
        """
        return self._to_import

    # ------------------------------------------------------------------ #
    # La lista
    # ------------------------------------------------------------------ #
    def _tokens(self) -> tuple[Token, ...]:
        return tokens_for_chain(self._container.token_store, self._chain_key)

    def _repaint(self) -> None:
        """Rellena la tabla con lo que pasa el filtro, y ordena por lo que pesa.

        El orden no es cosmético: la lista puede traer cien mints y lo que se viene
        a hacer es elegir uno de los cuatro que tienen fondos. Los que tienen saldo
        van primero, y entre ellos de mayor a menor valor; los demás después, por
        símbolo, que es un orden estable que no baila entre dos repintados.
        """
        filtro = self._search.text().strip().lower()
        visibles = [t for t in self._tokens() if self._matches(t, filtro)]
        visibles.sort(key=self._order)

        self._table.setRowCount(0)
        for token in visibles:
            self._add_row(token)

        self._refresh_import(filtro)
        self._refresh_ok()

    def _matches(self, token: Token, filtro: str) -> bool:
        """Símbolo, nombre cualificado o **dirección**. Igual que busca la cartera.

        Se usa la misma tripleta que `WalletPage._matches` a propósito: dos
        buscadores que contestan distinto a la misma pregunta son dos verdades
        sobre lo mismo, y la que se desvía es siempre la que nadie mira.
        """
        if not filtro:
            return True
        aguja = filtro.removeprefix("0x")
        candidatos = {token.symbol.lower(), token.qualified_symbol.lower()}
        if token.address:
            candidatos.add(token.address.lower())
            candidatos.add(token.address.lower().removeprefix("0x"))
        return any(aguja in candidato for candidato in candidatos)

    def _order(self, token: Token) -> tuple[int, float, str]:
        holding = self._balances.find(self._chain_key, token)
        sin_saldo = holding is None or holding.is_empty
        valor = 0.0
        if holding is not None and holding.value_in_reference is not None:
            valor = float(holding.value_in_reference.as_decimal())
        # `-valor` para que lo que más vale salga arriba dentro del grupo.
        return (1 if sin_saldo else 0, -valor, token.symbol.lower())

    def _add_row(self, token: Token) -> None:
        fila = self._table.rowCount()
        self._table.insertRow(fila)

        nombre = QTableWidgetItem(token.qualified_symbol)
        nombre.setIcon(icons.token_icon(token.display_symbol))
        nombre.setData(Qt.UserRole, token)
        if token.is_native:
            nombre.setToolTip("Es la moneda de la red: paga el gas y no necesita aprobación.")
        elif token.address:
            nombre.setToolTip(token.address)
        # Un token que la política no dejaría firmar se marca aquí, en la lista,
        # y no al pulsar el botón que firma: descubrirlo al final es descubrirlo
        # tarde. Cotizar con él sí se puede, y por eso se marca en gris en vez de
        # desaparecer.
        if not self._allowed(token):
            nombre.setForeground(QColor(COLOR_MUTED))
            nombre.setToolTip(
                f"«{token.symbol}» no está en `allowed_tokens`, así que la política "
                f"no dejará firmar una operación que lo toque. Se puede cotizar "
                f"igual: cotizar no mueve dinero."
            )
        self._table.setItem(fila, 0, nombre)

        holding = self._balances.find(self._chain_key, token)
        if holding is None:
            # Sin lectura de esa red no se escribe un cero: no es lo mismo «no
            # tienes» que «no se pudo leer», y un cero aquí se lee como lo primero.
            saldo = QTableWidgetItem("—")
            saldo.setForeground(QColor(COLOR_MUTED))
            saldo.setToolTip(
                "No se pudo leer el saldo de esta red, así que no se sabe cuánto "
                "tienes. No es cero."
            )
        else:
            saldo = QTableWidgetItem(format_amount(holding.as_decimal()))
            saldo.setToolTip(f"{holding.as_decimal():f} {token.symbol}")
        saldo.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._table.setItem(fila, 1, saldo)

        if holding is not None and holding.value_in_reference is not None:
            referencia = holding.value_in_reference
            valor = QTableWidgetItem(
                f"{format_amount(referencia.as_decimal())} {referencia.symbol}"
            )
            valor.setToolTip(f"{referencia.as_decimal():f} {referencia.symbol}")
        elif holding is not None and not holding.is_empty:
            valor = QTableWidgetItem("sin cotización")
            valor.setForeground(QColor(COLOR_WARNING))
            valor.setToolTip(
                "Esta posición tiene fondos y ningún motor activo cotiza este token "
                "contra la stablecoin de su red: no vale cero, vale «no se sabe»."
            )
        else:
            valor = QTableWidgetItem("—")
            valor.setForeground(QColor(COLOR_MUTED))
        valor.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._table.setItem(fila, 2, valor)

        if self._selected is not None and token.is_same_asset(self._selected):
            self._table.selectRow(fila)

    def _allowed(self, token: Token) -> bool:
        limites = self._container.policy.limits
        # Vacía no marca nada —está a medio configurar, no roto—; con lista,
        # decide `allows_token`, que es lo que decidirá el caso de uso al firmar:
        # así un `pUSD` con la lista diciendo `PUSD` no sale en gris.
        return not limites.allowed_tokens or limites.allows_token(token.symbol)

    def _refresh_import(self, filtro: str) -> None:
        """Ofrece importar lo pegado cuando es una dirección que no está en la lista.

        Se compara contra la lista **entera** y no contra la filtrada: una
        dirección que ya existe y no aparece en pantalla no hay que importarla, hay
        que encontrarla.
        """
        texto = self._search.text().strip()
        ya_esta = any(
            t.address is not None and t.address.lower() == texto.lower()
            for t in self._tokens()
        )
        ofrecer = bool(texto) and looks_like_address(texto) and not ya_esta
        self._to_import = texto if ofrecer else None
        self._import_btn.setVisible(ofrecer)
        if not ofrecer:
            self._notice.setVisible(False)
            return
        self._import_btn.setText(f"Importar {texto[:10]}… en {CHAINS[self._chain_key].name}")
        self._notice.setText(
            f"«{texto[:14]}…» no está en la lista. Se puede importar leyendo su "
            f"contrato: el símbolo y los decimales se leen de la cadena, no se "
            f"escriben a mano."
        )
        self._notice.setVisible(True)

    def _refresh_ok(self) -> None:
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(self._row_token() is not None)

    # ------------------------------------------------------------------ #
    # Acciones
    # ------------------------------------------------------------------ #
    def _row_token(self) -> Token | None:
        filas = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not filas:
            return None
        item = self._table.item(filas[0].row(), 0)
        if item is None:
            return None
        dato = item.data(Qt.UserRole)
        return dato if isinstance(dato, Token) else None

    def _on_selection(self) -> None:
        self._refresh_ok()

    def _accept(self) -> None:
        token = self._row_token()
        if token is None:
            return
        self._chosen = token
        self.accept()

    def _on_import(self) -> None:
        """Cierra devolviendo la dirección. Quien abrió esto hará la lectura."""
        if self._to_import is None:
            return
        self.reject()
