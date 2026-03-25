"""
Credential storage — plain JSON (no encryption).
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from src.utils import CONFIG_DIR, logger

_ENC_PATH = CONFIG_DIR / "accounts.json.enc"
_PLAIN_PATH = CONFIG_DIR / "accounts.json"


def _derive_key(password: str) -> bytes:
    """Legacy — kept for reading old encrypted files."""
    import base64, hashlib
    key_hash = hashlib.sha256(password.encode()).digest()
    return base64.urlsafe_b64encode(key_hash)


def encrypt_accounts(accounts: list[dict], password: str = "") -> bytes:
    """Legacy compat — just returns JSON bytes."""
    return json.dumps(accounts, ensure_ascii=False).encode("utf-8")


def decrypt_accounts(encrypted_data: bytes, password: str = "") -> list[dict]:
    """Decrypt accounts — tries plain JSON first, then Fernet for old files."""
    try:
        return json.loads(encrypted_data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    # Fallback: old Fernet-encrypted file
    try:
        from cryptography.fernet import Fernet
        key = _derive_key(password)
        fernet = Fernet(key)
        data = fernet.decrypt(encrypted_data)
        return json.loads(data.decode("utf-8"))
    except Exception:
        raise ValueError("Could not read accounts file")


def save_encrypted_accounts(accounts: list[dict], password: str = "") -> None:
    """Save accounts as plain JSON."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_ENC_PATH, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    logger.info(f"Accounts saved to {_ENC_PATH}")


def load_encrypted_accounts(password: str = "") -> list[dict]:
    """Load accounts from file."""
    if not _ENC_PATH.exists():
        raise FileNotFoundError(f"Accounts file not found: {_ENC_PATH}")
    with open(_ENC_PATH, "rb") as f:
        data = f.read()
    return decrypt_accounts(data, password)


def load_plaintext_accounts() -> Optional[list[dict]]:
    """Load accounts from unencrypted accounts.json (if exists)."""
    if not _PLAIN_PATH.exists():
        return None
    with open(_PLAIN_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def migrate_to_encrypted(password: str = "") -> bool:
    """Migrate plaintext accounts.json to .enc format."""
    accounts = load_plaintext_accounts()
    if accounts is None:
        return False
    save_encrypted_accounts(accounts, password)
    backup_path = CONFIG_DIR / "accounts.json.bak"
    _PLAIN_PATH.rename(backup_path)
    logger.info(f"Old file backed up to {backup_path}")
    return True


def hash_password(password: str) -> str:
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash


def prompt_master_password(verify_hash: str = "") -> str:
    """No-op — always returns empty string."""
    return ""
