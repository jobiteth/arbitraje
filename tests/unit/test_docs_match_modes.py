"""La documentación que se entrega no puede contradecir a la tabla de política.

Existe por un fallo que duró una entrega entera sin que nada lo dijera: cuando se
implementó la ejecución real, seis ficheros del repositorio siguieron afirmando
que la aplicación **no firma ni emite** transacciones. La afirmación no era una
imprecisión de matiz —describía como inexistente la función que acababa de
entregarse, y en `README.md` y `AGENTS.md`, que es lo primero que lee cualquiera
que llegue al proyecto.

El fallo no se vio porque nada lo miraba. La tabla `MODE_CAPABILITIES` es la
única fuente de verdad sobre qué permite cada modo, y las pruebas la cubrían
entera; pero la prosa que la describe no la leía nadie, así que pudo quedarse
atrás en silencio. Eso es lo que se corrige aquí: no se comprueba que la
documentación sea bonita ni completa, sino que **no diga lo contrario** de lo que
el código hace.

Se ata a la tabla y no a un texto fijo a propósito. Una prueba que exigiera la
frase exacta se rompería con cada reescritura y acabaría ajustándose sin leerla;
una que compara contra `MODE_CAPABILITIES` sigue siendo cierta mientras la
política no cambie, y **falla sola** el día que un modo gane o pierda una
capacidad y la tabla del README no se entere.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from amigocompora.domain.modes import Capability, OperationMode, modes_granting

#: `tests/unit/<fichero>` → la raíz del repositorio.
RAIZ: Final = Path(__file__).resolve().parents[2]

#: Lo que se documenta de cara a quien llega al proyecto. `CHANGELOG.md` queda
#: fuera: es un registro histórico, y sus entradas describen lo que era cierto
#: cuando se escribieron.
DOCUMENTOS: Final = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "GLOSSARY.md",
    "docs/ROADMAP.md",
)

#: La columna del README que tiene que coincidir con `BROADCAST_TX`.
COLUMNA_DE_EMITIR: Final = "Firma y emite"

#: Celda vacía de la tabla: así se escribe «este modo no puede».
VACIO: Final = "—"


def _documento(nombre: str) -> str:
    return (RAIZ / nombre).read_text(encoding="utf-8")


def _tabla_de_modos() -> list[list[str]]:
    """Las filas de la tabla de modos del README, ya partidas en celdas.

    Se busca la cabecera por su última columna en vez de por el número de línea:
    la tabla se mueve cada vez que alguien añade una sección arriba, y una prueba
    que dependiera de la posición fallaría por un motivo que no tiene que ver con
    lo que vigila.
    """
    lineas = _documento("README.md").splitlines()
    for indice, linea in enumerate(lineas):
        if not linea.startswith("|") or COLUMNA_DE_EMITIR not in linea:
            continue
        filas: list[list[str]] = []
        for fila in lineas[indice + 2 :]:
            if not fila.startswith("|"):
                break
            filas.append([celda.strip() for celda in fila.strip().strip("|").split("|")])
        return filas
    raise AssertionError(
        f"no se encontró en README.md una tabla con una columna «{COLUMNA_DE_EMITIR}»"
    )


def _etiqueta(celda: str) -> str:
    return celda.strip().strip("`").strip()


def test_the_readme_table_shows_every_mode_there_is() -> None:
    """Un modo nuevo sin fila en el README es un modo que nadie puede descubrir."""
    en_tabla = {_etiqueta(fila[0]) for fila in _tabla_de_modos()}
    esperados = {modo.label for modo in OperationMode}
    assert en_tabla == esperados, (
        "los modos del README y los de `OperationMode` no coinciden; "
        f"sólo en el README: {sorted(en_tabla - esperados)}; "
        f"sólo en el código: {sorted(esperados - en_tabla)}"
    )


def test_the_readme_grants_broadcasting_exactly_where_the_code_does() -> None:
    """La columna «Firma y emite» dice lo mismo que `MODE_CAPABILITIES`.

    Es la comprobación central del fichero: si mañana otro modo gana
    `BROADCAST_TX`, o `EJECUCIÓN` lo pierde, esta prueba falla y obliga a decidir
    qué poner en el README en vez de dejar que se separen.
    """
    conceden = {modo.label for modo in modes_granting(Capability.BROADCAST_TX)}
    en_tabla = {
        _etiqueta(fila[0])
        for fila in _tabla_de_modos()
        if _etiqueta(fila[-1]) != VACIO
    }
    assert en_tabla == conceden, (
        "el README y `MODE_CAPABILITIES` discrepan sobre quién puede emitir; "
        f"el README concede de más: {sorted(en_tabla - conceden)}; "
        f"y de menos: {sorted(conceden - en_tabla)}"
    )


@pytest.mark.parametrize("documento", DOCUMENTOS)
def test_no_document_claims_the_removed_concept(documento: str) -> None:
    """`UNIMPLEMENTED_CAPABILITIES` ya no existe, y ningún documento lo nombra.

    Se vigila el nombre y no una frase porque el nombre era el ancla de todo el
    razonamiento que quedó obsoleto: mientras aparezca, alguien puede leerlo y
    creer que el concepto sigue en pie.
    """
    assert "UNIMPLEMENTED_CAPABILITIES" not in _documento(documento), (
        f"{documento} nombra `UNIMPLEMENTED_CAPABILITIES`, que ya no existe en el "
        f"código: la ejecución real está implementada y sólo la concede `EJECUCIÓN`"
    )


@pytest.mark.parametrize("documento", DOCUMENTOS)
def test_a_document_that_talks_about_signing_says_what_gates_it(documento: str) -> None:
    """Hablar de firmar y emitir obliga a decir qué lo autoriza.

    Esta es la comprobación que habría cazado el fallo original, y se escribe en
    positivo a propósito. Las dos anteriores buscan frases concretas —un nombre
    retirado, una manera de decir «ningún modo»—, y una lista negra siempre deja
    fuera la redacción que nadie previó: la frase de `AGENTS.md` era «La
    aplicación no firma ni emite transacciones», que no se parece a ninguna de
    las que se me habrían ocurrido prohibir.

    Exigir la mención de la barrera no depende de cómo se redacte la afirmación:
    vale para cualquier frase, porque lo que comprueba no es lo que el documento
    dice del firmar, sino que **diga también** qué hace falta para llegar a él.
    """
    texto = _documento(documento)
    habla_de_firmar = re.search(
        r"\b(firma|firmar|emite|emitir|SIGN_TX|BROADCAST_TX)\b", texto
    )
    if habla_de_firmar is None:
        pytest.skip(f"{documento} no habla de firmar ni emitir")
    assert re.search(r"EJECUCIÓN|execution\.enabled", texto), (
        f"{documento} habla de firmar o emitir («{habla_de_firmar.group()}») pero no "
        f"nombra en ningún sitio la barrera que lo autoriza — ni el modo EJECUCIÓN "
        f"ni el interruptor `execution.enabled` —, así que se lee como si la "
        f"aplicación pudiera hacerlo sin más"
    )


@pytest.mark.parametrize("documento", DOCUMENTOS)
def test_no_document_asserts_that_no_mode_can_sign(documento: str) -> None:
    """Nadie escribe la afirmación contraria, ni siquiera de pasada.

    La prueba de arriba vigila la **omisión** —hablar de firmar sin decir qué lo
    autoriza—; ésta vigila la **contradicción**. Son fallos distintos: un
    documento puede nombrar `EJECUCIÓN` en una sección y afirmar en otra que
    ningún modo concede firmar, y entonces la prueba de la omisión lo daría por
    bueno. Aquí se busca la afirmación por su forma y no por su redacción, porque
    «ningún modo los concede», «no están implementados en ningún modo» y «no los
    habilita ningún modo» son la misma frase dicha de tres maneras.
    """
    texto = _documento(documento)
    patron = re.compile(
        r"ning[úu]n\s+modo\s+(?:los\s+)?(?:concede|permite|habilita)"
        r"|no\s+est[áa]n\s+implementad",
        re.IGNORECASE,
    )
    encontrado = patron.search(texto)
    assert encontrado is None, (
        f"{documento} afirma «{encontrado.group()}», y el modo EJECUCIÓN sí concede "
        f"firmar y emitir"
    )
