from cryptography.fernet import Fernet
from .config import FERNET_KEY

# fail at import, not on the first exchange, if the key is missing
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY is not set in .env (see .env.example)")

_fernet = Fernet(FERNET_KEY.encode())

# encrypt a secret (e.g. a plaid access_token) for storage; output starts with "gAAAAA"
def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()

# raises cryptography.fernet.InvalidToken if the ciphertext was tampered with or the key changed
def decrypt(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
