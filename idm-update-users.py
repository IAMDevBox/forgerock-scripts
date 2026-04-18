#!/usr/bin/env python3
"""
Update a specific attribute for a list of IDM users.

Usage:
    1. Edit the configuration section below
    2. Backup:  python3 idm-update-users.py backup
    3. Update:  python3 idm-update-users.py update
    4. Restore: python3 idm-update-users.py restore backup_status_20260403_1200.json
"""

import argparse
import glob
import json
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    print("Error: 'requests' library required. Install with: pip install requests")
    sys.exit(1)

###############################################################################
# Configuration — edit these values
###############################################################################

# IDM base URL
IDM_URL = "https://idm.example.com"

# Bearer token for authentication
TOKEN = "eyJhbGciOiJIUzI1NiJ9..."

# Or read token from file (set TOKEN = "" to use this instead)
TOKEN_FILE = ""  # e.g., "/opt/app/token.txt"

# User IDs file (one user ID per line, # comments and blank lines ignored)
USER_FILE = "users.txt"

# Attribute to update and its new value
ATTRIBUTE = "status"        # e.g., "status", "/preferences/updates", "accountStatus"
VALUE = "active"            # new value for all users

# Options
VERIFY_SSL = True           # False to skip SSL verification (for self-signed certs)
DELAY = 0.1                 # seconds between requests (avoid rate limiting)
MAX_UPDATES = 0             # 0 = update all, >0 = stop after N successful updates
STOP_ON_FAILURE = True      # True = stop immediately on first failure

###############################################################################
# Implementation — no need to edit below
###############################################################################

def load_user_ids(filepath):
    """Load user IDs from a text file.

    Skip: blank lines, # comments, and lines with UPDATED marker.
    Returns list of (line_number, user_id) tuples.
    """
    ids = []
    with open(filepath, "r") as f:
        for line_num, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Skip already updated users (marked with UPDATED)
            if "# UPDATED" in stripped:
                continue
            # User ID is the first token (before any comment)
            user_id = stripped.split("#")[0].strip().split()[0]
            if user_id:
                ids.append((line_num, user_id))
    return ids


