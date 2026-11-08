import base64
import getpass
import os
import sys
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

def unlock(password: str = None):
    vault_path = os.path.join(os.path.dirname(__file__), "vault.enc")
    if not os.path.exists(vault_path):
        print("[-] 'vault.enc' fayli topilmadi!")
        return

    if not password:
        if len(sys.argv) > 1:
            password = sys.argv[1]
        else:
            try:
                if sys.stdin.isatty():
                    password = getpass.getpass("Parolni kiriting: ")
                else:
                    password = sys.stdin.readline().strip()
            except Exception:
                password = input("Parolni kiriting: ")

    if not password:
        print("[-] Parol kiritilmadi!")
        return

    try:
        with open(vault_path, "rb") as f:
            content = f.read()

        salt = content[:16]
        token = content[16:]

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=200000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        fernet = Fernet(key)
        decrypted_data = fernet.decrypt(token).decode("utf-8")

        print("\n[+] Parol to'g'ri! Saqlangan ma'lumotlar:\n" + "=" * 40)
        print(decrypted_data)
        print("=" * 40 + "\n")
    except InvalidToken:
        print("\n[-] Noto'g'ri parol! Ma'lumotlarni ochib bo'lmadi.\n")
    except Exception as e:
        print(f"\n[-] Xatolik yuz berdi: {e}\n")

if __name__ == "__main__":
    unlock()
