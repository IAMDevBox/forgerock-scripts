#!/usr/bin/env python3
"""
Check & fix a date field inside an array attribute on IDM managed users.

Use case:
    Attribute is an array of objects, each with a date field.
    Example: <ATTRIBUTE> = [{<DATE_FIELD>: "08-08-2025"}, ...]
    This script finds users whose date values don't match mm-dd-yyyy
    (format + real calendar date), then lets you batch-fix them.

Workflow:
    1. python3 idm-check-dates.py check
       → check_<attr>_<ts>.json  (full audit log)
       → fixes_<attr>_<ts>.json  (editable template, INVALID users only)
    2. [manually edit fixes_*.json: fill `corrected_value` for each user]
    3. python3 idm-check-dates.py fix fixes_<attr>_<ts>.json
       → fix_rollback_<attr>_<ts>.json  (rollback data, in case)
    4. If needed:
       python3 idm-check-dates.py restore fix_rollback_<attr>_<ts>.json

Rules (hardcoded in check_dates()):
    - Attribute missing/empty          → OK (nothing to check)
    - Entry without DATE_FIELD         → skipped
    - ANY date value fails             → user is INVALID (strict)
    - Date must match mm-dd-yyyy AND be a real calendar date
"""

import argparse
import copy
import json
import re
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

IDM_URL = "https://idm.example.com"
TOKEN = "PLACEHOLDER_TOKEN"
TOKEN_FILE = ""                      # e.g., "/opt/app/token.txt"

USER_FILE = "users4date.txt"         # one user ID per line, # comments ignored
MANAGED_OBJECT = "alpha_user"        # IDM managed object endpoint
ATTRIBUTE = "REPLACE_ME"             # array attribute name on the user
DATE_FIELD = "REPLACE_ME"            # date field inside each array entry

VERIFY_SSL = True
DELAY = 0.1                          # seconds between requests

###############################################################################
# Validation
###############################################################################

MM_DD_YYYY_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")


def check_dates(attr_value):
    """Return (status, detail) per the rules in the module docstring."""
    if not attr_value:
        return ("OK", "no {} attribute — nothing to check".format(ATTRIBUTE))

    bad = []
    checked = 0
    for idx, entry in enumerate(attr_value):
        if not isinstance(entry, dict) or DATE_FIELD not in entry:
            continue
        checked += 1
        value = entry.get(DATE_FIELD)
        if not isinstance(value, str) or not MM_DD_YYYY_RE.match(value):
            bad.append("[{}].{}={!r}".format(idx, DATE_FIELD, value))
            continue
        try:
            datetime.strptime(value, "%m-%d-%Y")
        except ValueError:
            bad.append("[{}].{}={!r} (not a real date)".format(idx, DATE_FIELD, value))

    if bad:
        return ("INVALID", "bad {}: {}".format(DATE_FIELD, ", ".join(bad)))
    if checked == 0:
        return ("OK", "no {} entries with {} — nothing to check".format(ATTRIBUTE, DATE_FIELD))
    return ("OK", "{} {} value(s) valid".format(checked, DATE_FIELD))


def try_autofix_date(value):
    """Auto-fix to mm-dd-yyyy. Business rule: month is always before day.
    Handles two source shapes:
      1. m-d-yyyy   (pad):        '8-8-2025'   → '08-08-2025'
                                   '1-2-2025'   → '01-02-2025'  (Jan 2)
                                   '8-25-2025'  → '08-25-2025'
      2. yyyy-mm-dd (ISO rearrange): '2025-08-08' → '08-08-2025'
                                     '2025-1-2'   → '01-02-2025'
    Returns None for:
      - '25-8-2025'  (25 can't be month — no rearranging for dd-mm sources)
      - '2-30-2025'  (Feb 30 not real)
      - '8/8/2025'   (slash not handled)
      - anything else that doesn't match either shape
    """
    if not isinstance(value, str):
        return None
    s = value.strip()

    # Shape 2 first: ISO YYYY-M-D (4-digit first position disambiguates)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)),
                            int(m.group(3))).strftime("%m-%d-%Y")
        except ValueError:
            return None

    # Shape 1: m-d-yyyy (month first per convention, pad)
    m = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)),
                            int(m.group(2))).strftime("%m-%d-%Y")
        except ValueError:
            return None

    return None