def mark_updated(filepath, line_num, user_id):
    """Mark a user as updated in the user file by appending timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(filepath, "r") as f:
        lines = f.readlines()
    # Replace the line with user_id + UPDATED marker
    if line_num <= len(lines):
        original = lines[line_num - 1].rstrip()
        lines[line_num - 1] = "{} # UPDATED {}\n".format(original, ts)
    with open(filepath, "w") as f:
        f.writelines(lines)


def get_token():
    """Get Bearer token from TOKEN variable or TOKEN_FILE."""
    if TOKEN:
        return TOKEN
    if TOKEN_FILE:
        with open(TOKEN_FILE, "r") as f:
            return f.read().strip()
    return None


def get_user_attribute(base_url, user_id, attribute, headers):
    """Fetch the current value of an attribute for a user."""
    field = attribute.lstrip("/")
    url = "{}/openidm/managed/user/{}?_fields={}".format(
        base_url.rstrip("/"), user_id, field)
    resp = requests.get(url, headers=headers, verify=VERIFY_SSL, timeout=30)
    if resp.status_code != 200:
        return None, resp.status_code, resp.text
    data = resp.json()
    # Navigate nested fields like "preferences/updates"
    val = data
    for part in field.split("/"):
        if isinstance(val, dict):
            val = val.get(part)
        else:
            val = None
            break
    return val, 200, ""


def save_backup(backup, filepath):
    """Write backup data to JSON file."""
    with open(filepath, "w") as f:
        json.dump(backup, f, indent=2)


def update_user(base_url, user_id, attribute, value, headers):
    """Update a single user attribute via IDM PATCH API (JSON Patch / RFC 6902)."""
    url = "{}/openidm/managed/user/{}".format(base_url.rstrip("/"), user_id)
    field = attribute if attribute.startswith("/") else "/" + attribute

    patch_body = [
        {
            "operation": "replace",
            "field": field,
            "value": value,
        }
    ]

    resp = requests.patch(
        url,
        headers=headers,
        json=patch_body,
        verify=VERIFY_SSL,
        timeout=30,
    )

    return resp.status_code, resp.text


def require_token():
    token = get_token()
    if not token:
        print("Error: TOKEN or TOKEN_FILE must be set")
        sys.exit(1)
    return token


def require_users():
    try:
        entries = load_user_ids(USER_FILE)
    except FileNotFoundError:
        print("Error: file not found: {}".format(USER_FILE))
        sys.exit(1)
    if not entries:
        print("Error: no pending user IDs found in {} (all may be already UPDATED)".format(USER_FILE))
        sys.exit(1)
    return entries


def make_headers(token):
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def find_latest_backup():
    """Find the most recent backup_*.json file in current directory."""
    pattern = "backup_{}_*.json".format(ATTRIBUTE.replace("/", "_"))
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# backup: fetch original values and save to file
# ---------------------------------------------------------------------------
def cmd_backup(args):
    token = require_token()
    user_entries = require_users()
    headers = make_headers(token)

    print("=" * 50)
    print("  IDM Backup — fetch original values")
    print("=" * 50)
    print("  URL:        {}".format(IDM_URL))
    print("  Attribute:  {}".format(ATTRIBUTE))
    print("  Users:      {}".format(len(user_entries)))
    print("=" * 50)
    print("")

    backup_entries = []
    fetch_failed = 0
    for i, (line_num, user_id) in enumerate(user_entries, 1):
        try:
            val, status, _ = get_user_attribute(IDM_URL, user_id, ATTRIBUTE, headers)
            if status == 200:
                backup_entries.append({
                    "user_id": user_id,
                    "original_value": val,
                })
                print("[{}/{}] {} — {} = {}".format(
                    i, len(user_entries), user_id, ATTRIBUTE, val))
            else:
                print("[{}/{}] {} — FAILED to read (HTTP {})".format(
                    i, len(user_entries), user_id, status))
                fetch_failed += 1
                if STOP_ON_FAILURE:
                    print("\n[FATAL] Cannot read original value. Aborting.")
                    sys.exit(1)
        except requests.RequestException as e:
            print("[{}/{}] {} — ERROR: {}".format(
                i, len(user_entries), user_id, str(e)))
            fetch_failed += 1
            if STOP_ON_FAILURE:
                print("\n[FATAL] Cannot read original value. Aborting.")
                sys.exit(1)
        if DELAY and i < len(user_entries):
            time.sleep(DELAY)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = "backup_{}_{}.json".format(ATTRIBUTE.replace("/", "_"), ts)
    backup_data = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "idm_url": IDM_URL,
        "attribute": ATTRIBUTE,
        "new_value": VALUE,
        "users": backup_entries,
    }
    save_backup(backup_data, backup_file)

    print("")
    print("=" * 50)
    print("  Backup saved: {}".format(backup_file))
    print("  Users: {} saved, {} failed".format(len(backup_entries), fetch_failed))
    print("")
    print("  Next step:")
    print("    python3 idm-update-users.py update --backup {}".format(backup_file))
    print("=" * 50)


# ---------------------------------------------------------------------------
# update: apply changes (requires --backup file)
# ---------------------------------------------------------------------------
def cmd_update(args):
    token = require_token()
    user_entries = require_users()
    headers = make_headers(token)

    backup_file = args.backup or find_latest_backup()
    if not backup_file:
        print("Error: No backup file found. Run 'backup' first.")
        sys.exit(1)

    # Verify backup data is complete and consistent
    try:
        with open(backup_file, "r") as f:
            backup_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print("Error reading backup file: {}".format(e))
        sys.exit(1)

    errors = []
    # Required top-level fields
    for key in ("attribute", "idm_url", "users", "timestamp"):
        if key not in backup_data:
            errors.append("Missing field: '{}'".format(key))
    if errors:
        for e in errors:
            print("  " + e)
        sys.exit(1)

    # Attribute must match current config
    if backup_data["attribute"] != ATTRIBUTE:
        errors.append("Attribute mismatch: backup='{}', config='{}'".format(
            backup_data["attribute"], ATTRIBUTE))

    # IDM URL must match
    if backup_data["idm_url"] != IDM_URL:
        errors.append("IDM URL mismatch: backup='{}', config='{}'".format(
            backup_data["idm_url"], IDM_URL))

    # Each user entry must have user_id and original_value
    backup_users = backup_data.get("users", [])
    for i, entry in enumerate(backup_users):
        if "user_id" not in entry:
            errors.append("users[{}]: missing 'user_id'".format(i))
        if "original_value" not in entry:
            errors.append("users[{}] ({}): missing 'original_value'".format(
                i, entry.get("user_id", "?")))

    # All pending users must be in backup
    backed_up_ids = {e["user_id"] for e in backup_users if "user_id" in e}
    pending_ids = {uid for _, uid in user_entries}
    missing = pending_ids - backed_up_ids
    if missing:
        errors.append("{} users not in backup: {}".format(
            len(missing), ", ".join(sorted(missing)[:5])))

    if errors:
        print("Error: backup file '{}' failed validation:".format(backup_file))
        for e in errors:
            print("  - " + e)
        print("\nRun 'backup' again to create a valid backup.")
        sys.exit(1)

    print("=" * 50)
    print("  IDM User Attribute Update")
    print("=" * 50)
    print("  URL:        {}".format(IDM_URL))
    print("  Attribute:  {}".format(ATTRIBUTE))
    print("  Value:      {}".format(VALUE))
    print("  Users:      {}".format(len(user_entries)))
    print("  SSL verify: {}".format(VERIFY_SSL))
    print("  Max updates:{}".format(MAX_UPDATES if MAX_UPDATES > 0 else "all"))
    print("  Stop on fail: {}".format(STOP_ON_FAILURE))
    print("  Backup:     {}".format(backup_file))
    print("  Log:        {} (in-place)".format(USER_FILE))
    print("=" * 50)
    print("")

    success = 0
    failed = 0
    errors = []
    stopped = False

    total = len(user_entries)
    for i, (line_num, user_id) in enumerate(user_entries, 1):
        if MAX_UPDATES > 0 and success >= MAX_UPDATES:
            print("\n[INFO] Reached MAX_UPDATES limit ({})".format(MAX_UPDATES))
            stopped = True
            break

        try:
            status, body = update_user(IDM_URL, user_id, ATTRIBUTE, VALUE, headers)
            if 200 <= status < 300:
                print("[{}/{}] OK: {} (HTTP {})".format(i, total, user_id, status))
                success += 1
                mark_updated(USER_FILE, line_num, user_id)
            else:
                print("[{}/{}] FAIL: {} (HTTP {}) — {}".format(
                    i, total, user_id, status, body[:200]))
                failed += 1
                errors.append({"user": user_id, "status": status, "body": body[:200]})
                if STOP_ON_FAILURE:
                    print("\n[FATAL] Stopping due to failure (STOP_ON_FAILURE=True)")
                    stopped = True
                    break
        except requests.RequestException as e:
            print("[{}/{}] ERROR: {} — {}".format(i, total, user_id, str(e)))
            failed += 1
            errors.append({"user": user_id, "status": 0, "body": str(e)})
            if STOP_ON_FAILURE:
                print("\n[FATAL] Stopping due to error (STOP_ON_FAILURE=True)")
                stopped = True
                break

        if DELAY and i < total:
            time.sleep(DELAY)

    # Summary
    print("")
    print("=" * 50)
    remaining = total - success - failed
    print("  Done: {} success, {} failed, {} remaining".format(success, failed, remaining))
    if stopped:
        print("  Status: STOPPED")
    else:
        print("  Status: COMPLETED")
    print("  Backup: {} (use 'restore' to rollback)".format(backup_file))
    print("  Log: {} (in-place)".format(USER_FILE))
    print("=" * 50)
    if errors:
        print("")
        print("Failed users:")
        for e in errors:
            print("  {} — HTTP {} — {}".format(e["user"], e["status"], e["body"]))


# ---------------------------------------------------------------------------
# restore: rollback from backup file
# ---------------------------------------------------------------------------
def cmd_restore(args):
    token = require_token()
    headers = make_headers(token)

    try:
        with open(args.file, "r") as f:
            backup = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print("Error reading backup file: {}".format(e))
        sys.exit(1)

    attribute = backup["attribute"]
    entries = backup["users"]
    base_url = backup.get("idm_url", IDM_URL)

    print("=" * 50)
    print("  IDM User Attribute RESTORE")
    print("=" * 50)
    print("  Backup:     {}".format(args.file))
    print("  URL:        {}".format(base_url))
    print("  Attribute:  {}".format(attribute))
    print("  Users:      {}".format(len(entries)))
    print("  Backup time:{}".format(backup.get("timestamp", "?")))
    print("=" * 50)
    print("")

    success = 0
    failed = 0
    total = len(entries)
    for i, entry in enumerate(entries, 1):
        user_id = entry["user_id"]
        original = entry["original_value"]
        try:
            status, body = update_user(base_url, user_id, attribute, original, headers)
            if 200 <= status < 300:
                print("[{}/{}] RESTORED: {} — {} = {}".format(
                    i, total, user_id, attribute, original))
                success += 1
            else:
                print("[{}/{}] FAIL: {} (HTTP {}) — {}".format(
                    i, total, user_id, status, body[:200]))
                failed += 1
        except requests.RequestException as e:
            print("[{}/{}] ERROR: {} — {}".format(i, total, user_id, str(e)))
            failed += 1
        if DELAY and i < total:
            time.sleep(DELAY)

    print("")
    print("=" * 50)
    print("  Restore done: {} success, {} failed".format(success, failed))
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(
        description="IDM User Attribute Update",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Workflow:
  1. backup                          Fetch & save original values
  2. update [--backup <file>]        Apply changes (auto-detects latest backup)
  3. restore <file>                  Rollback to original values""")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("backup", help="Fetch original values and save to backup file")

    p_update = sub.add_parser("update", help="Apply attribute changes to users")
    p_update.add_argument("--backup", metavar="FILE",
                          help="Backup file (default: auto-detect latest)")

    p_restore = sub.add_parser("restore", help="Restore original values from backup")
    p_restore.add_argument("file", help="Backup JSON file to restore from")

    args = parser.parse_args()

    if args.command == "backup":
        cmd_backup(args)
    elif args.command == "update":
        cmd_update(args)
    elif args.command == "restore":
        cmd_restore(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
