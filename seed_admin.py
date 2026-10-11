#!/usr/bin/env python3
"""
Seed Script for Assignment Submission Tracker
Generates a secure password hash for the Master Admin and prints .env configuration.
"""

import sys
import getpass
from werkzeug.security import generate_password_hash

def main():
    print("==================================================")
    print(" Assignment Submission Tracker - Admin Credential Seed ")
    print("==================================================")

    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = input("Enter master admin password to hash: ").strip()
        if not password:
            print("Error: Password cannot be blank.")
            sys.exit(1)

    hashed = generate_password_hash(password)
    print("\nAdmin Password Hash generated successfully:")
    print("--------------------------------------------------")
    print(hashed)
    print("--------------------------------------------------\n")
    print("Add this to your .env file or Render Environment Variables:")
    print(f"ADMIN_PASSWORD_HASH='{hashed}'\n")

if __name__ == "__main__":
    main()
