"""
Credential encryption/decryption using Fernet symmetric encryption.
"""

import json
import base64
import hashlib
import getpass
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from src.utils import CONFIG_DIR, logger


def _derive_key(password: str) -> bytes:
    """Derive a Fernet key from a password using SHA-256."""
    key_hash = hashlib.sha256(password.encode()).digest()
    return base64.urlsafe_b64encode(key_hash)


def encrypt_accounts(accounts: list[dict], password: str) -> bytes:
    """Encrypt accounts list with the given password."""
    key = _derive_key(password)
    fernet = Fernet(key)
    data = json.dumps(accounts, ensure_ascii=False).encode("utf-8")
    return fernet.encrypt(data)


def decrypt_accounts(encrypted_data: bytes, password: str) -> list[dict]:
    """Decrypt accounts with the given password."""
    key = _derive_key(password)
    fernet = Fernet(key)
    try:
        data = fernet.decrypt(encrypted_data)
        return json.loads(data.decode("utf-8"))
    except InvalidToken:
        raise ValueError("Incorrect password or corrupted data")


def save_encrypted_accounts(accounts: list[dict], password: str) -> None:
    """Encrypt and save accounts to config/accounts.json.enc."""
    enc_path = CONFIG_DIR / "accounts.json.enc"
    encrypted = encrypt_accounts(accounts, password)
    with open(enc_path, "wb") as f:
        f.write(encrypted)
    logger.info(f"Encrypted accounts saved to {enc_path}")


def load_encrypted_accounts(password: str) -> list[dict]:
    """Load and decrypt accounts from config/accounts.json.enc."""
    enc_path = CONFIG_DIR / "accounts.json.enc"
    if not enc_path.exists():
        raise FileNotFoundError(
            f"Encrypted accounts file not found: {enc_path}\n"
            "Run 'Setup Accounts' from the main menu first."
        )
    with open(enc_path, "rb") as f:
        encrypted = f.read()
    return decrypt_accounts(encrypted, password)


def load_plaintext_accounts() -> Optional[list[dict]]:
    """Load accounts from unencrypted accounts.json (if exists)."""
    plain_path = CONFIG_DIR / "accounts.json"
    if not plain_path.exists():
        return None
    with open(plain_path, "r", encoding="utf-8") as f:
        return json.load(f)


def migrate_to_encrypted(password: str) -> bool:
    """Migrate plaintext accounts.json to encrypted .enc format."""
    accounts = load_plaintext_accounts()
    if accounts is None:
        logger.warning("No plaintext accounts.json found to migrate")
        return False

    save_encrypted_accounts(accounts, password)

    # Rename old file
    plain_path = CONFIG_DIR / "accounts.json"
    backup_path = CONFIG_DIR / "accounts.json.bak"
    plain_path.rename(backup_path)
    logger.info(f"Old plaintext file backed up to {backup_path}")
    return True


def hash_password(password: str) -> str:
    """Hash password for verification (stored in settings)."""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against stored hash."""
    return hash_password(password) == password_hash


def prompt_master_password(verify_hash: str = "") -> str:
    """Prompt user for master password. Verify against hash if provided."""
    password = getpass.getpass("🔐 Enter master password: ")
    if verify_hash and not verify_password(password, verify_hash):
        raise ValueError("Incorrect master password")
    return password