def autofix_array(raw_value):
    """Try to auto-fix every bad DATE_FIELD in raw_value.
       Returns a corrected deep-copy if ALL bad entries are fixable, else None.
       Entries without DATE_FIELD or already-valid values are preserved as-is.
    """
    if not isinstance(raw_value, list):
        return None
    result = copy.deepcopy(raw_value)
    for entry in result:
        if not isinstance(entry, dict) or DATE_FIELD not in entry:
            continue
        value = entry.get(DATE_FIELD)
        if isinstance(value, str) and MM_DD_YYYY_RE.match(value):
            try:
                datetime.strptime(value, "%m-%d-%Y")
                continue  # already valid — leave untouched
            except ValueError:
                pass
        fixed = try_autofix_date(value)
        if fixed is None:
            return None
        entry[DATE_FIELD] = fixed
    return result


###############################################################################
# Shared helpers
###############################################################################

def load_user_ids(filepath):
    ids = []
    with open(filepath, "r") as f:
        for line_num, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            user_id = stripped.split("#")[0].strip().split()[0]
            if user_id:
                ids.append((line_num, user_id))
    return ids


def get_token():
    if TOKEN and TOKEN != "PLACEHOLDER_TOKEN":
        return TOKEN
    if TOKEN_FILE:
        with open(TOKEN_FILE, "r") as f:
            return f.read().strip()
    return None


def make_headers(token):
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def managed_url(base_url, user_id):
    return "{}/openidm/managed/{}/{}".format(base_url.rstrip("/"), MANAGED_OBJECT, user_id)


def fetch_attribute(base_url, user_id, attribute, headers):
    """GET the attribute value. Returns (value, http_status, error_body)."""
    field = attribute.lstrip("/")
    url = "{}?_fields={}".format(managed_url(base_url, user_id), field)
    resp = requests.get(url, headers=headers, verify=VERIFY_SSL, timeout=30)
    if resp.status_code != 200:
        return None, resp.status_code, resp.text
    return resp.json().get(field), 200, ""


def patch_attribute(base_url, user_id, attribute, value, headers):
    """PATCH (replace) the attribute to the given value. Returns (http_status, body)."""
    field = attribute if attribute.startswith("/") else "/" + attribute
    patch_body = [{"operation": "replace", "field": field, "value": value}]
    resp = requests.patch(
        managed_url(base_url, user_id),
        headers=headers,
        json=patch_body,
        verify=VERIFY_SSL,
        timeout=30,
    )
    return resp.status_code, resp.text


def require_token():
    token = get_token()
    if not token:
        print("Error: TOKEN or TOKEN_FILE must be set (and TOKEN != 'PLACEHOLDER_TOKEN')")
        sys.exit(1)
    return token


def require_config_set():
    """Fail fast if ATTRIBUTE or DATE_FIELD still have placeholder values."""
    missing = [k for k, v in [("ATTRIBUTE", ATTRIBUTE), ("DATE_FIELD", DATE_FIELD)]
               if v == "REPLACE_ME"]
    if missing:
        print("Error: set {} in the config section".format(", ".join(missing)))
        sys.exit(1)


def ts_now():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


###############################################################################
# check: audit + generate fixes template
###############################################################################

