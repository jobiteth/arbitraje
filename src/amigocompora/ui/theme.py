"""Tema visual de la aplicación.

Un único sitio para colores, tipografía y stylesheet. Así el modo oscuro (futuro)
es un cambio de constantes y no una caza por 15 ficheros.
"""

from __future__ import annotations

from typing import Final

APP_TITLE: Final = "Amigocompora"
APP_SUBTITLE: Final = "Análisis on-chain y mercados de predicción — la IA propone, tú decides"

COLOR_BG: Final = "#0f1115"
COLOR_CARD: Final = "#1a1d24"
COLOR_BORDER: Final = "#2a2f3a"
COLOR_TEXT: Final = "#e6e8eb"
COLOR_MUTED: Final = "#8b95a5"
COLOR_ACCENT: Final = "#4f8cff"
COLOR_ACCENT_HOVER: Final = "#6aa0ff"
COLOR_SUCCESS: Final = "#3dd68c"
COLOR_WARNING: Final = "#f5c518"
COLOR_DANGER: Final = "#ff5a5a"

# Severidad → color de badge
SEVERITY_COLOR: Final[dict[str, str]] = {
    "low": COLOR_MUTED,
    "medium": COLOR_WARNING,
    "high": COLOR_DANGER,
}

STYLESHEET: Final = f"""
QMainWindow {{
    background: {COLOR_BG};
    color: {COLOR_TEXT};
}}
QWidget {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 13px;
    color: {COLOR_TEXT};
}}
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    background: {COLOR_CARD};
    padding: 8px;
}}
QTabBar::tab {{
    background: transparent;
    color: {COLOR_MUTED};
    padding: 8px 16px;
    border-radius: 6px;
    margin-right: 4px;
}}
QTabBar::tab:selected {{
    background: {COLOR_CARD};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
}}
QPushButton {{
    background: {COLOR_ACCENT};
    color: white;
    border: none;
    border-radius: 6px;
    padding: 7px 14px;
    font-weight: 600;
}}
QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}
QPushButton:disabled {{ background: #2a2f3a; color: {COLOR_MUTED}; }}
QPushButton#secondary {{
    background: transparent;
    border: 1px solid {COLOR_BORDER};
    color: {COLOR_TEXT};
}}
QPushButton#secondary:hover {{ background: #232730; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {COLOR_BG};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    color: {COLOR_TEXT};
}}
QComboBox QAbstractItemView {{
    background: {COLOR_CARD};
    border: 1px solid {COLOR_BORDER};
}}
QTableView {{
    background: {COLOR_CARD};
    alternate-background-color: #1e2129;
    gridline-color: {COLOR_BORDER};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
}}
QHeaderView::section {{
    background: {COLOR_BG};
    color: {COLOR_MUTED};
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
}}
QTextEdit, QPlainTextEdit {{
    background: {COLOR_BG};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px;
}}
QStatusBar {{
    background: {COLOR_BG};
    border-top: 1px solid {COLOR_BORDER};
    color: {COLOR_MUTED};
}}
QToolTip {{
    background: #232730;
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    padding: 6px 8px;
    border-radius: 6px;
}}
QProgressBar {{
    background: {COLOR_BG};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    text-align: center;
    min-height: 14px;
}}
QProgressBar::chunk {{
    background: {COLOR_ACCENT};
    border-radius: 5px;
}}
"""
