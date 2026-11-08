"""
apple-contacts-cleaner — manual, interactive cleanup of macOS/iCloud Contacts.

Three modes:
  delete    — walks every contact, shows its details, asks y/N whether to delete it.
              Nothing is removed until you type DELETE to confirm the whole batch.
  rename    — walks every contact, shows a suggested "First Name" value built from
              whatever is currently in the name fields (prefix/given/middle/family/suffix),
              lets you accept it (Enter), type a replacement, delete the contact
              instead ('d'), skip ('s'), or quit ('q').
              All other info (phone, email, birthday, org, etc.) is left untouched —
              only the name fields are touched, and everything except First Name is cleared.
              Nothing is written until you type SAVE to confirm the whole batch
              (renames and deletions are both included in that one confirmation).
  diagnose  — read-only. Lists every contact this script can see along with which
              account/container it's actually stored in (iCloud, a linked Google
              or Exchange account, "On My Mac", etc.), plus totals per container.
              Useful if contacts you deleted elsewhere are still showing up here —
              it'll usually be because they live in a different, unsynced source.
  snapshot  — read-only. Writes one "identifier<TAB>name" line per contact to a
              file, sorted by identifier. Take a snapshot before some external
              change (e.g. merging WhatsApp-only contacts into Contacts.app),
              take another one after, then `diff` the two files — since each
              contact keeps the same identifier for life, any line that's only
              in the "after" file is a genuinely new contact, not just a changed
              one. Usage: `python3 contacts_cleanup.py snapshot before.txt`

SETUP
-----
    pip install pyobjc-framework-Contacts

The first time you run this, macOS will prompt you to grant your terminal app
Contacts access (System Settings -> Privacy & Security -> Contacts). If you
don't see the prompt, check that setting manually and enable it for Terminal
(or whichever app/shell you're running this from).

BEFORE YOU RUN THIS: export a full backup first —
    Contacts.app -> File -> Export -> Export vCard...
This script is interactive and asks for a final confirmation before writing
anything, but a vCard backup is your safety net if a rename/delete choice
turns out wrong after the fact.

USAGE
-----
    python3 contacts_cleanup.py delete
    python3 contacts_cleanup.py rename
    python3 contacts_cleanup.py rename --start 50     # resume partway through
    python3 contacts_cleanup.py diagnose              # read-only, no --start needed
    python3 contacts_cleanup.py snapshot before.txt    # read-only, then again later:
    python3 contacts_cleanup.py snapshot after.txt     #   diff before.txt after.txt

You can quit at any point with 'q' — everything queued so far is still asked
about in a final confirmation, nothing already-decided is lost, but contacts
you haven't reached yet are simply not touched.
"""

import argparse
import subprocess
import sys
import threading

import Contacts


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def request_access(store):
    status = Contacts.CNContactStore.authorizationStatusForEntityType_(
        Contacts.CNEntityTypeContacts
    )
    if status == Contacts.CNAuthorizationStatusAuthorized:
        return True
    if status == Contacts.CNAuthorizationStatusDenied:
        print(
            "Contacts access is denied for this app. Enable it in "
            "System Settings -> Privacy & Security -> Contacts, then re-run."
        )
        return False

    done = threading.Event()
    result = {"granted": False}

    def handler(granted, error):
        result["granted"] = bool(granted)
        done.set()

    store.requestAccessForEntityType_completionHandler_(
        Contacts.CNEntityTypeContacts, handler
    )
    done.wait(60)
    return result["granted"]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def full_name(c):
    parts = [c.givenName(), c.middleName(), c.familyName()]
    joined = " ".join(p for p in parts if p)
    return joined or "(no name)"


def suggested_name(c):
    """Concatenate whatever is currently in the name fields, in order."""
    parts = [c.namePrefix(), c.givenName(), c.middleName(), c.familyName(), c.nameSuffix()]
    return " ".join(p for p in parts if p)


