"""
Credential encryption service for Reddit Scraper API.
"""

import os
import base64
from cryptography.fernet import Fernet

# Import config
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config import SECURITY_CONFIG


def get_encryption_key():
    """Get or generate encryption key for credentials."""
    key_file = SECURITY_CONFIG["encryption_key_file"]
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(key_file, "wb") as f:
            f.write(key)
        return key


# Initialize encryption
ENCRYPTION_KEY = get_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)


def encrypt_credential(value: str) -> str:
    """Encrypt a credential value."""
    return base64.b64encode(cipher_suite.encrypt(value.encode())).decode()


def decrypt_credential(encrypted_value: str) -> str:
    """Decrypt a credential value."""
    return cipher_suite.decrypt(base64.b64decode(encrypted_value.encode())).decode()
