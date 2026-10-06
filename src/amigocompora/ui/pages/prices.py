"""Pestaña: Cotizaciones DEX, oportunidades, y ejecución real.

La página tiene dos caminos con consecuencias opuestas y el usuario tiene que
poder distinguirlos de un vistazo:

- **Preparar swap**: construye el payload y lo confirma. No firma nada. El
  resultado se puede guardar y firmar en otra cartera.
- **Ejecutar**: firma y emite. Sale dinero. Es irreversible.

Están separados en dos botones —y no en uno con una casilla— porque la diferencia
no es un grado: uno produce un fichero y el otro produce una transacción en la
red. Un botón que hace una cosa u otra según cómo esté configurado algo convierte
esa distinción en algo que hay que recordar en vez de algo que se ve.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.addresses import is_evm_address, is_solana_address
from amigocompora.domain.chains import CHAINS, AddressFormat, chain
from amigocompora.domain.errors import ConfirmationDeniedError
from amigocompora.domain.models import (
    Opportunity,
    PlannedTransaction,
    PriceComparison,
    Quote,
    Token,
    TradingPair,
)
from amigocompora.domain.modes import Capability
from amigocompora.engines.catalog import quote_token, tokens_for, wrapped_native
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED
from amigocompora.ui.widgets import spawn

#: Aviso que acompaña a todo payload exportado. Va dentro del fichero y no sólo
#: en la pantalla: el fichero sobrevive a la sesión y viaja a otro sitio, y quien
#: lo abra tiene que saber qué tiene entre manos.
#:
#: Dice **de este documento** y no de la aplicación, que es la corrección que hizo
#: falta al añadir la ejecución: «Amigocompora no firma ni emite transacciones»
#: era verdad cuando este texto se escribió y dejó de serlo. Lo que sigue siendo
#: cierto pase lo que pase es que un fichero no firma nada por sí solo.
_UNSIGNED_NOTICE = (
    "Payload SIN FIRMAR: este documento no es una transacción emitida, y guardarlo "
    "no emite nada. Revísalo y fírmalo con tu propia cartera."
)


class PricesPage(QWidget):
    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._comparison: PriceComparison | None = None
        self._prepared: PlannedTransaction | None = None
        #: Tokens que el usuario ha añadido por su dirección. Viven aquí y no en
        #: el catálogo porque un catálogo es una tabla que alguien mantiene, y
        #: esto es lo contrario: el token que a este usuario le interesa hoy y que
        #: no está en ninguna tabla. Se pierden al cerrar la aplicación, que es lo
        #: correcto: lo que se escribió una vez y no se guardó no debería
        #: reaparecer como si fuera configuración.
        self._extras: list[Token] = []

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # Filtros
        top = QHBoxLayout()
        top.addWidget(QLabel("Red:"))
        self._chain = QComboBox()
        for key in sorted(CHAINS):
            self._chain.addItem(f"{CHAINS[key].name} ({key})", key)
        top.addWidget(self._chain)

        # La dirección va antes que el token a propósito: decide qué se entrega
        # y, con ello, **en qué unidad** se escribe la cantidad. Es lo primero
        # que hay que tener claro porque cambia el significado de todo lo demás.
        top.addWidget(QLabel("Dirección:"))
        self._direction = QComboBox()
        self._direction.addItem("Comprar", True)
        self._direction.addItem("Vender", False)
        top.addWidget(self._direction)

        # El desplegable elige **el token**, no la pata base del par: según la
        # dirección ese mismo token cae del lado que se entrega o del que se
        # recibe. Llamarlo «Base» era exacto mientras sólo se pudiera vender.
        top.addWidget(QLabel("Token:"))
        self._base = QComboBox()
        top.addWidget(self._base)

        # Un token que no está en el catálogo entra por su dirección, y el
        # símbolo y los decimales se leen de su contrato. Es lo que hace que la
        # lista no sea el límite de lo que la aplicación sabe nombrar: con esto,
        # cualquier token de la red se puede cotizar y operar.
        self._lookup_btn = QPushButton("+ Token…")
        self._lookup_btn.setObjectName("secondary")
        self._lookup_btn.setToolTip(
            "Añade un token que no está en la lista, por su dirección de contrato. "
            "El símbolo y los decimales se leen de la cadena, no se piden a mano."
        )
        self._lookup_btn.clicked.connect(self._on_lookup_token)
        top.addWidget(self._lookup_btn)

        # La otra pata del par. Antes era siempre la stablecoin de referencia
        # —por eso el par no aparecía en ningún desplegable—, y con la stablecoin
        # fija un token que sólo tiene mercado contra el nativo, como `bankr`
        # contra WETH, no se podía ni cotizar.
        top.addWidget(QLabel("Contra:"))
        self._contra = QComboBox()
        top.addWidget(self._contra)

        self._pair = QLabel("—")
        self._pair.setStyleSheet(f"color: {COLOR_MUTED};")
        top.addWidget(self._pair)

        top.addWidget(QLabel("Cantidad:"))
        self._amount = QDoubleSpinBox()
        self._amount.setRange(0.000001, 1_000_000)
        self._amount.setDecimals(6)
        self._amount.setValue(1.0)
        top.addWidget(self._amount)
        # La unidad, pegada al importe que califica. Sin ella el número no dice
        # nada: el mismo «1» es un token o mil dólares según la dirección.
        self._unit = QLabel("")
        self._unit.setStyleSheet(f"color: {COLOR_MUTED};")
        top.addWidget(self._unit)

        self._btn = QPushButton("Cotizar")
        self._btn.clicked.connect(self._on_quote)
        top.addWidget(self._btn)
        top.addStretch()
        lay.addLayout(top)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["Venue", "Motor", "Recibes", "Precio", "Comisión", "Impacto", "Liquidez", "Nota"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self._table, stretch=1)

        self._opp_table = QTableWidget(0, 5)
        self._opp_table.setHorizontalHeaderLabels(["Mejor", "Referencia", "Spread bruto", "Spread neto", "Accionable"])
        self._opp_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._opp_table.setAlternatingRowColors(True)
        self._opp_table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(QLabel("<b>Oportunidades (diferencial neto ≥ 10 bps)</b>"))
        lay.addWidget(self._opp_table)

        # Acciones sobre la cotización elegida. Deshabilitadas hasta que haya una
        # fila seleccionada y un motor capaz de construir: un botón que existe y
        # falla al pulsarlo enseña a desconfiar de los botones.
        actions = QHBoxLayout()
        self._swap_btn = QPushButton("Preparar swap…")
        self._swap_btn.setObjectName("secondary")
        self._swap_btn.setToolTip(
            "Construye la transacción sin firmar de la cotización seleccionada. "
            "Este botón no firma ni emite nada: para eso está el de al lado, que "
            "es irreversible."
        )
        self._swap_btn.clicked.connect(self._on_prepare_swap)
        self._swap_btn.setEnabled(False)
        actions.addWidget(self._swap_btn)

        self._exec_btn = QPushButton("Ejecutar: firmar y emitir…")
        self._exec_btn.setObjectName("danger")
        self._exec_btn.setToolTip(
            "Firma la transacción y la emite a la red. Es irreversible y sale "
            "dinero de tu cartera."
        )
        self._exec_btn.clicked.connect(self._on_execute)
        self._exec_btn.setEnabled(False)
        actions.addWidget(self._exec_btn)

        self._save_btn = QPushButton("Guardar payload…")
        self._save_btn.setObjectName("secondary")
        self._save_btn.setToolTip("Exporta a JSON la última transacción sin firmar que confirmaste.")
        self._save_btn.clicked.connect(self._on_save_payload)
        self._save_btn.setEnabled(False)
        actions.addWidget(self._save_btn)
        actions.addStretch()
        lay.addLayout(actions)

        # Por qué «Ejecutar» está apagado, en la misma línea y no escondido en un
        # tooltip. Un botón apagado sin motivo es lo que empuja a buscar la forma
        # de saltárselo, así que aquí se dice qué falta exactamente — y se dice
        # **antes** de que alguien lo intente, no cuando falla.
        self._exec_note = QLabel("")
        self._exec_note.setWordWrap(True)
        self._exec_note.setStyleSheet(f"color: {COLOR_DANGER};")
        lay.addWidget(self._exec_note)

        self._table.itemSelectionChanged.connect(self._on_quote_selected)
        self._chain.currentIndexChanged.connect(self._refresh_tokens)
        self._base.currentIndexChanged.connect(self._update_direction_labels)
        self._contra.currentIndexChanged.connect(self._update_direction_labels)
        self._direction.currentIndexChanged.connect(self._update_direction_labels)
        self._refresh_tokens()

    # ------------------------------------------------------------------ #
    # Filtros
    # ------------------------------------------------------------------ #
    def _available_tokens(self, chain_key: str) -> tuple[Token, ...]:
        """Los tokens que se pueden elegir en esta red: catálogo más añadidos.

        Sin quitar la stablecoin de referencia, que es lo que hacía la versión
        anterior: mientras el par era siempre «token contra stablecoin», ofrecer
        la stablecoin como token era ofrecer un par consigo misma. En cuanto la
        otra pata se elige aparte, comprar dólares con ETH es una operación
        perfectamente normal y el filtro sobraba.
        """
        found = list(tokens_for(chain_key))
        conocidos = {(token.symbol, token.address) for token in found}
        found.extend(
            token
            for token in self._extras
            if token.chain == chain_key and (token.symbol, token.address) not in conocidos
        )
        return tuple(found)

    def _refresh_tokens(self) -> None:
        key = self._chain.currentData()
        previous = self._contra.currentData()
        self._base.clear()
        self._contra.clear()
        for token in self._available_tokens(key):
            self._base.addItem(token.symbol, token)
            self._contra.addItem(token.symbol, token)

        # La pata contraria: se conserva la que el usuario ya había elegido si
        # sigue existiendo en esta red, y si no se cae a la stablecoin de
        # referencia —con la que se cotiza todo y que hace comparables los precios
        # de los venues— o, en una red sin ella, al envoltorio nativo, que es la
        # otra moneda con la que existen mercados de verdad.
        #
        # Conservarla importa porque esta función también se llama al añadir un
        # token por dirección: repintar las listas no es motivo para deshacer una
        # elección que el usuario acaba de hacer.
        elegido = (
            self._contra.findText(previous.symbol) if previous is not None else -1
        )
        if elegido < 0:
            default = quote_token(key) or wrapped_native(key)
            if default is not None:
                elegido = self._contra.findText(default.symbol)
        if elegido >= 0:
            self._contra.setCurrentIndex(elegido)
        self._update_direction_labels()

    def _on_lookup_token(self) -> None:
        """Añade un token por su dirección, leyendo su contrato."""
        chain_key: str = self._chain.currentData()
        address, accepted = QInputDialog.getText(
            self,
            "Token por dirección",
            f"Dirección del contrato en {CHAINS[chain_key].name}:\n"
            "(el símbolo y los decimales se leen de la cadena)",
        )
        if not accepted or not address.strip():
            return
        self._lookup_btn.setEnabled(False)
        self._status.setText("Leyendo el contrato del token…")
        spawn(self._do_lookup_token(chain_key, address.strip()))

    async def _do_lookup_token(self, chain_key: str, address: str) -> None:
        try:
            token = await self._container.token_lookup.by_address(chain_key, address)
        except Exception as error:
            self._status.setText(f"No se pudo añadir el token: {error}")
            return
        finally:
            self._lookup_btn.setEnabled(True)

        self._extras.append(token)
        self._refresh_tokens()
        index = self._base.findText(token.symbol)
        if index >= 0:
            self._base.setCurrentIndex(index)
        self._status.setText(
            f"Añadido {token.symbol} ({token.decimals} decimales) desde {token.address}. "
            f"Ya se puede cotizar."
        )

    def _update_direction_labels(self) -> None:
        """Qué se entrega y qué se recibe, y en qué unidad va la cantidad.

        La cantidad se escribe en la unidad de la pata que **se entrega**, y esa
        pata cambia con la dirección. Por eso la etiqueta no es decoración: sin
        ella el mismo número significa dos cosas distintas según un desplegable
        que está tres widgets más allá, y esto acaba en una firma.
        """
        entrega, recibe = self._legs()
        if entrega is None or recibe is None:
            self._pair.setText("—")
            self._unit.setText("")
            return
        self._pair.setText(f"{entrega.symbol} → {recibe.symbol}")
        self._unit.setText(entrega.symbol)

    def _legs(self) -> tuple[Token | None, Token | None]:
        """Las dos patas del par, en el orden en que se mueven.

        `entrega` es lo que sale de la cartera y `recibe` lo que entra. Es la
        única traducción de «comprar/vender» a un par, y por eso está en un sitio
        solo: repartida por la página, la dirección acabaría invertida en alguna
        de las vistas y el importe se firmaría al revés.
        """
        token = self._base.currentData()
        contra = self._contra.currentData()
        if token is None or contra is None:
            return None, None
        comprando = bool(self._direction.currentData())
        return (contra, token) if comprando else (token, contra)

    def _on_quote(self) -> None:
        self._btn.setEnabled(False)
        self._status.setText("Consultando…")
        spawn(self._do_quote())

    async def _do_quote(self) -> None:
        try:
            entrega, recibe = self._legs()
            if entrega is None or recibe is None:
                self._status.setText("Elige el token y la moneda contra la que cotizarlo.")
                return
            if entrega.symbol == recibe.symbol and entrega.address == recibe.address:
                # Se dice en vez de impedirlo: los dos desplegables son libres a
                # propósito —bloquear combinaciones entre ellos convierte un
                # error evidente en un desplegable que se mueve solo y no se
                # entiende—, y la etiqueta del par ya lo está enseñando.
                self._status.setText(
                    f"«{entrega.symbol}» en las dos patas no es una operación: "
                    f"elige dos tokens distintos."
                )
                self._table.setRowCount(0)
                self._opp_table.setRowCount(0)
                return

            # Qué lado es `base` **es** la dirección: `base` es lo que se
            # entrega. Y la cantidad se expresa siempre en el `base`, así que con
            # el par bien armado la dirección no vuelve a aparecer en ninguna
            # cuenta de las de después.
            pair = TradingPair(base=entrega, quote=recibe)
            amount = pair.base.amount(Decimal(str(self._amount.value())))
            comparison = await self._container.compare_prices(pair, amount)
            self._comparison = comparison
            self._fill_table(comparison)
            # También buscar oportunidades con el umbral por defecto.
            opps = await self._container.scan_opportunities(pair, amount)
            self._fill_opps(opps)
            note = "con estimaciones" if comparison.has_estimates else "todo medido"
            if comparison.has_unknown_fees:
                note += " · hay comisiones no desglosadas"
            if comparison.has_partial_sources:
                # Se dice en la misma línea que el resultado y no en un aviso
                # aparte: una comparación a la que le falta una fuente se lee
                # como «aquí no hay nada mejor», y puede que la mejor fuera
                # justo la que no respondió.
                note += f" · sin respuesta: {', '.join(comparison.failed_engines)}"
            self._status.setText(
                f"{len(comparison.quotes)} venue(s) · spread {comparison.spread_bps} · {note} · observado {comparison.observed_at.isoformat()}"
            )
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._table.setRowCount(0)
            self._opp_table.setRowCount(0)
        finally:
            self._btn.setEnabled(True)
            self._on_quote_selected()

    def _fill_table(self, comp: PriceComparison) -> None:
        self._table.setRowCount(0)
        for quote in comp.ranked:
            row = self._table.rowCount()
            self._table.insertRow(row)
            fee = str(quote.fee_bps) if quote.fee_bps is not None else "— no desglosada"
            fee_basis = quote.fee_basis.value if quote.fee_basis is not None else "—"
            self._table.setItem(row, 0, QTableWidgetItem(quote.venue.name))
            # Qué motor produjo la cifra. Con varios motores activos la tabla es
            # una mezcla, y dos motores pueden cotizar el mismo par por caminos
            # distintos: sin esta columna, dos filas del mismo venue parecerían
            # un error de la vista en vez de dos fuentes que no coinciden.
            self._table.setItem(row, 1, QTableWidgetItem(quote.engine_id))
            self._table.setItem(row, 2, QTableWidgetItem(str(quote.amount_out)))
            self._table.setItem(row, 3, QTableWidgetItem(str(quote.price)))
            item_fee = QTableWidgetItem(f"{fee} [{fee_basis}]")
            if not quote.fee_is_known:
                item_fee.setForeground(Qt.yellow)
            self._table.setItem(row, 4, item_fee)
            # Un impacto sin publicar no es un cero: se escribe igual que en el
            # diálogo de confirmación, para que las dos vistas digan lo mismo de
            # la misma cotización.
            impact = (
                str(quote.price_impact_bps)
                if quote.price_impact_bps is not None
                else "— no publicado"
            )
            impact_basis = quote.impact_basis.value if quote.impact_basis is not None else "—"
            item_imp = QTableWidgetItem(f"{impact} [{impact_basis}]")
            if not quote.impact_is_known or not quote.is_exact:
                item_imp.setForeground(Qt.yellow)
            self._table.setItem(row, 5, item_imp)
            self._table.setItem(
                row, 6, QTableWidgetItem(str(quote.liquidity) if quote.liquidity else "—")
            )
            self._table.setItem(row, 7, QTableWidgetItem(quote.source_note))

    def _fill_opps(self, opps: tuple[Opportunity, ...]) -> None:
        self._opp_table.setRowCount(0)
        for opp in opps:
            row = self._opp_table.rowCount()
            self._opp_table.insertRow(row)
            self._opp_table.setItem(row, 0, QTableWidgetItem(opp.best.venue.name))
            self._opp_table.setItem(row, 1, QTableWidgetItem(opp.reference.venue.name))
            self._opp_table.setItem(row, 2, QTableWidgetItem(str(opp.gross_spread_bps)))
            self._opp_table.setItem(row, 3, QTableWidgetItem(str(opp.net_spread_bps)))
            mark = "✓" if opp.is_actionable else "—"
            self._opp_table.setItem(row, 4, QTableWidgetItem(mark))

    # ------------------------------------------------------------------ #
    # Preparar el swap
    # ------------------------------------------------------------------ #
    def _selected_quote(self) -> Quote | None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows or self._comparison is None:
            return None
        ranked = self._comparison.ranked
        index = rows[0].row()
        return ranked[index] if 0 <= index < len(ranked) else None

    def _on_quote_selected(self) -> None:
        quote = self._selected_quote()
        self._swap_btn.setEnabled(
            quote is not None and self._container.prepare_swap.is_available()
        )
        motivos = self._execution_blockers(quote.pair if quote is not None else None)
        self._exec_btn.setEnabled(quote is not None and not motivos)
        # El motivo se enseña sólo cuando ya hay una cotización elegida: antes de
        # eso el botón apagado no dice nada que el usuario no sepa ya, y un aviso
        # permanente en rojo se aprende a ignorar.
        self._exec_note.setText(
            ""
            if quote is None or not motivos
            else "No se puede ejecutar: " + " · ".join(motivos) + "."
        )

    def refresh_execution_state(self) -> None:
        """Repinta lo que depende del modo, que hoy es el botón de ejecutar.

        Público porque quien sabe que el modo ha cambiado es la ventana, no esta
        pestaña: `ModeGuard.subscribe` avisa a quien se apunte, y la pestaña no se
        apunta sola. Existe en vez de que la ventana llame a `_on_quote_selected`
        porque un método privado de otra clase no es una interfaz, es un atajo.
        """
        self._on_quote_selected()

    def _execution_blockers(self, pair: TradingPair | None = None) -> tuple[str, ...]:
        """Todo lo que falta para poder firmar y emitir. Vacío significa que sí.

        Se devuelve la **lista de motivos** y no un booleano porque un botón
        apagado sin explicación es lo que empuja a alguien a buscar la forma de
        saltárselo. Y se devuelven **todos** y no el primero: son condiciones
        distintas —el modo se cambia aquí, `enabled` se cambia en el fichero, los
        motores y la cartera se configuran aparte— y descubrirlas de una en una,
        arreglando una para que aparezca la siguiente, es lo que hace pensar que
        la aplicación está rota en vez de a medio configurar.

        Mira las mismas cosas que mira `ExecuteSwap` antes de firmar, en el mismo
        orden: si esta lista dijera que sí y el caso de uso dijera que no, la
        interfaz estaría prometiendo algo que no cumple.

        El orden va de la ruta a la custodia: primero si la operación puede
        existir —el modo, el interruptor, un motor que sepa construir el payload—
        y después si hay con qué firmarla. Decirlo al revés llevaría a configurar
        una cartera para descubrir luego que no había por dónde.

        El par se pregunta a propósito, y no sólo la red: un motor puede construir
        en una red y no en otra —`uniswap` cubre Base pero no Solana—, así que
        «hay algún planificador» no es la misma pregunta que «hay planificador
        para el par que estoy mirando». Y con un token añadido por dirección la
        lista blanca deja de ser un detalle de configuración que se supone
        correcto: es la diferencia entre un botón que firma y uno que no, así que
        se comprueba aquí y se dice cuál falta.
        """
        container = self._container
        motivos: list[str] = []
        if not container.guard.mode.grants(Capability.BROADCAST_TX):
            motivos.append(
                f"el modo {container.guard.mode.label} no permite emitir "
                "(cambia a EJECUCIÓN)"
            )
        if not container.policy.limits.enabled:
            motivos.append(
                "la ejecución está apagada (`enabled = true` bajo `[execution]` "
                "en config.toml)"
            )
        if pair is not None:
            fuera = sorted(
                symbol
                for symbol in (pair.base.symbol, pair.quote.symbol)
                if symbol not in container.policy.limits.allowed_tokens
            )
            if fuera:
                motivos.append(
                    f"{', '.join(fuera)} no está en `allowed_tokens`, así que la "
                    "política no deja operar con ese par (añádelo en config.toml)"
                )
        chain_key = pair.chain if pair is not None else None
        if not container.prepare_swap.is_available(chain_key):
            # Se midió: con `geckoterminal` activo —que cotiza pero no construye—
            # no hay ningún planificador, y sin este motivo el botón quedaba
            # encendido y fallaba al pulsarlo. Es justo el botón que enseña a
            # desconfiar de los botones, y el más caro de todos: el que firma.
            donde = f" en {chain_key}" if chain_key else ""
            motivos.append(
                f"no hay ningún motor activo capaz de construir el swap{donde} "
                "(activa `uniswap` o `zeroex` y dale su clave)"
            )
        if not container.keys.available():
            motivos.append("no hay cartera configurada, así que no hay con qué firmar")
        return tuple(motivos)

    def _recipient_candidates(self, chain_key: str) -> list[str]:
        """Las direcciones ya configuradas que son válidas en esa red.

        Ofrecer las propias antes que un campo vacío no es comodidad: teclear una
        dirección a mano es la forma más común de perder fondos, y la lista sale
        de lo que el usuario ya declaró en su configuración.

        La **cartera de la aplicación va primera**, porque es la dirección desde
        la que sale el dinero —de ahí sale y ahí vuelve— y es la única de la lista
        que la aplicación conoce sin que nadie la haya escrito en la
        configuración. Se deriva de la clave, así que nunca es la clave.
        """
        spec = chain(chain_key)
        solana = spec.address_format is AddressFormat.SOLANA_BASE58
        check = is_solana_address if solana else is_evm_address
        candidates: list[str] = []
        propia = self._container.keys.address()
        if propia is not None and check(propia):
            candidates.append(propia)
        candidates.extend(
            address for address in self._container.settings.watch_addresses if check(address)
        )
        # Sin repetidos, conservando el orden: la cartera puede estar también en
        # `watch_addresses`, y verla dos veces en el desplegable parece un fallo.
        return list(dict.fromkeys(candidates))

    def _ask_recipient(self, chain_key: str) -> str | None:
        candidates = self._recipient_candidates(chain_key)
        title = "Destino del swap"
        if candidates:
            text, accepted = QInputDialog.getItem(
                self,
                title,
                "Dirección que recibe (elige una tuya o pega otra):",
                candidates,
                0,
                True,  # editable: la lista es un atajo, no un límite
            )
        else:
            text, accepted = QInputDialog.getText(self, title, "Dirección que recibe:")
        if not accepted:
            return None
        return text.strip() or None

    def _on_prepare_swap(self) -> None:
        quote = self._selected_quote()
        if quote is None:
            self._status.setText("Selecciona primero una cotización de la tabla.")
            return
        recipient = self._ask_recipient(quote.pair.chain)
        if recipient is None:
            return
        self._swap_btn.setEnabled(False)
        self._status.setText("Construyendo la transacción sin firmar…")
        spawn(self._do_prepare(quote, recipient))

    async def _do_prepare(self, quote: Quote, recipient: str) -> None:
        try:
            transaction = await self._container.prepare_swap(quote, recipient=recipient)
            self._prepared = transaction
            self._save_btn.setEnabled(True)
            self._status.setText(
                "Transacción preparada y confirmada. Amigocompora NO la ha firmado "
                "ni emitido: guárdala y fírmala en tu cartera."
            )
        except ConfirmationDeniedError:
            # Decir «error» aquí sería mentir: el usuario hizo lo correcto.
            self._status.setText("Cancelaste la preparación. No se construyó nada.")
        except Exception as error:
            self._status.setText(f"Error: {error}")
        finally:
            self._on_quote_selected()

    # ------------------------------------------------------------------ #
    # Ejecutar: firmar y emitir
    # ------------------------------------------------------------------ #
    def _on_execute(self) -> None:
        quote = self._selected_quote()
        if quote is None:
            self._status.setText("Selecciona primero una cotización de la tabla.")
            return
        # Se vuelve a comprobar aquí aunque el botón ya estuviera apagado: el modo
        # se puede cambiar desde la barra mientras esta pestaña está abierta, y un
        # botón sólo se repinta cuando algo lo repinta. La puerta de verdad está
        # en `ExecuteSwap`; esto es para no llegar hasta ella y volver con un
        # error que se puede decir antes de molestar a nadie.
        motivos = self._execution_blockers(quote.pair)
        if motivos:
            self._status.setText("No se puede ejecutar: " + " · ".join(motivos) + ".")
            return
        recipient = self._ask_recipient(quote.pair.chain)
        if recipient is None:
            return
        self._exec_btn.setEnabled(False)
        self._status.setText("Firmando y emitiendo…")
        spawn(self._do_execute(quote, recipient))

    async def _do_execute(self, quote: Quote, recipient: str) -> None:
        try:
            receipt = await self._container.execute_swap(quote, recipient=recipient)
            self._status.setText(
                f"Emitida {receipt.tx_hash} · estado {receipt.status.value}. "
                "El hash se puede contrastar en un explorador de la red."
            )
        except ConfirmationDeniedError:
            # Decir «error» aquí sería mentir: el usuario hizo lo correcto.
            self._status.setText("Cancelaste la operación. No se firmó ni se emitió nada.")
        except Exception as error:
            self._status.setText(f"Error: {error}")
        finally:
            self._on_quote_selected()

    def _on_save_payload(self) -> None:
        if self._prepared is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar transacción sin firmar",
            "swap-sin-firmar.json",
            "JSON (*.json)",
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(_payload_document(self._prepared), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as error:
            self._status.setText(f"No se pudo guardar: {error}")
            return
        self._status.setText(
            f"Guardada en {path}. Sigue SIN FIRMAR: nada se ha emitido."
        )


def _payload_document(transaction: PlannedTransaction) -> dict[str, str]:
    """Documento exportable de un payload sin firmar.

    Se construye desde `describe()` —los mismos campos que se mostraron en el
    diálogo de confirmación— y no volcando el objeto: lo que se exporta tiene que
    ser exactamente lo que el usuario leyó y aprobó, no una representación
    interna que puede cambiar sin que el diálogo cambie.
    """
    document: dict[str, str] = {"tipo": type(transaction).__name__}
    document.update(dict(transaction.describe()))
    document["aviso"] = _UNSIGNED_NOTICE
    return document