def print_contact(c, i, total):
    print(f"\n[{i + 1}/{total}] {full_name(c)}")

    org = c.organizationName()
    if org:
        print(f"  Org: {org}")

    phones = []
    for p in c.phoneNumbers():
        try:
            phones.append(p.value().stringValue())
        except Exception:
            pass
    if phones:
        print(f"  Phone: {', '.join(phones)}")

    emails = []
    for e in c.emailAddresses():
        try:
            emails.append(str(e.value()))
        except Exception:
            pass
    if emails:
        print(f"  Email: {', '.join(emails)}")

    bday = c.birthday()
    if bday is not None:
        try:
            y = bday.year() if bday.year() else "?"
            print(f"  Birthday: {bday.day()}/{bday.month()}/{y}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Writes, via AppleScript / Contacts.app
# ---------------------------------------------------------------------------
#
# CNSaveRequest from a bare `python3` process (as opposed to a signed,
# bundled app) reliably fails here with a generic CoreData faulting error
# (NSCocoaErrorDomain 134092) on any *real* write, regardless of which
# field is touched or which account the contact is in — only true no-op
# saves succeed. Contacts.app itself, being a signed system app, has no
# trouble writing the exact same records. So instead of writing through
# the Contacts *framework*, these helpers drive the write through
# Contacts.app's own AppleScript dictionary via `osascript`, using the
# same contact identifier (Contacts.app's "id" matches the framework's
# CNContact identifier, e.g. "...:ABPerson"). Reading/browsing still goes
# through pyobjc above, which has never had trouble.
#
# First use may prompt for an Automation permission (separate from the
# earlier Contacts permission) — System Settings -> Privacy & Security ->
# Automation -> allow this terminal/app to control Contacts.

def _applescript_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run_applescript(script):
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True
    )
    ok = result.returncode == 0
    error = result.stderr.strip() if not ok else None
    return ok, error


def apple_rename(identifier, new_name):
    ident = _applescript_escape(identifier)
    name = _applescript_escape(new_name)
    script = f'''
    tell application "Contacts"
        set thePerson to person id "{ident}"
        set first name of thePerson to "{name}"
        set middle name of thePerson to ""
        set last name of thePerson to ""
        save
    end tell
    '''
    return _run_applescript(script)


def apple_delete(identifier):
    ident = _applescript_escape(identifier)
    script = f'''
    tell application "Contacts"
        delete (person id "{ident}")
        save
    end tell
    '''
    return _run_applescript(script)


def fetch_contacts(store, keys):
    request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
    contacts = []

    def block(contact, stop):
        contacts.append(contact)

    ok, error = store.enumerateContactsWithFetchRequest_error_usingBlock_(
        request, None, block
    )
    if error:
        print(f"Error fetching contacts: {error}")
        sys.exit(1)

    contacts.sort(key=lambda c: (c.givenName() or "", c.familyName() or ""))
    return contacts


def container_label(store, identifier):
    """Which account/source a contact actually lives in (iCloud, a linked
    Google/Exchange account, local "On My Mac", etc.) — useful for figuring
    out why a contact you deleted somewhere is still showing up here.
    """
    predicate = Contacts.CNContainer.predicateForContainerOfContactWithIdentifier_(
        identifier
    )
    containers, error = store.containersMatchingPredicate_error_(predicate, None)
    if error or not containers:
        return "(unknown container)"

    type_names = {
        Contacts.CNContainerTypeLocal: "Local / On My Mac",
        Contacts.CNContainerTypeExchange: "Exchange",
        Contacts.CNContainerTypeCardDAV: "CardDAV (iCloud or other)",
        Contacts.CNContainerTypeUnassigned: "Unassigned",
    }
    c = containers[0]
    type_name = type_names.get(c.type(), f"Unknown type ({c.type()})")
    name = c.name() or "(unnamed)"
    return f"{name} — {type_name}"


