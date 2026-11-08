# apple-contacts-cleaner

A macOS/iCloud Contacts cleanup utility that provides interactive tools for managing contacts through the Contacts.app framework.

## Features

- **Delete Contacts** – Walk through all contacts, show details, and ask for confirmation before deletion. Nothing is removed until you type `DELETE` to confirm the entire batch.
- **Rename Contacts** – Walk through contacts, suggest a new first name based on the current name fields (prefix, given, middle, family, suffix), and let you accept, replace, or skip each contact. All other contact information is preserved except the first name.
- **Diagnose** – Read-only mode that lists every contact the script can see along with their source container (iCloud, Google, Exchange, On My Mac, etc.).
- **Snapshot** – Write a snapshot of contacts to a file for comparison before external changes.

## Requirements

- macOS with the Contacts framework (pyobjc-framework-Contacts)
- Python 3

### Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# Delete all contacts (with confirmation)
python3 main.py delete

# Rename contacts (interactive)
python3 main.py rename

# Diagnose contacts (read-only)
python3 main.py diagnose

# Snapshot contacts to a file
python3 main.py snapshot before.txt
```

## How It Works

The script uses AppleScript via `osascript` to interact with Contacts.app, allowing it to read, rename, and delete contacts without requiring direct database access. All changes are batched and require explicit confirmation before being written.

## Security

- All contact modifications are batched and require explicit user confirmation (`DELETE`, `SAVE`, or `q` to quit).
- No hardcoded credentials or secrets are present in the code.
- The script relies on the macOS Contacts framework for data access, which requires user authorization at runtime.

## License

MIT
