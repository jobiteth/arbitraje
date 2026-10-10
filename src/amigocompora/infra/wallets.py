"""Varias carteras: el libro, el almacén cifrado y el proveedor que firma.

### El problema

Hasta ahora la aplicación tenía **una** cartera: la clave privada vivía en el
llavero del sistema bajo `app:execution_private_key` y `SpendingKeyProvider` la
resolvía. Eso cubre «tengo una clave», y no cubre «tengo varias identidades y
quiero elegir con cuál opero»: no había dónde guardar una segunda clave, ni una
dirección de sólo lectura, ni forma de saber de qué cartera salió un movimiento.

### Tres fuentes, una interfaz

Cada cartera declara de dónde sale su clave (`WalletSource`):

- `KEYSTORE`: la clave está **cifrada con una contraseña** en este mismo libro.
- `KEYRING`: la clave vive en el llavero del sistema, como siempre. Es la
  cartera heredada; se puede migrar a `KEYSTORE` sin borrar la copia.
- `WATCH`: no hay clave. La dirección se lee y recibe, pero no firma.

`WalletKeyProvider` implementa la **misma interfaz** que `SpendingKeyProvider`
—`available`, `address`, `get`, `require`, `env_var`—, así que toda la
aplicación —casos de uso de swap, predicción, retiros y puentes, y los gates de
la interfaz— sigue preguntando exactamente lo mismo y la **cartera activa**
manda sin tocarles una línea.

### El cifrado no se escribe a mano

El almacén es el **keystore estándar de Ethereum** (Web3 Secret Storage),
producido y leído por `eth-account`: scrypt + AES-128-CTR + keccak. Escribir una
caja propia sería la forma más rápida de perder dinero por un detalle sutil, y
además regala compatibilidad con cualquier herramienta del ecosistema. Los
parámetros de scrypt (n=16384) están elegidos por una razón medida: cifrar
~75 ms y descifrar ~35 ms, que es lo que permite **descifrar al firmar** en vez
de tener la clave descifrada en memoria todo el rato.

### Qué se guarda en memoria

Sólo la **contraseña de la sesión**, mientras la sesión esté desbloqueada:
firmar sin volver a teclearla es lo que hace usable la ejecución desatendida.
La clave privada descifrada **no se cachea nunca**: se devuelve, se usa y se
suelta, igual que hacía `SpendingKeyProvider` con la del llavero. `lock()` olvida
la contraseña.

### La cartera heredada no se toca hasta que haga falta

Mientras el libro no tenga **ningún cambio guardado**, el llavero manda: si
tiene clave, se enseña como una cartera virtual «1» (`source=KEYRING`) sin
escribir nada en disco, y `available/address/get/require` delegan en el
proveedor heredado con su comportamiento de siempre —incluido
`execution.allow_env_key`—. El primer cambio (añadir, renombrar, cambiar de
activa, borrar) materializa esa cartera en el libro y a partir de ahí manda el
libro: una instalación de antes sigue funcionando sin migrar nada a mano, y una
cartera que el usuario borra no reaparece en el arranque siguiente.

### Lo que este módulo NO hace

Ni decide qué es un saldo ni lee direcciones para eso: `WalletProfile` sigue
siendo el contrato de dominio. Ni escribe contraseñas o claves en un log, en un
mensaje de error o en un `repr`: lo que se registra es el identificador, la
etiqueta y la dirección, que son públicos por diseño.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import structlog
from eth_account import Account
from eth_keyfile.exceptions import EthKeyfileValueError

from amigocompora.domain.addresses import (
    is_evm_address,
    is_solana_address,
    require_evm_address,
    require_solana_address,
)
from amigocompora.domain.errors import (
    InvalidAmountError,
    KeyCustodyError,
    NoWalletError,
    WalletExistsError,
    WalletLockedError,
    WrongPasswordError,
)
from amigocompora.domain.wallet import WalletKind, WalletProfile
from amigocompora.infra.evm.signer import address_from_key
from amigocompora.infra.secrets import SpendingKeyProvider

_log = structlog.get_logger(__name__)

#: Nombre del fichero del libro dentro del directorio de configuración.
WALLETS_FILE_NAME: Final = "wallets.json"

#: Formato del fichero. Existe para poder migrar sin adivinar qué se escribió.
_FORMAT_VERSION: Final = 1

#: Parámetros de scrypt del almacén cifrado. n=16384 cuesta ~75 ms al cifrar y
#: ~35 ms al descifrar —medido—, deliberadamente por debajo del defecto de
#: `eth-account` (n=262144, ~600 ms): el descifrado ocurre **en cada firma**,
#: porque la clave no se cachea, y medio segundo por firma en el bucle de
#: eventos se nota. El parámetro está en el propio fichero, así que subirlo más
#: adelante no invalida lo ya guardado.
_KEYSTORE_KDF: Final = "scrypt"
_KEYSTORE_N: Final = 16384

#: Identificador de la cartera heredada mientras vive sólo en el llavero.
LEGACY_WALLET_ID: Final = "legacy"


class WalletSource(StrEnum):
    """De dónde sale la clave de una cartera.

    Son tres porque son tres cosas distintas para quien opera: una clave
    cifrada con contraseña propia (`KEYSTORE`), la del llavero del sistema
    (`KEYRING`, la que había antes de que existieran varias) y ninguna
    (`WATCH`: sólo se lee y recibe, no firma).
    """

    KEYSTORE = "keystore"
    KEYRING = "keyring"
    WATCH = "watch"

    @property
    def label(self) -> str:
        """El nombre de la fuente para la interfaz."""
        match self:
            case WalletSource.KEYSTORE:
                return "cifrada"
            case WalletSource.KEYRING:
                return "llavero"
            case _:
                return "observación"


@dataclass(frozen=True, slots=True)
class StoredWallet:
    """Una cartera del libro: identidad, fuente y —si toca— su keystore.

    La validación de la etiqueta y de la dirección se delega en
    `WalletProfile`, que es el contrato de dominio: aquí no se repiten reglas,
    se reutilizan. El `keystore` es el blob cifrado del estándar Web3 Secret
    Storage; **no contiene la clave en claro** y queda fuera del `repr` para que
    un registro de depuración no escupa criptografía por la consola.
    """

    wallet_id: str
    label: str
    address: str
    kind: WalletKind
    source: WalletSource
    keystore: Mapping[str, Any] | None = field(default=None, repr=False)
    created_at: str = ""

    def __post_init__(self) -> None:
        profile = WalletProfile(
            wallet_id=self.wallet_id,
            label=self.label,
            kind=self.kind,
            address=self.address,
        )
        object.__setattr__(self, "address", profile.address)
        if not self.created_at:
            object.__setattr__(self, "created_at", datetime.now(UTC).isoformat(timespec="seconds"))
        if self.source is WalletSource.KEYSTORE and not self.keystore:
            raise InvalidAmountError("una cartera cifrada necesita su keystore")
        if self.source is not WalletSource.KEYSTORE and self.keystore is not None:
            raise InvalidAmountError("sólo una cartera cifrada puede llevar keystore")

    def profile(self) -> WalletProfile:
        """La vista de dominio, que es lo que leen las páginas."""
        return WalletProfile(
            wallet_id=self.wallet_id,
            label=self.label,
            kind=self.kind,
            address=self.address,
        )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.wallet_id,
            "label": self.label,
            "address": self.address,
            "kind": str(self.kind),
            "source": str(self.source),
            "created_at": self.created_at,
        }
        if self.keystore is not None:
            payload["keystore"] = dict(self.keystore)
        return payload

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> StoredWallet:
        """Lee una entrada del fichero. Lanza si no describe una cartera.

        No se defiende aquí: quien llama —el libro— es quien decide que una
        entrada rota se salta sin llevarse por delante a las demás.
        """
        keystore = raw.get("keystore")
        if keystore is not None and not isinstance(keystore, dict):
            raise ValueError("«keystore» no es un objeto")
        return cls(
            wallet_id=_required_text(raw, "id"),
            label=_required_text(raw, "label"),
            address=_required_text(raw, "address"),
            kind=WalletKind(_required_text(raw, "kind")),
            source=WalletSource(_required_text(raw, "source")),
            keystore=dict(keystore) if isinstance(keystore, dict) else None,
            created_at=str(raw.get("created_at") or ""),
        )


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"«{key}» falta o no es texto")
    return value.strip()


def encrypt_private_key(private_key: str, password: str) -> dict[str, Any]:
    """Cifra una clave privada con la contraseña, en keystore estándar.

    La clave se valida **antes** de llegar aquí (con `address_from_key`, que no
    la cita en ningún mensaje), así que un fallo de `Account.encrypt` es un
    problema del almacén y no de lo que se escribió. El error no encadena el
    original a propósito: si `eth-account` llegara a repetir el valor en su
    mensaje, el encadenado lo arrastraría hasta el sitio menos pensado.

    Se pasa como entero —y no como la cadena `0x…`— porque es el mismo material
    de clave para `eth-account` (32 bytes) y porque los tipos del binding exigen
    su alias `HexStr`, que aquí no aporta nada: el formato ya lo garantizó
    `address_from_key`.
    """
    try:
        material = int(private_key, 16)
        return Account.encrypt(material, password, kdf=_KEYSTORE_KDF, iterations=_KEYSTORE_N)
    except (TypeError, ValueError):
        raise KeyCustodyError("no se pudo cifrar la clave privada en el almacén") from None


def decrypt_private_key(keystore: Mapping[str, Any], password: str) -> str:
    """Descifra un keystore y devuelve la clave privada en hex `0x…`.

    Un `EthKeyfileValueError` significa, en este formato, que el MAC no cuadra:
    la contraseña no abre este almacén. Cualquier otro fallo —una entrada sin
    los campos del formato, hex ilegible— es un almacén dañado, que es un
    problema distinto y merece un mensaje distinto: quien lo lee puede hacer
    algo con «contraseña incorrecta» y nada con «error de descifrado».
    """
    try:
        key = Account.decrypt(dict(keystore), password)
    except EthKeyfileValueError as error:
        raise WrongPasswordError("la contraseña no abre el almacén cifrado de carteras") from error
    except (KeyError, TypeError, ValueError) as error:
        raise KeyCustodyError(
            "el almacén cifrado de carteras está dañado o no es un keystore"
        ) from error
    return key.to_0x_hex()


class WalletBook:
    """El libro de carteras en disco, con la escritura atómica y la lectura tolerante.

    El formato es un objeto con versión, el identificador de la activa y la
    lista de carteras. `seeded` merece una línea: es `True` en cuanto el libro
    tiene **cualquier** cambio guardado, y es lo que apaga la cartera heredada
    implícita. Sin él, borrar la cartera del llavero no se podría distinguir de
    no haber tocado nada, y reaparecería en el arranque siguiente — que es la
    peor forma de borrar algo: la que parece funcionar.

    Se imita a `UserTokenStore` a propósito: escritura a temporal + `replace`,
    y una lectura que ante un JSON roto avisa **una vez** y sigue con lo que
    haya, porque perder una lista de atajos cuesta menos que no abrir la
    aplicación.
    """

    __slots__ = ("_active_id", "_loaded", "_path", "_seeded", "_wallets", "_warned")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._wallets: list[StoredWallet] = []
        self._active_id: str | None = None
        self._seeded = False
        self._loaded = False
        self._warned = False

    def __repr__(self) -> str:
        # Sin recorrer la lista: un `repr` no debe tocar el disco ni el llavero.
        return f"WalletBook(path={str(self._path)!r}, seeded={self._seeded})"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def seeded(self) -> bool:
        """Si el libro tiene algún cambio guardado (y la heredada ya no es implícita)."""
        self.load()
        return self._seeded

    @property
    def active_id(self) -> str | None:
        """El identificador guardado de la activa, sin resolver el que toca por defecto."""
        self.load()
        return self._active_id

    # --------------------------------------------------------------- lectura #
    def load(self) -> tuple[StoredWallet, ...]:
        if not self._loaded:
            self._read()
        return tuple(self._wallets)

    def get(self, wallet_id: str) -> StoredWallet | None:
        self.load()
        for wallet in self._wallets:
            if wallet.wallet_id == wallet_id:
                return wallet
        return None

    def active(self) -> StoredWallet | None:
        """La activa: la marcada, y si ya no está, la primera. `None` si no hay ninguna."""
        self.load()
        if not self._wallets:
            return None
        if self._active_id is not None:
            found = self.get(self._active_id)
            if found is not None:
                return found
        return self._wallets[0]

    def _read(self) -> None:
        self._loaded = True
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            self._warn_once(
                "wallet_book.unreadable",
                reason=str(error),
                hint=(
                    "se empieza con el libro vacío; las claves cifradas no se "
                    "recuperan de un fichero que no se puede leer"
                ),
            )
            return
        if not isinstance(raw, dict):
            self._warn_once("wallet_book.not_an_object", kind=type(raw).__name__)
            return

        entries = raw.get("wallets")
        if not isinstance(entries, list):
            self._warn_once("wallet_book.no_list", kind=type(entries).__name__)
            entries = []

        parsed: list[StoredWallet] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                wallet = StoredWallet.from_json(entry)
            except (InvalidAmountError, KeyError, TypeError, ValueError):
                # Una entrada rota no se lleva por delante a las demás.
                continue
            clave = wallet.address.lower()
            if clave in seen:
                continue
            seen.add(clave)
            parsed.append(wallet)

        self._wallets = parsed
        self._seeded = bool(raw.get("seeded", False))
        active = raw.get("active")
        if isinstance(active, str) and any(w.wallet_id == active for w in parsed):
            self._active_id = active

    def _warn_once(self, event: str, **fields: Any) -> None:
        if self._warned:
            return
        self._warned = True
        _log.warning(event, **fields)

    # -------------------------------------------------------------- escritura #
    def add(
        self,
        *,
        label: str,
        address: str,
        kind: WalletKind,
        source: WalletSource,
        keystore: Mapping[str, Any] | None = None,
    ) -> StoredWallet:
        """Añade una cartera y la deja activa. La identidad es la dirección.

        Se deduplica por dirección —comparada sin distinguir mayúsculas, como en
        el resto de la aplicación— porque dos entradas de la misma dirección
        serían la misma cartera dos veces: firmarían lo mismo y confundirían
        cualquier recuento.
        """
        self.load()
        self._refuse_duplicate(address)
        wallet = StoredWallet(
            wallet_id=uuid.uuid4().hex[:12],
            label=label,
            address=address,
            kind=kind,
            source=source,
            keystore=keystore,
        )
        self._wallets.append(wallet)
        self._active_id = wallet.wallet_id
        self._write()
        _log.info(
            "wallet_book.added",
            wallet_id=wallet.wallet_id,
            label=wallet.label,
            address=wallet.address,
            source=str(wallet.source),
        )
        return wallet

    def replace_wallet(self, wallet: StoredWallet) -> StoredWallet:
        """Sustituye una entrada por la misma cartera con otro contenido.

        Es lo que usa la migración del llavero al almacén cifrado: la cartera es
        la misma —mismo identificador, misma etiqueta, misma dirección— y lo que
        cambia es de dónde sale su clave.
        """
        self.load()
        for index, existing in enumerate(self._wallets):
            if existing.wallet_id == wallet.wallet_id:
                self._wallets[index] = wallet
                self._write()
                return wallet
        raise InvalidAmountError(f"no hay ninguna cartera «{wallet.wallet_id}» en el libro")

    def rename(self, wallet_id: str, label: str) -> StoredWallet:
        wallet = self.get(wallet_id)
        if wallet is None:
            raise InvalidAmountError(f"no hay ninguna cartera «{wallet_id}» en el libro")
        # `replace` vuelve a pasar por la validación de la etiqueta.
        renamed = replace(wallet, label=label)
        return self.replace_wallet(renamed)

    def set_active(self, wallet_id: str) -> StoredWallet:
        wallet = self.get(wallet_id)
        if wallet is None:
            raise InvalidAmountError(f"no hay ninguna cartera «{wallet_id}» en el libro")
        self._active_id = wallet_id
        self._write()
        return wallet

    def remove(self, wallet_id: str) -> bool:
        """Borra una cartera. Si era la activa, pasa a serlo la primera que quede.

        Borrar es definitivo **en el libro**: la copia que hubiera en el llavero
        no se toca, y eso es lo que se dice en la interfaz antes de confirmar.
        """
        self.load()
        kept = [w for w in self._wallets if w.wallet_id != wallet_id]
        if len(kept) == len(self._wallets):
            return False
        self._wallets = kept
        if self._active_id == wallet_id:
            self._active_id = kept[0].wallet_id if kept else None
        self._write()
        _log.info("wallet_book.removed", wallet_id=wallet_id)
        return True

    def mark_seeded(self) -> None:
        """Marca el libro como tocado sin añadir nada.

        Es lo que hace que borrar la **cartera heredada virtual** se recuerde:
        no había entrada que quitar, y lo que hay que guardar es que ya no debe
        aparecer sola.
        """
        self.load()
        self._write()

    def _refuse_duplicate(self, address: str) -> None:
        wanted = address.strip().lower()
        for wallet in self._wallets:
            if wallet.address.lower() == wanted:
                raise WalletExistsError(
                    f"la dirección {wallet.address} ya está en el libro como la "
                    f"cartera «{wallet.label}»"
                )

    def _write(self) -> None:
        self.load()
        self._seeded = True
        payload: dict[str, Any] = {
            "version": _FORMAT_VERSION,
            "seeded": True,
            "active": self._active_id,
            "wallets": [wallet.to_json() for wallet in self._wallets],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            scratch = self._path.with_suffix(self._path.suffix + ".tmp")
            scratch.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            scratch.replace(self._path)
        except OSError as error:
            # Igual que el almacén de tokens: el cambio vale en esta sesión y no
            # se recuerda en la próxima. Abortar la operación por eso sería
            # convertir una molestia en un error.
            _log.warning(
                "wallet_book.write_failed",
                path=str(self._path),
                reason=str(error),
                hint="el cambio de carteras funciona en esta sesión pero no se recordará",
            )


class WalletKeyProvider:
    """La cartera activa del libro, para todo lo que firma y todo lo que pinta.

    Sustituye a `SpendingKeyProvider` en el contenedor: misma interfaz, misma
    política de custodia, y por debajo el libro. Las reglas por fuente son las
    que separan una cartera de otra:

    - `KEYSTORE`: se descifra **en el momento de firmar**, con la contraseña de
      la sesión; sin sesión desbloqueada, `require()` lanza `WalletLockedError`
      con un mensaje que dice dónde se desbloquea.
    - `KEYRING`: se delega en el proveedor heredado, con su `allow_env_key` y
      todo, para que la instalación de antes se comporte exactamente igual.
    - `WATCH`: `address()` responde —es la dirección que se lee y el destino por
      omisión— y `available()` dice que no, porque no hay con qué firmar.

    La contraseña de sesión se guarda aquí y sólo aquí, en memoria. La clave
    descifrada no se guarda en ningún sitio.
    """

    __slots__ = ("_book", "_legacy", "_password")

    def __init__(self, book: WalletBook, *, legacy: SpendingKeyProvider) -> None:
        self._book = book
        self._legacy = legacy
        self._password: str | None = None

    def __repr__(self) -> str:
        return f"WalletKeyProvider(book={self._book!r}, unlocked={self._password is not None})"

    # ------------------------------------------------------------- catálogo #
    def wallets(self) -> tuple[StoredWallet, ...]:
        """Todas las carteras, con la heredada delante mientras sea implícita."""
        entries = self._book.load()
        legacy = self._legacy_wallet()
        if legacy is None:
            return entries
        if any(w.address.lower() == legacy.address.lower() for w in entries):
            return entries
        return (legacy, *entries)

    def active(self) -> StoredWallet | None:
        entries = self.wallets()
        if not entries:
            return None
        chosen = self._book.active_id
        if chosen is not None:
            for wallet in entries:
                if wallet.wallet_id == chosen:
                    return wallet
        return entries[0]

    def next_label(self) -> str:
        """El primer número libre: «1», «2», «3»… Es el nombre por omisión.

        Se cuentan **todas** las etiquetas y no sólo las numéricas: dos carteras
        pueden llamarse igual —la etiqueta no es identidad, la dirección sí—,
        pero el número que se propone por defecto no debe repetirse.
        """
        used = {wallet.label.strip() for wallet in self.wallets()}
        number = 1
        while str(number) in used:
            number += 1
        return str(number)

    def get_wallet(self, wallet_id: str) -> StoredWallet | None:
        for wallet in self.wallets():
            if wallet.wallet_id == wallet_id:
                return wallet
        return None

    def set_active(self, wallet_id: str) -> StoredWallet:
        legacy = self._legacy_wallet()
        if legacy is not None and wallet_id == legacy.wallet_id:
            materialized = self._materialize(legacy)
            return self._book.set_active(materialized.wallet_id)
        return self._book.set_active(wallet_id)

    def rename(self, wallet_id: str, label: str) -> StoredWallet:
        legacy = self._legacy_wallet()
        if legacy is not None and wallet_id == legacy.wallet_id:
            materialized = self._materialize(legacy)
            return self._book.rename(materialized.wallet_id, label)
        return self._book.rename(wallet_id, label)

    def remove(self, wallet_id: str) -> bool:
        """Borra una cartera. Devuelve si estaba.

        Borrar la heredada virtual no tiene entrada que quitar: lo que se guarda
        es que ya no debe aparecer sola, y la clave sigue en el llavero hasta
        que alguien la borre desde Credenciales. Es lo que hace que el borrado
        de verdad —el que no resucita— se pueda ofrecer sin tocar el respaldo.
        """
        legacy = self._legacy_wallet()
        if legacy is not None and wallet_id == legacy.wallet_id:
            self._book.mark_seeded()
            self._forget_password_if_unused()
            _log.info("wallets.legacy_hidden")
            return True
        removed = self._book.remove(wallet_id)
        if removed:
            self._forget_password_if_unused()
        return removed

    # -------------------------------------------------------------- sesión #
    def is_unlocked(self) -> bool:
        """Si la sesión tiene la contraseña en memoria."""
        return self._password is not None

    def is_locked(self) -> bool:
        """Si la activa es cifrada y no hay contraseña de sesión."""
        wallet = self.active()
        return (
            wallet is not None
            and wallet.source is WalletSource.KEYSTORE
            and self._password is None
        )

    def has_password(self) -> bool:
        """Si ya hay alguna clave cifrada, es decir, si la contraseña existe."""
        return any(w.source is WalletSource.KEYSTORE for w in self._book.load())

    def unlock(self, password: str) -> bool:
        """Verifica la contraseña contra el almacén y, si abre, la recuerda.

        Se verifica descifrando de verdad un keystore: no hay un hash de la
        contraseña que comprobar aparte, y no hace falta — el MAC del keystore
        ya responde a la única pregunta que importa, que es si esta contraseña
        abre estas claves.
        """
        vault = self._vault_wallet()
        if vault is None or vault.keystore is None:
            return False
        try:
            decrypt_private_key(vault.keystore, password)
        except WrongPasswordError:
            return False
        self._password = password
        _log.info("wallets.unlocked", wallet_id=vault.wallet_id)
        return True

    def lock(self) -> None:
        """Olvida la contraseña de la sesión. La clave descifrada no se guardaba."""
        self._password = None
        _log.info("wallets.locked")

    @property
    def env_var(self) -> str:
        """La variable donde buscaría la clave la cartera heredada."""
        return self._legacy.env_var

    def env_lookup(self) -> str | None:
        """La variable de la que saldría la clave de la activa, si es que sale de una.

        Sólo la cartera heredada puede leerse del entorno —una clave cifrada no,
        por diseño—, así que con cualquier otra activa la respuesta es que no:
        nombrar una variable sería prometer una clave que no va a aparecer por
        ahí. Como en el proveedor heredado, se pregunta cuando el llavero ya ha
        fallado, que es donde tiene sentido.
        """
        wallet = self.active()
        if wallet is not None and wallet.source is not WalletSource.KEYRING:
            return None
        if wallet is None and self._book.seeded:
            return None
        return self._legacy.env_lookup()

    # ------------------------------------------------------------ interfaz #
    def available(self) -> bool:
        """Si hay con qué **firmar ahora mismo**. No lanza: se pregunta para pintar.

        Una cartera cifrada bloqueada no está disponible —no hay contraseña—;
        una en observación tampoco —no hay clave—; la heredada, lo que diga el
        proveedor del llavero. Que la activa sea *leíble* no entra aquí: para eso
        está `address()`, que responde sin clave.
        """
        wallet = self.active()
        if wallet is None:
            return False if self._book.seeded else self._legacy.available()
        match wallet.source:
            case WalletSource.KEYRING:
                return self._legacy.available()
            case WalletSource.WATCH:
                return False
            case _:
                return self._password is not None

    def address(self) -> str | None:
        """La dirección de la activa, sin descifrar nada.

        Para la cartera del llavero se deriva **en vivo** en vez de devolver la
        guardada: si esa clave cambia desde Credenciales, la dirección que se
        enseña es la que firmaría, y no una foto vieja que apunta a otra cuenta.
        """
        wallet = self.active()
        if wallet is None:
            return None if self._book.seeded else self._legacy.address()
        if wallet.source is WalletSource.KEYRING:
            return self._legacy.address() or wallet.address
        return wallet.address

    def get(self) -> str | None:
        """La clave privada de la activa, o `None` si no hay ninguna.

        Mismos matices que `SpendingKeyProvider.get`: `KeyCustodyError` cuando
        podría haber clave pero el almacén no responde, `WalletLockedError`
        cuando la hay pero está cifrada y la sesión no está desbloqueada.
        """
        wallet = self.active()
        if wallet is not None:
            match wallet.source:
                case WalletSource.WATCH:
                    return None
                case WalletSource.KEYRING:
                    return self._legacy.get()
                case _:
                    if self._password is None:
                        raise WalletLockedError(
                            f"la cartera «{wallet.label}» está cifrada y la sesión no "
                            f"está desbloqueada. Desbloquéala en la tarjeta Cartera "
                            f"(menú de la cabecera → «Desbloquear cartera»)."
                        )
                    if wallet.keystore is None:
                        return None
                    return decrypt_private_key(wallet.keystore, self._password)
        if self._book.seeded:
            return None
        return self._legacy.get()

    def require(self) -> str:
        """Como `get`, pero lanza si no hay con qué firmar y dice por qué."""
        wallet = self.active()
        if wallet is not None:
            match wallet.source:
                case WalletSource.WATCH:
                    raise NoWalletError(
                        f"la cartera «{wallet.label}» está en modo observación: no "
                        f"guarda ninguna clave con la que firmar. Elige una cartera "
                        f"con clave en la tarjeta Cartera para operar."
                    )
                case WalletSource.KEYRING:
                    return self._legacy.require()
        elif not self._book.seeded:
            # Ni cartera en el libro ni nada guardado: se responde con el
            # proveedor heredado, que es quien sabe decir dónde se configura la
            # clave cuando todavía no se ha usado ninguna cartera del libro.
            return self._legacy.require()
        key = self.get()
        if key is None:
            raise NoWalletError(
                "no hay ninguna cartera con clave configurada, así que no hay con qué "
                "firmar. Añádela en la tarjeta Cartera."
            )
        return key

    # ------------------------------------------------------------- acciones #
    def add_signing(self, private_key: str, label: str, password: str) -> StoredWallet:
        """Añade una cartera con clave privada, cifrándola con la contraseña.

        Si la dirección ya estaba en modo observación, la entrada se **asciende**
        —misma cartera, ahora con clave— en vez de duplicarla: la dirección es
        la identidad, y tenerla dos veces haría que la activa pudiera firmar
        algo que la interfaz enseña como solo-lectura.

        Con claves ya guardadas, la contraseña tiene que ser la misma: todas las
        carteras comparten un único almacén, y admitir otra aquí dejaría claves
        que el desbloqueo de la sesión no abriría.
        """
        address = address_from_key(private_key)
        existing = self._find_by_address(address)
        if existing is not None and existing.source is WalletSource.KEYSTORE:
            raise WalletExistsError(
                f"la dirección {existing.address} ya está en el libro como la cartera "
                f"«{existing.label}» y su clave ya está guardada"
            )

        self._check_password_against_vault(password)

        keystore = encrypt_private_key(private_key, password)
        self._password = password

        if existing is not None:
            stored = self._book.get(existing.wallet_id) or self._materialize(existing)
            upgraded = self._book.replace_wallet(
                replace(stored, source=WalletSource.KEYSTORE, keystore=keystore)
            )
            self._book.set_active(upgraded.wallet_id)
            _log.info(
                "wallets.upgraded",
                wallet_id=upgraded.wallet_id,
                address=upgraded.address,
            )
            return upgraded

        legacy = self._legacy_wallet()
        if legacy is not None:
            # La heredada se materializa antes, para no perderla al escribir.
            self._materialize(legacy)
        return self._book.add(
            label=label,
            address=address,
            kind=WalletKind.EVM,
            source=WalletSource.KEYSTORE,
            keystore=keystore,
        )

    def add_watch(self, address: str, label: str) -> StoredWallet:
        """Añade una cartera de sólo lectura, con la familia que diga su forma."""
        value = address.strip()
        if is_evm_address(value):
            kind = WalletKind.EVM
            normalized = require_evm_address(value, "cartera")
        elif is_solana_address(value):
            kind = WalletKind.SOLANA
            normalized = require_solana_address(value, "cartera")
        else:
            raise InvalidAmountError(
                f"«{value}» no tiene forma de dirección EVM (0x…) ni de dirección de "
                f"Solana, así que no se puede añadir ni leer"
            )

        existing = self._find_by_address(normalized)
        if existing is not None:
            raise WalletExistsError(self._duplicate_message(existing))

        legacy = self._legacy_wallet()
        if legacy is not None:
            self._materialize(legacy)
        wallet = self._book.add(
            label=label,
            address=normalized,
            kind=kind,
            source=WalletSource.WATCH,
        )
        _log.info("wallets.watch_added", wallet_id=wallet.wallet_id, address=wallet.address)
        return wallet

    def reveal(self, password: str) -> str:
        """Devuelve la clave privada de la activa, tras verificar la contraseña.

        Se pide la contraseña **siempre**, incluso con la sesión desbloqueada:
        este es el único sitio donde una clave privada se pone en pantalla, y
        que enseñarla cueste un gesto deliberado es la diferencia entre una
        decisión y un accidente.

        Para la cartera heredada, además, migra: la clave del llavero se cifra
        con esa contraseña y pasa al libro. La copia del llavero no se borra
        —es el respaldo— y se dice en la interfaz.
        """
        wallet = self.active()
        if wallet is None:
            raise NoWalletError(
                "no hay ninguna cartera activa de la que enseñar la clave. Añádela "
                "en la tarjeta Cartera."
            )
        if wallet.source is WalletSource.WATCH:
            raise NoWalletError(
                f"la cartera «{wallet.label}» está en modo observación: no guarda "
                f"ninguna clave privada que enseñar."
            )
        if wallet.source is WalletSource.KEYRING:
            # La migración cifra con esta contraseña, así que tiene que ser la
            # del almacén si ya existe: si no, la clave recién migrada quedaría
            # en un almacén que el desbloqueo de la sesión no abriría.
            self._check_password_against_vault(password)
            key = self._legacy.require()
            keystore = encrypt_private_key(key, password)
            stored = self._book.get(wallet.wallet_id) or self._materialize(wallet)
            self._book.replace_wallet(
                replace(stored, source=WalletSource.KEYSTORE, keystore=keystore)
            )
            self._password = password
            _log.info(
                "wallets.key_migrated",
                wallet_id=stored.wallet_id,
                address=stored.address,
            )
            return key

        if wallet.keystore is None:
            raise KeyCustodyError(f"la cartera «{wallet.label}» no tiene keystore que abrir")
        key = decrypt_private_key(wallet.keystore, password)
        if address_from_key(key).lower() != wallet.address.lower():
            raise KeyCustodyError(
                "el keystore no se corresponde con la dirección de la cartera: no se "
                "enseña una clave que no es la suya"
            )
        self._password = password
        return key

    # -------------------------------------------------------------- interno #
    def _check_password_against_vault(self, password: str) -> None:
        """Exige que la contraseña sea la del almacén ya existente, si lo hay.

        Todas las carteras cifradas comparten una única contraseña —la sesión
        desbloquea el almacén entero—, así que admitir aquí una distinta dejaría
        una clave que el desbloqueo de la sesión no abriría. Se verifica
        descifrando de verdad un keystore: el MAC ya responde a la única
        pregunta que importa, y el mensaje no cita lo que se escribió.
        """
        vault = self._vault_wallet()
        if vault is None or vault.keystore is None:
            return
        try:
            decrypt_private_key(vault.keystore, password)
        except WrongPasswordError as error:
            raise WrongPasswordError(
                "la contraseña no es la de las claves ya guardadas: todas las "
                "carteras comparten una, y esta no la abre"
            ) from error

    def _legacy_wallet(self) -> StoredWallet | None:
        """La cartera del llavero como cartera implícita, si aún manda el llavero."""
        if self._book.seeded:
            return None
        address = self._legacy.address()
        if address is None:
            return None
        return StoredWallet(
            wallet_id=LEGACY_WALLET_ID,
            label="1",
            address=address,
            kind=WalletKind.EVM,
            source=WalletSource.KEYRING,
        )

    def _materialize(self, wallet: StoredWallet) -> StoredWallet:
        """Escribe la heredada implícita en el libro, conservando su etiqueta."""
        stored = self._book.add(
            label=wallet.label,
            address=wallet.address,
            kind=wallet.kind,
            source=wallet.source,
        )
        _log.info("wallets.legacy_materialized", wallet_id=stored.wallet_id)
        return stored

    def _vault_wallet(self) -> StoredWallet | None:
        """La cartera cifrada contra la que verificar la contraseña, si hay alguna."""
        vault = [w for w in self._book.load() if w.source is WalletSource.KEYSTORE]
        if not vault:
            return None
        active = self.active()
        if active is not None:
            for wallet in vault:
                if wallet.wallet_id == active.wallet_id:
                    return wallet
        return vault[0]

    def _find_by_address(self, address: str) -> StoredWallet | None:
        wanted = address.strip().lower()
        for wallet in self.wallets():
            if wallet.address.lower() == wanted:
                return wallet
        return None

    def _duplicate_message(self, existing: StoredWallet) -> str:
        if existing.source is WalletSource.WATCH:
            return (
                f"la dirección {existing.address} ya está en el libro como la cartera "
                f"«{existing.label}», en modo observación"
            )
        return (
            f"la dirección {existing.address} ya está en el libro como la cartera "
            f"«{existing.label}»"
        )

    def _forget_password_if_unused(self) -> None:
        """Suelta la contraseña cuando ya no queda ninguna clave cifrada que la use."""
        if not self.has_password():
            self._password = None