# ---------------------------------------------------------------------------
# Diagnose mode
# ---------------------------------------------------------------------------

def diagnose_mode(store):
    """List every contact the framework can see, with its source container,
    plus a count per container. Read-only — makes no changes."""
    keys = [
        Contacts.CNContactIdentifierKey,
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactMiddleNameKey,
    ]
    contacts = fetch_contacts(store, keys)
    print(f"{len(contacts)} contact(s) visible to this script.\n")

    counts = {}
    for c in contacts:
        label = container_label(store, c.identifier())
        counts[label] = counts.get(label, 0) + 1
        print(f"{full_name(c):40s}  [{label}]  id={c.identifier()}")

    print("\n--- Totals by container ---")
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {label}")


# ---------------------------------------------------------------------------
# Snapshot mode
# ---------------------------------------------------------------------------

def snapshot_mode(store, out_path):
    """Write identifier<TAB>name for every contact to a file, sorted by
    identifier. Since a contact's identifier never changes, diffing two
    snapshot files taken at different times shows exactly which contacts
    are new (or gone) between the two — not just which ones look
    different, which changed name, etc."""
    keys = [
        Contacts.CNContactIdentifierKey,
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactMiddleNameKey,
    ]
    contacts = fetch_contacts(store, keys)
    rows = sorted(
        (c.identifier(), full_name(c)) for c in contacts
    )
    with open(out_path, "w") as f:
        for identifier, name in rows:
            f.write(f"{identifier}\t{name}\n")
    print(f"Wrote {len(rows)} contact(s) to {out_path}.")


# ---------------------------------------------------------------------------
# Delete mode
# ---------------------------------------------------------------------------

def delete_mode(store, start):
    keys = [
        Contacts.CNContactIdentifierKey,
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactMiddleNameKey,
        Contacts.CNContactOrganizationNameKey,
        Contacts.CNContactPhoneNumbersKey,
        Contacts.CNContactEmailAddressesKey,
        Contacts.CNContactBirthdayKey,
    ]
    contacts = fetch_contacts(store, keys)
    print(f"{len(contacts)} contacts loaded.")

    to_delete = []  # (identifier, display_name)
    try:
        for i in range(start, len(contacts)):
            c = contacts[i]
            print_contact(c, i, len(contacts))
            choice = input("Delete this contact? [y/N/q to quit]: ").strip().lower()
            if choice == "q":
                break
            if choice == "y":
                to_delete.append((c.identifier(), full_name(c)))
    except KeyboardInterrupt:
        print("\nInterrupted.")

    if not to_delete:
        print("\nNo contacts marked for deletion. Nothing written.")
        return

    print(f"\n{len(to_delete)} contact(s) marked for deletion:")
    for _, name in to_delete:
        print(f"  - {name}")

    confirm = input('\nType DELETE (all caps) to confirm and write changes: ').strip()
    if confirm != "DELETE":
        print("Not confirmed. Nothing written.")
        return

    deleted = 0
    failed = []
    for identifier, name in to_delete:
        ok, error = apple_delete(identifier)
        if ok:
            deleted += 1
            print(f'  deleted: "{name}"')
        else:
            failed.append((name, identifier, error))
            print(f'  FAILED to delete "{name}": {error}')

    if failed:
        print(f"\n{len(failed)} contact(s) failed to delete:")
        for name, identifier, err in failed:
            print(f'  - "{name}"  [{container_label(store, identifier)}]  id={identifier}')
            print(f'    {err}')

    print(f"\nDeleted {deleted}/{len(to_delete)} contact(s).")


# ---------------------------------------------------------------------------
# Rename mode
# ---------------------------------------------------------------------------

