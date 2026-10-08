"""Tema visual de la aplicación.

Un único sitio para colores, tipografía y stylesheet. Así el modo claro (futuro)
es un cambio de constantes y no una caza por 15 ficheros.

### Por qué el tema es un fichero y no una hoja de estilos suelta

Qt no admite variables en su lenguaje de hojas de estilo: `QPushButton { background:
$accent }` no existe. La única forma de que un cambio de paleta no obligue a
buscar y reemplazar por toda la interfaz es construir el texto del stylesheet en
Python, con las constantes intercaladas. De ahí que esto sea un f-string tan
largo y no un `.qss` al lado.

### Las tres reglas de la paleta

1. **El peligro es un color, no un icono.** Firmar y emitir es irreversible, así
   que los botones que emiten llevan el rojo de `COLOR_DANGER` y los que sólo
   construyen un payload no. La diferencia entre «preparar» y «ejecutar» tiene
   que verse sin leer.
2. **Lo que no se sabe se pinta distinto, no se pinta de cero.** Una comisión sin
   desglosar va en ámbar, nunca en blanco: un cero y un «no publicado» no pueden
   parecerse.
3. **El acento no decora: señala.** Sólo lo pulsable y lo seleccionado llevan
   acento. Una tabla con todo en azul no tiene nada señalado.
"""

from __future__ import annotations

from typing import Final

APP_TITLE: Final = "Amigocompora"
APP_SUBTITLE: Final = "Análisis on-chain y mercados de predicción — la IA propone, tú decides"

# --------------------------------------------------------------------------- #
# Paleta
# --------------------------------------------------------------------------- #
#: Fondo de la ventana. Más oscuro que las tarjetas a propósito: la profundidad
#: la da el contraste entre los dos, no una sombra, que en Qt hay que dibujar a
#: mano y sale mal en todas las plataformas menos en una.
COLOR_BG: Final = "#0b0d12"
#: Fondo de las tarjetas y de las tablas: donde vive el contenido.
COLOR_CARD: Final = "#141821"
#: Un escalón por encima de la tarjeta. Para cabeceras de tabla y campos.
COLOR_ELEVATED: Final = "#1b2029"
COLOR_BORDER: Final = "#262c38"
COLOR_BORDER_STRONG: Final = "#39414f"
COLOR_TEXT: Final = "#e8eaf0"
COLOR_MUTED: Final = "#8b93a7"
#: Texto sobre fondo de acento. Blanco puro, no el `COLOR_TEXT`: sobre azul el
#: blanco puro es lo único que mantiene el contraste que exige la WCAG.
COLOR_ON_ACCENT: Final = "#ffffff"

COLOR_ACCENT: Final = "#4c8dff"
COLOR_ACCENT_HOVER: Final = "#6ba1ff"
COLOR_ACCENT_PRESSED: Final = "#3b7ae6"
#: Relleno translúcido del acento, para el fondo de lo seleccionado. En Qt el
#: alfa se escribe `#AARRGGBB`, no `rgba()`: `rgba()` funciona en algunos
#: widgets y en otros se ignora en silencio.
COLOR_ACCENT_SOFT: Final = "#2a4c8dff"

COLOR_SUCCESS: Final = "#3dd68c"
COLOR_WARNING: Final = "#f5c518"
COLOR_DANGER: Final = "#ff5a5a"
COLOR_DANGER_HOVER: Final = "#ff7575"

# Severidad → color de badge
SEVERITY_COLOR: Final[dict[str, str]] = {
    "low": COLOR_MUTED,
    "medium": COLOR_WARNING,
    "high": COLOR_DANGER,
}

#: Radios y espaciados, en un sitio para que la interfaz no tenga veinte.
RADIUS: Final = 8
RADIUS_SM: Final = 6

