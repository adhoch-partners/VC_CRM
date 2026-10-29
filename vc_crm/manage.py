"""Command-line user management, for use from a server shell.

    python -m vc_crm.manage create-user --email a@firm.com --name "A Partner" --role admin
    python -m vc_crm.manage reset-password --email a@firm.com
    python -m vc_crm.manage list-users

Passwords are prompted for, never passed as arguments (they would land in shell history).
Uses DATABASE_URL (Postgres) or VC_CRM_DB (SQLite path), like the app.
"""

import argparse
import getpass
import sys

from . import auth
from .db import Database


def prompt_password():
    pw = getpass.getpass("Password: ")
    if problem := auth.check_password_policy(pw):
        sys.exit(problem)
    if getpass.getpass("Repeat password: ") != pw:
        sys.exit("Passwords do not match.")
    return pw


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m vc_crm.manage")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("--email", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--role", choices=auth.ROLES, default="viewer")
    r = sub.add_parser("reset-password")
    r.add_argument("--email", required=True)
    sub.add_parser("list-users")
    args = p.parse_args(argv)

    db = Database()
    db.init()
    conn = db.connect()
    try:
        if args.cmd == "list-users":
            for u in conn.execute("SELECT email, name, role, active FROM users ORDER BY email"):
                print(f"{u['email']:<35} {u['name']:<25} {u['role']:<7} "
                      f"{'active' if u['active'] else 'deactivated'}")
            return
        email = args.email.strip().lower()
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if args.cmd == "create-user":
            if existing:
                sys.exit(f"{email} already exists.")
            conn.insert("users", {"email": email, "name": args.name, "role": args.role,
                                  "password_hash": auth.hash_password(prompt_password())})
            print(f"Created {args.role} {email}.")
        else:
            if not existing:
                sys.exit(f"No user {email}.")
            with conn.transaction():
                conn.execute("UPDATE users SET password_hash = ?, active = 1 WHERE id = ?",
                             (auth.hash_password(prompt_password()), existing["id"]))
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (existing["id"],))
            print(f"Password reset for {email}; their existing sessions were signed out.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