def cmd_check(_args):
    require_config_set()
    token = require_token()

    try:
        user_entries = load_user_ids(USER_FILE)
    except FileNotFoundError:
        print("Error: file not found: {}".format(USER_FILE))
        sys.exit(1)
    if not user_entries:
        print("Error: no user IDs in {}".format(USER_FILE))
        sys.exit(1)

    headers = make_headers(token)
    total = len(user_entries)

    print("=" * 60)
    print("  IDM Date Format Check")
    print("=" * 60)
    print("  URL:        {}".format(IDM_URL))
    print("  Endpoint:   /openidm/managed/{}/{{id}}".format(MANAGED_OBJECT))
    print("  Attribute:  {}".format(ATTRIBUTE))
    print("  Date field: {}".format(DATE_FIELD))
    print("  Users:      {}".format(total))
    print("  Rule:       mm-dd-yyyy + real calendar date")
    print("=" * 60)
    print("")

    counts = {"OK": 0, "INVALID": 0, "ERROR": 0}
    non_ok = []

    for i, (_line_num, user_id) in enumerate(user_entries, 1):
        try:
            val, http, body = fetch_attribute(IDM_URL, user_id, ATTRIBUTE, headers)
            if http != 200:
                status, detail, raw = "ERROR", "HTTP {} — {}".format(http, body[:100]), None
            else:
                status, detail = check_dates(val)
                raw = val
        except requests.RequestException as e:
            status, detail, raw = "ERROR", "request failed: {}".format(e), None

        counts[status] = counts.get(status, 0) + 1
        print("[{}/{}] {:<8} {} — {}".format(i, total, status, user_id, detail))

        if status != "OK":
            non_ok.append({
                "user_id": user_id,
                "status": status,
                "detail": detail,
                "raw_value": raw,
            })

        if DELAY and i < total:
            time.sleep(DELAY)

    print("")
    print("=" * 60)
    print("  Summary")
    print("=" * 60)
    for k in ("OK", "INVALID", "ERROR"):
        print("  {:<9} {}".format(k + ":", counts.get(k, 0)))
    print("=" * 60)

    invalid_users = [u for u in non_ok if u["status"] == "INVALID"]
    if invalid_users:
        print("")
        print("Invalid user IDs ({}):".format(len(invalid_users)))
        for u in invalid_users:
            print("  {}  — {}".format(u["user_id"], u["detail"]))

    attr_slug = ATTRIBUTE.replace("/", "_")
    ts = ts_now()

    # 1. Audit log — all non-OK users, read-only record
    if non_ok:
        audit_file = "check_{}_{}.json".format(attr_slug, ts)
        with open(audit_file, "w") as f:
            json.dump({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "idm_url": IDM_URL,
                "managed_object": MANAGED_OBJECT,
                "attribute": ATTRIBUTE,
                "date_field": DATE_FIELD,
                "counts": counts,
                "users": non_ok,
            }, f, indent=2)
        print("")
        print("  Audit log saved:  {}".format(audit_file))

    # 2. Fixes template — INVALID users only
    #    Auto-fill corrected_value via zero-padding when the pattern m-d-yyyy fits.
    #    Users where any bad date can't be auto-fixed get corrected_value: null.
    if invalid_users:
        fixes_entries = []
        auto_filled = 0
        needs_manual = 0
        for u in invalid_users:
            suggestion = autofix_array(u["raw_value"])
            if suggestion is not None:
                auto_filled += 1
            else:
                needs_manual += 1
            fixes_entries.append({
                "user_id": u["user_id"],
                "reason": u["detail"],
                "current_value": u["raw_value"],
                "corrected_value": suggestion,
                "auto_filled": suggestion is not None,
            })

        fixes_file = "fixes_{}_{}.json".format(attr_slug, ts)
        with open(fixes_file, "w") as f:
            json.dump({
                "_instructions": (
                    "Review each corrected_value. Auto-filled entries (auto_filled=true) "
                    "were padded from m-d-yyyy — verify they match what you expect. "
                    "Entries with corrected_value=null need manual filling (copy "
                    "current_value and fix the date). Then run: "
                    "python3 idm-check-dates.py fix " + fixes_file
                ),
                "idm_url": IDM_URL,
                "managed_object": MANAGED_OBJECT,
                "attribute": ATTRIBUTE,
                "date_field": DATE_FIELD,
                "users": fixes_entries,
            }, f, indent=2)
        print("  Fixes template:   {}".format(fixes_file))
        print("    auto-filled:    {}  (zero-padded m-d-yyyy → mm-dd-yyyy)".format(auto_filled))
        print("    need manual:    {}  (corrected_value=null)".format(needs_manual))
        print("  → review the template, then run: fix {}".format(fixes_file))


