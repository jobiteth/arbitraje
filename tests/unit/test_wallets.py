"""Tests del libro de carteras, del almacén cifrado y del proveedor multi-cartera.

Lo que se fija aquí son las decisiones que no se pueden deducir leyendo la
interfaz: que la cartera heredada del llavero sigue funcionando **sin migrar
nada**, que el primer cambio la materializa y a partir de ahí manda el libro,
que borrar la heredada no resucita en el arranque siguiente, y que la clave
privada no aparece en claro ni en el fichero, ni en un mensaje de error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amigocompora.domain.errors import (
    InvalidAmountError,
    KeyCustodyError,
    NoWalletError,
    WalletExistsError,
    WalletLockedError,
    WrongPasswordError,
)
from amigocompora.domain.wallet import WalletKind
from amigocompora.infra.evm.signer import address_from_key
from amigocompora.infra.secrets import (
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    SpendingKeyProvider,
    app_secret_key,
)
from amigocompora.infra.wallets import (
    LEGACY_WALLET_ID,
    StoredWallet,
    WalletBook,
    WalletKeyProvider,
    WalletSource,
    decrypt_private_key,
    encrypt_private_key,
)

# Claves de desarrollo de Hardhat: públicas, conocidas y sin fondos. Nunca una
# clave real en un test — el repositorio no es sitio para material de firma.
CLAVE = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
CLAVE_2 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
OTRA_DIRECCION = "0x000000000000000000000000000000000000dEaD"
OTRA_DIRECCION_2 = "0x000000000000000000000000000000000000beef"
DIRECCION_SOLANA = "So11111111111111111111111111111111111111112"
#: La contraseña de los tests **no** es la de nadie: la de verdad no se escribe
#: en el repositorio, entre otras cosas porque un test no debe fijarla.
CONTRASENA = "contrasena-de-prueba"


def _build(
    tmp_path: Path,
    *,
    keyring_key: str | None = None,
    allow_env: bool = False,
) -> tuple[WalletKeyProvider, WalletBook, InMemorySecretStore]:
    store = InMemorySecretStore()
    if keyring_key is not None:
        store.set(app_secret_key(PRIVATE_KEY_SECRET), keyring_key)
    legacy = SpendingKeyProvider(store, allow_env_fallback=allow_env)
    book = WalletBook(tmp_path / "wallets.json")
    return WalletKeyProvider(book, legacy=legacy), book, store


# --------------------------------------------------------------------------- #
# El libro
# --------------------------------------------------------------------------- #
def test_book_writes_and_reads_back(tmp_path: Path) -> None:
    book = WalletBook(tmp_path / "wallets.json")
    added = book.add(
        label="Mirón",
        address=OTRA_DIRECCION,
        kind=WalletKind.EVM,
        source=WalletSource.WATCH,
    )

    reopened = WalletBook(tmp_path / "wallets.json")
    stored = reopened.load()
    assert len(stored) == 1
    assert stored[0].wallet_id == added.wallet_id
    assert stored[0].label == "Mirón"
    assert stored[0].address == OTRA_DIRECCION
    assert stored[0].source is WalletSource.WATCH
    active = reopened.active()
    assert active is not None
    assert active.wallet_id == added.wallet_id


def test_book_ignores_corrupt_json_and_keeps_going(tmp_path: Path) -> None:
    path = tmp_path / "wallets.json"
    path.write_text("{esto no es json", encoding="utf-8")

    book = WalletBook(path)
    assert book.load() == ()
    assert book.active() is None


def test_book_skips_a_broken_entry_without_losing_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "wallets.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "seeded": True,
                "active": None,
                "wallets": [
                    {
                        "id": "a",
                        "label": "1",
                        "address": OTRA_DIRECCION,
                        "kind": "inventado",
                        "source": "watch",
                    },
                    {
                        "id": "b",
                        "label": "2",
                        "address": OTRA_DIRECCION_2,
                        "kind": "evm",
                        "source": "watch",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    stored = WalletBook(path).load()
    assert [w.wallet_id for w in stored] == ["b"]


def test_book_refuses_duplicate_address_ignoring_case(tmp_path: Path) -> None:
    book = WalletBook(tmp_path / "wallets.json")
    book.add(
        label="1",
        address=OTRA_DIRECCION,
        kind=WalletKind.EVM,
        source=WalletSource.WATCH,
    )
    with pytest.raises(WalletExistsError):
        book.add(
            label="2",
            address=OTRA_DIRECCION.lower(),
            kind=WalletKind.EVM,
            source=WalletSource.WATCH,
        )


def test_book_remove_of_active_falls_back_to_the_first_remaining(tmp_path: Path) -> None:
    book = WalletBook(tmp_path / "wallets.json")
    first = book.add(
        label="1", address=OTRA_DIRECCION, kind=WalletKind.EVM, source=WalletSource.WATCH
    )
    second = book.add(
        label="2", address=OTRA_DIRECCION_2, kind=WalletKind.EVM, source=WalletSource.WATCH
    )

    assert book.remove(second.wallet_id) is True
    active = book.active()
    assert active is not None
    assert active.wallet_id == first.wallet_id
    assert book.remove("no-existe") is False


def test_book_rename_validates_the_label(tmp_path: Path) -> None:
    book = WalletBook(tmp_path / "wallets.json")
    wallet = book.add(
        label="1", address=OTRA_DIRECCION, kind=WalletKind.EVM, source=WalletSource.WATCH
    )
    renamed = book.rename(wallet.wallet_id, "Ahorros")
    assert renamed.label == "Ahorros"
    with pytest.raises(InvalidAmountError):
        book.rename(wallet.wallet_id, "x" * 60)


def test_stored_wallet_validates_the_address() -> None:
    """La dirección se acepta en cualquier grafía válida y se rechaza si no lo es.

    No se re-checksumea: la grafía que el usuario pegó es la que verá, y la
    comparación de duplicados —que sí importa— va en minúsculas.
    """
    wallet = StoredWallet(
        wallet_id="a",
        label="1",
        address=OTRA_DIRECCION.lower(),
        kind=WalletKind.EVM,
        source=WalletSource.WATCH,
    )
    assert wallet.address.lower() == OTRA_DIRECCION.lower()

    with pytest.raises(InvalidAmountError):
        StoredWallet(
            wallet_id="a",
            label="1",
            address="no-es-una-direccion",
            kind=WalletKind.EVM,
            source=WalletSource.WATCH,
        )


# --------------------------------------------------------------------------- #
# El cifrado
# --------------------------------------------------------------------------- #
def test_encrypt_and_decrypt_roundtrip_without_plaintext() -> None:
    keystore = encrypt_private_key(CLAVE, CONTRASENA)
    assert decrypt_private_key(keystore, CONTRASENA) == CLAVE

    blob = json.dumps(keystore).lower()
    assert CLAVE.lower() not in blob
    assert CLAVE[2:].lower() not in blob


def test_decrypt_with_wrong_password_says_nothing_about_secrets() -> None:
    keystore = encrypt_private_key(CLAVE, CONTRASENA)
    with pytest.raises(WrongPasswordError) as caught:
        decrypt_private_key(keystore, "otra-cosa")
    message = str(caught.value)
    assert "otra-cosa" not in message
    assert CONTRASENA not in message
    assert CLAVE not in message


def test_decrypt_of_a_broken_keystore_is_a_custody_problem() -> None:
    with pytest.raises(KeyCustodyError):
        decrypt_private_key({}, CONTRASENA)


# --------------------------------------------------------------------------- #
# El proveedor: la cartera heredada
# --------------------------------------------------------------------------- #
def test_without_any_wallet_there_is_nothing_to_sign(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    assert provider.available() is False
    assert provider.address() is None
    assert provider.wallets() == ()
    with pytest.raises(NoWalletError):
        provider.require()


def test_keyring_wallet_works_without_migrating_anything(tmp_path: Path) -> None:
    provider, book, _ = _build(tmp_path, keyring_key=CLAVE)

    wallets = provider.wallets()
    assert len(wallets) == 1
    assert wallets[0].wallet_id == LEGACY_WALLET_ID
    assert wallets[0].source is WalletSource.KEYRING
    assert wallets[0].label == "1"
    assert provider.address() == address_from_key(CLAVE)
    assert provider.available() is True
    assert provider.require() == CLAVE
    # Nada escrito en disco: el llavero manda mientras no se toque el libro.
    assert book.seeded is False
    assert not book.path.exists()


def test_keyring_wallet_tracks_a_key_changed_in_credentials(tmp_path: Path) -> None:
    provider, _, store = _build(tmp_path, keyring_key=CLAVE)
    store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_2)
    assert provider.address() == address_from_key(CLAVE_2)


def test_removing_the_keyring_wallet_does_not_come_back(tmp_path: Path) -> None:
    provider, book, store = _build(tmp_path, keyring_key=CLAVE)

    assert provider.remove(LEGACY_WALLET_ID) is True
    assert provider.wallets() == ()
    assert provider.address() is None
    assert provider.available() is False
    with pytest.raises(NoWalletError):
        provider.require()
    # La copia del llavero no se toca: es el respaldo.
    assert store.get(app_secret_key(PRIVATE_KEY_SECRET)) == CLAVE

    # Y en el arranque siguiente tampoco: el borrado se recuerda.
    reopened = WalletKeyProvider(
        WalletBook(book.path),
        legacy=SpendingKeyProvider(store, allow_env_fallback=False),
    )
    assert reopened.wallets() == ()
    assert reopened.address() is None


# --------------------------------------------------------------------------- #
# El proveedor: modo observación
# --------------------------------------------------------------------------- #
def test_watch_wallet_reads_but_does_not_sign(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path, keyring_key=CLAVE)
    wallet = provider.add_watch(OTRA_DIRECCION, "Mirón")

    assert wallet.source is WalletSource.WATCH
    # La heredada se materializa al escribir, para no perderla.
    assert [w.label for w in provider.wallets()] == ["1", "Mirón"]
    active = provider.active()
    assert active is not None
    assert active.wallet_id == wallet.wallet_id
    assert provider.address() == OTRA_DIRECCION
    assert provider.available() is False
    assert provider.get() is None
    with pytest.raises(NoWalletError) as caught:
        provider.require()
    assert "observación" in str(caught.value)


def test_watch_wallet_accepts_a_solana_address(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    wallet = provider.add_watch(DIRECCION_SOLANA, "Sol")
    assert wallet.kind is WalletKind.SOLANA


def test_watch_wallet_refuses_something_that_is_not_an_address(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    with pytest.raises(InvalidAmountError):
        provider.add_watch("hola", "Cosa")


def test_watch_wallet_refuses_a_duplicate(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    provider.add_watch(OTRA_DIRECCION, "1")
    with pytest.raises(WalletExistsError):
        provider.add_watch(OTRA_DIRECCION, "2")


def test_active_wallet_can_go_back_to_the_keyring_one(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path, keyring_key=CLAVE)
    watch = provider.add_watch(OTRA_DIRECCION, "Mirón")

    # La activa es la de observación: se lee su dirección y no se firma.
    assert provider.address() == OTRA_DIRECCION
    with pytest.raises(NoWalletError):
        provider.require()

    legacy = provider.wallets()[0]
    provider.set_active(legacy.wallet_id)
    assert provider.require() == CLAVE
    active = provider.active()
    assert active is not None
    assert active.wallet_id != watch.wallet_id


# --------------------------------------------------------------------------- #
# El proveedor: cartera cifrada
# --------------------------------------------------------------------------- #
def test_signing_wallet_is_encrypted_and_signs_when_unlocked(tmp_path: Path) -> None:
    provider, book, _ = _build(tmp_path)
    wallet = provider.add_signing(CLAVE, "1", CONTRASENA)

    assert wallet.source is WalletSource.KEYSTORE
    assert provider.has_password() is True
    assert provider.is_unlocked() is True
    assert provider.available() is True
    assert provider.require() == CLAVE
    assert provider.address() == address_from_key(CLAVE)

    # La clave no está en claro en el fichero del libro.
    text = book.path.read_text(encoding="utf-8").lower()
    assert CLAVE.lower() not in text
    assert CLAVE[2:].lower() not in text


def test_locked_vault_refuses_to_sign_until_unlocked(tmp_path: Path) -> None:
    provider, book, _ = _build(tmp_path)
    provider.add_signing(CLAVE, "1", CONTRASENA)

    reopened = WalletKeyProvider(
        WalletBook(book.path),
        legacy=SpendingKeyProvider(InMemorySecretStore(), allow_env_fallback=False),
    )
    assert reopened.is_locked() is True
    assert reopened.available() is False
    # La dirección se enseña sin descifrar nada.
    assert reopened.address() == address_from_key(CLAVE)
    with pytest.raises(WalletLockedError) as caught:
        reopened.require()
    assert "Cartera" in str(caught.value)

    assert reopened.unlock("equivocada") is False
    with pytest.raises(WalletLockedError):
        reopened.require()
    assert reopened.unlock(CONTRASENA) is True
    assert reopened.require() == CLAVE

    reopened.lock()
    assert reopened.is_unlocked() is False
    with pytest.raises(WalletLockedError):
        reopened.get()


def test_unlock_without_any_vault_is_false(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path, keyring_key=CLAVE)
    assert provider.has_password() is False
    assert provider.unlock(CONTRASENA) is False


def test_adding_a_second_key_requires_the_same_password(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    provider.add_signing(CLAVE, "1", CONTRASENA)
    with pytest.raises(WrongPasswordError):
        provider.add_signing(CLAVE_2, "2", "otra-cosa")
    provider.add_signing(CLAVE_2, "2", CONTRASENA)
    assert [w.label for w in provider.wallets()] == ["1", "2"]


def test_adding_a_key_upgrades_a_watch_wallet_instead_of_duplicating(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    watched = provider.add_watch(address_from_key(CLAVE), "1")
    upgraded = provider.add_signing(CLAVE, "1", CONTRASENA)

    assert upgraded.wallet_id == watched.wallet_id
    assert upgraded.source is WalletSource.KEYSTORE
    assert len(provider.wallets()) == 1
    assert provider.require() == CLAVE


def test_adding_a_duplicate_key_says_so(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    provider.add_signing(CLAVE, "1", CONTRASENA)
    with pytest.raises(WalletExistsError):
        provider.add_signing(CLAVE, "2", CONTRASENA)


def test_a_bad_private_key_is_rejected_without_echoing_it(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    with pytest.raises(KeyCustodyError) as caught:
        provider.add_signing("no-es-una-clave", "1", CONTRASENA)
    assert "no-es-una-clave" not in str(caught.value)


def test_adding_a_key_materializes_the_keyring_wallet_first(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path, keyring_key=CLAVE)
    provider.add_signing(CLAVE_2, "2", CONTRASENA)

    wallets = provider.wallets()
    assert [w.label for w in wallets] == ["1", "2"]
    assert wallets[0].source is WalletSource.KEYRING
    assert wallets[1].source is WalletSource.KEYSTORE
    assert provider.require() == CLAVE_2


def test_next_label_skips_the_taken_numbers(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    assert provider.next_label() == "1"
    provider.add_signing(CLAVE, "1", CONTRASENA)
    assert provider.next_label() == "2"
    provider.rename(provider.wallets()[0].wallet_id, "Ahorros")
    assert provider.next_label() == "1"


# --------------------------------------------------------------------------- #
# El proveedor: enseñar la clave privada
# --------------------------------------------------------------------------- #
def test_reveal_always_asks_for_the_password(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    provider.add_signing(CLAVE, "1", CONTRASENA)

    with pytest.raises(WrongPasswordError):
        provider.reveal("equivocada")
    assert provider.reveal(CONTRASENA) == CLAVE


def test_reveal_of_a_watch_wallet_is_refused(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    provider.add_watch(OTRA_DIRECCION, "1")
    with pytest.raises(NoWalletError):
        provider.reveal(CONTRASENA)


def test_reveal_migrates_the_keyring_key_to_the_encrypted_book(tmp_path: Path) -> None:
    provider, book, store = _build(tmp_path, keyring_key=CLAVE)

    assert provider.reveal(CONTRASENA) == CLAVE

    migrated = provider.wallets()[0]
    assert migrated.source is WalletSource.KEYSTORE
    assert migrated.address == address_from_key(CLAVE)
    assert migrated.keystore is not None
    assert migrated.keystore.get("crypto") is not None
    # La copia del llavero sigue donde estaba: es el respaldo.
    assert store.get(app_secret_key(PRIVATE_KEY_SECRET)) == CLAVE

    # Y al arrancar de nuevo la clave se descifra del libro.
    reopened = WalletKeyProvider(
        WalletBook(book.path),
        legacy=SpendingKeyProvider(store, allow_env_fallback=False),
    )
    assert reopened.is_locked() is True
    assert reopened.unlock(CONTRASENA) is True
    assert reopened.require() == CLAVE
    assert reopened.get() == CLAVE


def test_reveal_of_the_keyring_wallet_requires_the_vault_password(
    tmp_path: Path,
) -> None:
    """Con un almacén ya creado, la migración no puede traer una segunda contraseña.

    Todas las claves cifradas comparten una única contraseña, y es la que la
    sesión usa para desbloquear: migrar la del llavero con otra dejaría una
    clave que el desbloqueo no abriría, y el fallo se descubriría al firmar.
    """
    provider, _, _ = _build(tmp_path, keyring_key=CLAVE)
    provider.add_signing(CLAVE_2, "2", CONTRASENA)  # el almacén nace con esta
    heredada = next(w for w in provider.wallets() if w.source is WalletSource.KEYRING)
    provider.set_active(heredada.wallet_id)

    with pytest.raises(WrongPasswordError):
        provider.reveal("otra-cosa")
    intacta = provider.get_wallet(heredada.wallet_id)
    assert intacta is not None
    assert intacta.source is WalletSource.KEYRING

    assert provider.reveal(CONTRASENA) == CLAVE
    migrated = provider.get_wallet(heredada.wallet_id)
    assert migrated is not None
    assert migrated.source is WalletSource.KEYSTORE


# --------------------------------------------------------------------------- #
# Interfaz compartida con el proveedor heredado
# --------------------------------------------------------------------------- #
def test_allow_env_key_still_applies_to_the_keyring_wallet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGOCOMPORA_EXECUTION_PRIVATE_KEY", CLAVE)
    provider, _, _ = _build(tmp_path, allow_env=True)

    assert provider.available() is True
    assert provider.require() == CLAVE
    assert provider.env_var == "AMIGOCOMPORA_EXECUTION_PRIVATE_KEY"


def test_password_is_forgotten_when_the_last_vault_wallet_goes(tmp_path: Path) -> None:
    provider, _, _ = _build(tmp_path)
    provider.add_signing(CLAVE, "1", CONTRASENA)
    assert provider.is_unlocked() is True

    provider.remove(provider.wallets()[0].wallet_id)
    assert provider.has_password() is False
    assert provider.is_unlocked() is False
