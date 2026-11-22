"""Set or rotate the administrator password (stored only as a salted scrypt hash).

  .venv/bin/python scripts/admin_password.py            # new random password, written to data/admin_credentials.txt
  .venv/bin/python scripts/admin_password.py --username admin --password '...'
"""
import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import auth  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--username", default="admin")
ap.add_argument("--password")
a = ap.parse_args()
pw = a.password or secrets.token_urlsafe(14)
if len(pw) < 10:
    sys.exit("Use a password of at least 10 characters.")
auth.set_admin(a.username, pw)
if not a.password:
    with open(auth.CRED_FILE, "w") as f:
        f.write(f"Payables desk administrator (local only; keep private)\nusername: {a.username}\npassword: {pw}\n")
    os.chmod(auth.CRED_FILE, 0o600)
    print(f"New password written to {os.path.abspath(auth.CRED_FILE)}")
else:
    print("Administrator password updated.")