STYLESHEET: Final = f"""
/* ------------------------------------------------------------------ base -- */
QMainWindow, QDialog {{
    background: {COLOR_BG};
    color: {COLOR_TEXT};
}}
QWidget {{
    font-family: 'Inter', 'Segoe UI', 'Noto Sans', system-ui, sans-serif;
    font-size: 13px;
    color: {COLOR_TEXT};
}}
QLabel {{ background: transparent; }}

/* --------------------------------------------------------------- tarjetas -- */
QFrame#card {{
    background: {COLOR_CARD};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS}px;
}}
QFrame#cardHeader {{
    background: {COLOR_ELEVATED};
    border: none;
    border-top-left-radius: {RADIUS}px;
    border-top-right-radius: {RADIUS}px;
}}
QLabel#cardTitle {{
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.4px;
}}
QLabel#sectionTitle {{
    font-size: 11px;
    font-weight: 700;
    color: {COLOR_MUTED};
    letter-spacing: 1px;
}}
QLabel#hint {{
    color: {COLOR_MUTED};
    font-size: 11px;
}}
QLabel#danger {{ color: {COLOR_DANGER}; }}
QLabel#warning {{ color: {COLOR_WARNING}; }}
QLabel#success {{ color: {COLOR_SUCCESS}; }}
QLabel#muted {{ color: {COLOR_MUTED}; }}
QLabel#bigNumber {{
    font-size: 20px;
    font-weight: 700;
}}
/* Las dos patas del par. Un recuadro por lado, y no dos filas de campos: lo que
   se está describiendo es un intercambio entre dos cosas, y las dos tienen que
   verse a la vez y del mismo tamaño. */
QFrame#legBox {{
    background: {COLOR_BG};
    border: 1px solid {COLOR_BORDER_STRONG};
    border-radius: {RADIUS}px;
}}
QLabel#empty {{
    color: {COLOR_MUTED};
    font-style: italic;
    padding: 6px 2px;
}}
QFrame#divider {{
    background: {COLOR_BORDER};
    max-height: 1px;
    border: none;
}}

/* --------------------------------------------------------------- pestañas -- */
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS}px;
    background: {COLOR_CARD};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {COLOR_MUTED};
    padding: 9px 18px;
    border: 1px solid transparent;
    border-top-left-radius: {RADIUS_SM}px;
    border-top-right-radius: {RADIUS_SM}px;
    margin-right: 3px;
    font-weight: 600;
}}
QTabBar::tab:hover {{ color: {COLOR_TEXT}; }}
QTabBar::tab:selected {{
    background: {COLOR_CARD};
    color: {COLOR_TEXT};
    border-color: {COLOR_BORDER};
    border-bottom-color: {COLOR_CARD};
}}

/* ---------------------------------------------------------------- botones -- */
QPushButton {{
    background: {COLOR_ACCENT};
    color: {COLOR_ON_ACCENT};
    border: 1px solid {COLOR_ACCENT};
    border-radius: {RADIUS_SM}px;
    padding: 7px 16px;
    font-weight: 600;
}}
QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; border-color: {COLOR_ACCENT_HOVER}; }}
QPushButton:pressed {{ background: {COLOR_ACCENT_PRESSED}; }}
QPushButton:disabled {{
    background: {COLOR_ELEVATED};
    border-color: {COLOR_BORDER};
    color: {COLOR_MUTED};
}}
QPushButton#secondary {{
    background: transparent;
    border: 1px solid {COLOR_BORDER_STRONG};
    color: {COLOR_TEXT};
}}
QPushButton#secondary:hover {{ background: {COLOR_ELEVATED}; border-color: {COLOR_ACCENT}; }}
QPushButton#secondary:disabled {{ border-color: {COLOR_BORDER}; color: {COLOR_MUTED}; }}
/* El botón que firma. Rojo macizo en vez de un icono de aviso: el color es lo
   único que se lee sin detenerse, y este botón es el que saca dinero. */
QPushButton#danger {{ background: {COLOR_DANGER}; border-color: {COLOR_DANGER}; }}
QPushButton#danger:hover {{ background: {COLOR_DANGER_HOVER}; border-color: {COLOR_DANGER_HOVER}; }}
QPushButton#danger:disabled {{
    background: {COLOR_ELEVATED};
    border-color: {COLOR_BORDER};
    color: {COLOR_MUTED};
}}
QPushButton#link {{
    background: transparent;
    border: none;
    color: {COLOR_ACCENT};
    padding: 2px 4px;
    font-weight: 600;
    text-align: left;
}}
QPushButton#link:hover {{ color: {COLOR_ACCENT_HOVER}; text-decoration: underline; }}
QPushButton:focus {{ outline: none; border-color: {COLOR_ACCENT_HOVER}; }}

/* ---------------------------------------------------------------- campos -- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {COLOR_BG};
    border: 1px solid {COLOR_BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: 6px 10px;
    color: {COLOR_TEXT};
    selection-background-color: {COLOR_ACCENT};
    selection-color: {COLOR_ON_ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {COLOR_ACCENT};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {COLOR_MUTED};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 18px;
    border: none;
    background: transparent;
}}
QComboBox QAbstractItemView {{
    background: {COLOR_ELEVATED};
    border: 1px solid {COLOR_BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: 4px;
    outline: none;
    selection-background-color: {COLOR_ACCENT_SOFT};
    selection-color: {COLOR_TEXT};
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 16px;
    background: transparent;
    border: none;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {COLOR_ELEVATED};
    border-radius: 3px;
}}

/* ---------------------------------------------------------------- tablas -- */
QTableView, QTableWidget {{
    background: {COLOR_CARD};
    alternate-background-color: {COLOR_ELEVATED};
    gridline-color: transparent;
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_SM}px;
    outline: none;
}}
QTableView::item, QTableWidget::item {{
    padding: 6px 8px;
    border: none;
}}
QTableView::item:hover, QTableWidget::item:hover {{ background: {COLOR_ELEVATED}; }}
QTableView::item:selected, QTableWidget::item:selected {{
    background: {COLOR_ACCENT_SOFT};
    color: {COLOR_TEXT};
}}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {COLOR_ELEVATED};
    color: {COLOR_MUTED};
    padding: 7px 8px;
    border: none;
    border-right: 1px solid {COLOR_CARD};
    border-bottom: 1px solid {COLOR_BORDER};
    font-weight: 700;
    font-size: 11px;
    letter-spacing: 0.6px;
}}
QHeaderView::section:last {{ border-right: none; }}
QTableCornerButton::section {{ background: {COLOR_ELEVATED}; border: none; }}

/* ------------------------------------------------------------ desplazar -- */
QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {COLOR_BORDER_STRONG};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {COLOR_MUTED}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {COLOR_BORDER_STRONG};
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: {COLOR_MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ------------------------------------------------------- barra de estado -- */
QStatusBar {{
    background: {COLOR_BG};
    border-top: 1px solid {COLOR_BORDER};
    color: {COLOR_MUTED};
}}
QStatusBar::item {{ border: none; }}

QToolTip {{
    background: {COLOR_ELEVATED};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER_STRONG};
    padding: 6px 9px;
    border-radius: {RADIUS_SM}px;
}}
QProgressBar {{
    background: {COLOR_BG};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_SM}px;
    text-align: center;
    min-height: 14px;
}}
QProgressBar::chunk {{ background: {COLOR_ACCENT}; border-radius: 5px; }}
QMenu {{
    background: {COLOR_ELEVATED};
    border: 1px solid {COLOR_BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {COLOR_ACCENT_SOFT}; }}
"""