def rename_mode(store, start):
    keys = [
        Contacts.CNContactIdentifierKey,
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactMiddleNameKey,
        Contacts.CNContactNamePrefixKey,
        Contacts.CNContactNameSuffixKey,
        Contacts.CNContactOrganizationNameKey,
        Contacts.CNContactPhoneNumbersKey,
        Contacts.CNContactEmailAddressesKey,
        Contacts.CNContactBirthdayKey,
    ]
    contacts = fetch_contacts(store, keys)
    print(f"{len(contacts)} contacts loaded.")

    queued = []  # (identifier, old_display, new_name)
    to_delete = []  # (identifier, display_name)

    try:
        for i in range(start, len(contacts)):
            c = contacts[i]
            print_contact(c, i, len(contacts))
            suggestion = suggested_name(c)
            print(f'  Suggested First Name: "{suggestion}"')
            choice = input(
                "  Enter=accept, type a name to override, "
                "'d'=delete, 's'=skip, 'q'=quit: "
            ).strip()

            if choice.lower() == "q":
                break
            if choice.lower() == "s":
                continue
            if choice.lower() == "d":
                to_delete.append((c.identifier(), full_name(c)))
                print(f'  -> queued for DELETION: "{full_name(c)}"')
                continue

            new_name = choice if choice else suggestion
            if not new_name:
                print("  (nothing to set, skipping)")
                continue

            queued.append((c.identifier(), full_name(c), new_name))
            print(f'  -> queued: "{new_name}"')
    except KeyboardInterrupt:
        print("\nInterrupted.")

    if not queued and not to_delete:
        print("\nNo changes queued. Nothing written.")
        return

    if queued:
        print(f"\n{len(queued)} contact(s) queued for rename:")
        for _, old, new in queued:
            print(f'  - "{old}"  ->  "{new}"')

    if to_delete:
        print(f"\n{len(to_delete)} contact(s) queued for DELETION:")
        for _, name in to_delete:
            print(f'  - "{name}"')

    confirm = input('\nType SAVE (all caps) to confirm and write changes: ').strip()
    if confirm != "SAVE":
        print("Not confirmed. Nothing written.")
        return

    renamed = 0
    failed = []
    for identifier, old, new_name in queued:
        ok, error = apple_rename(identifier, new_name)
        if ok:
            renamed += 1
            print(f'  renamed: "{old}" -> "{new_name}"')
        else:
            failed.append((old, identifier, error))
            print(f'  FAILED to rename "{old}": {error}')

    deleted = 0
    for identifier, name in to_delete:
        ok, error = apple_delete(identifier)
        if ok:
            deleted += 1
            print(f'  deleted: "{name}"')
        else:
            failed.append((name, identifier, error))
            print(f'  FAILED to delete "{name}": {error}')

    if failed:
        print(f"\n{len(failed)} contact(s) failed:")
        for name, identifier, err in failed:
            print(f'  - "{name}"  id={identifier}')
            print(f'    {err}')

    print(f"\nRenamed {renamed}/{len(queued)}, deleted {deleted}/{len(to_delete)} contact(s).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Manual Contacts.app cleanup.")
    parser.add_argument("mode", choices=["delete", "rename", "diagnose", "snapshot"])
    parser.add_argument(
        "--start", type=int, default=0, help="index to resume from (0-based)"
    )
    parser.add_argument(
        "snapshot_path",
        nargs="?",
        help="output file path, required for 'snapshot' mode",
    )
    args = parser.parse_args()

    store = Contacts.CNContactStore.alloc().init()
    if not request_access(store):
        print("Contacts access not granted. Exiting.")
        sys.exit(1)

    if args.mode == "delete":
        delete_mode(store, args.start)
    elif args.mode == "rename":
        rename_mode(store, args.start)
    elif args.mode == "diagnose":
        diagnose_mode(store)
    else:
        if not args.snapshot_path:
            print("Usage: python3 contacts_cleanup.py snapshot <output_file>")
            sys.exit(1)
        snapshot_mode(store, args.snapshot_path)


if __name__ == "__main__":
    main()