###############################################################################
# fix: apply corrections from an edited fixes template
###############################################################################

def cmd_fix(args):
    require_token_val = require_token()
    headers = make_headers(require_token_val)

    try:
        with open(args.file, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print("Error reading fixes file: {}".format(e))
        sys.exit(1)

    # Validate fixes file
    required = ("idm_url", "managed_object", "attribute", "users")
    missing = [k for k in required if k not in data]
    if missing:
        print("Error: fixes file missing fields: {}".format(", ".join(missing)))
        sys.exit(1)

    if data["idm_url"] != IDM_URL:
        print("Error: IDM URL mismatch — fixes='{}', config='{}'".format(
            data["idm_url"], IDM_URL))
        sys.exit(1)
    if data["managed_object"] != MANAGED_OBJECT:
        print("Error: managed_object mismatch — fixes='{}', config='{}'".format(
            data["managed_object"], MANAGED_OBJECT))
        sys.exit(1)
    if data["attribute"] != ATTRIBUTE:
        print("Error: attribute mismatch — fixes='{}', config='{}'".format(
            data["attribute"], ATTRIBUTE))
        sys.exit(1)

    attribute = data["attribute"]
    all_users = data["users"]
    pending = [u for u in all_users if u.get("corrected_value") is not None]
    skipped = [u for u in all_users if u.get("corrected_value") is None]

    if not pending:
        print("Nothing to fix — all {} user(s) still have corrected_value=null.".format(
            len(all_users)))
        print("Edit {} and fill in corrected_value for users you want to fix.".format(
            args.file))
        sys.exit(0)

    # Re-validate corrected values against the same rule — refuse to write bad data
    rule_failures = []
    for u in pending:
        status, detail = check_dates(u["corrected_value"])
        if status != "OK":
            rule_failures.append((u["user_id"], status, detail))
    if rule_failures:
        print("Error: corrected_value still fails date rules for {} user(s):".format(
            len(rule_failures)))
        for uid, st, dt in rule_failures:
            print("  {} — {}: {}".format(uid, st, dt))
        print("\nFix the corrected_value entries and re-run.")
        sys.exit(1)

    print("=" * 60)
    print("  IDM Date Fix")
    print("=" * 60)
    print("  URL:        {}".format(IDM_URL))
    print("  Endpoint:   /openidm/managed/{}/{{id}}".format(MANAGED_OBJECT))
    print("  Attribute:  {}".format(attribute))
    print("  To fix:     {} user(s)".format(len(pending)))
    print("  Skipped:    {} user(s) (corrected_value still null)".format(len(skipped)))
    print("=" * 60)
    print("")

    rollback_entries = []
    success = 0
    failed = 0
    errors = []
    total = len(pending)

    for i, u in enumerate(pending, 1):
        user_id = u["user_id"]
        new_val = u["corrected_value"]
        original = u.get("current_value")

        try:
            http, body = patch_attribute(IDM_URL, user_id, attribute, new_val, headers)
            if 200 <= http < 300:
                print("[{}/{}] OK: {} (HTTP {})".format(i, total, user_id, http))
                rollback_entries.append({"user_id": user_id, "original_value": original})
                success += 1
            else:
                print("[{}/{}] FAIL: {} (HTTP {}) — {}".format(
                    i, total, user_id, http, body[:200]))
                failed += 1
                errors.append({"user_id": user_id, "http": http, "body": body[:200]})
        except requests.RequestException as e:
            print("[{}/{}] ERROR: {} — {}".format(i, total, user_id, e))
            failed += 1
            errors.append({"user_id": user_id, "http": 0, "body": str(e)})

        if DELAY and i < total:
            time.sleep(DELAY)

    # Always write rollback file if any PATCH succeeded
    if rollback_entries:
        attr_slug = attribute.replace("/", "_")
        rb_file = "fix_rollback_{}_{}.json".format(attr_slug, ts_now())
        with open(rb_file, "w") as f:
            json.dump({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "idm_url": IDM_URL,
                "managed_object": MANAGED_OBJECT,
                "attribute": attribute,
                "users": rollback_entries,
            }, f, indent=2)
        print("")
        print("  Rollback saved:  {}".format(rb_file))
        print("  To undo:  python3 idm-check-dates.py restore {}".format(rb_file))

    print("")
    print("=" * 60)
    print("  Done: {} success, {} failed, {} skipped".format(success, failed, len(skipped)))
    print("=" * 60)
    if errors:
        print("")
        print("Failed users:")
        for e in errors:
            print("  {} — HTTP {} — {}".format(e["user_id"], e["http"], e["body"]))


###############################################################################
# restore: roll back a fix using a fix_rollback_*.json file
###############################################################################

def cmd_restore(args):
    token = require_token()
    headers = make_headers(token)

    try:
        with open(args.file, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print("Error reading rollback file: {}".format(e))
        sys.exit(1)

    attribute = data["attribute"]
    users = data["users"]

    print("=" * 60)
    print("  IDM Date Fix — RESTORE (rollback)")
    print("=" * 60)
    print("  Rollback file: {}".format(args.file))
    print("  URL:           {}".format(data.get("idm_url", IDM_URL)))
    print("  Endpoint:      /openidm/managed/{}/{{id}}".format(
        data.get("managed_object", MANAGED_OBJECT)))
    print("  Attribute:     {}".format(attribute))
    print("  Users:         {}".format(len(users)))
    print("=" * 60)
    print("")

    success = 0
    failed = 0
    total = len(users)
    for i, u in enumerate(users, 1):
        user_id = u["user_id"]
        original = u["original_value"]
        try:
            http, body = patch_attribute(
                data.get("idm_url", IDM_URL), user_id, attribute, original, headers)
            if 200 <= http < 300:
                print("[{}/{}] RESTORED: {} (HTTP {})".format(i, total, user_id, http))
                success += 1
            else:
                print("[{}/{}] FAIL: {} (HTTP {}) — {}".format(
                    i, total, user_id, http, body[:200]))
                failed += 1
        except requests.RequestException as e:
            print("[{}/{}] ERROR: {} — {}".format(i, total, user_id, e))
            failed += 1

        if DELAY and i < total:
            time.sleep(DELAY)

    print("")
    print("=" * 60)
    print("  Restore done: {} success, {} failed".format(success, failed))
    print("=" * 60)


###############################################################################
# Main
###############################################################################

def main():
    parser = argparse.ArgumentParser(
        description="IDM date format check & fix",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Workflow:
  1. check            Scan users, write audit + fixes template
  2. [edit fixes_*.json — fill corrected_value for each user]
  3. fix <file>       Apply corrections from fixes template
  4. restore <file>   Roll back a fix using fix_rollback_*.json""")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="Check date format for users in USER_FILE")

    p_fix = sub.add_parser("fix", help="Apply corrections from edited fixes template")
    p_fix.add_argument("file", help="fixes_*.json file with corrected_value filled in")

    p_restore = sub.add_parser("restore", help="Roll back a fix using rollback file")
    p_restore.add_argument("file", help="fix_rollback_*.json file")

    args = parser.parse_args()

    if args.command == "check":
        cmd_check(args)
    elif args.command == "fix":
        cmd_fix(args)
    elif args.command == "restore":
        cmd_restore(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
