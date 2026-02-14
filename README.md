# Updatery

A terminal UI tool for monitoring and installing package updates. Currently supports [winget](https://github.com/microsoft/winget-cli), with plans to add more package managers in the future.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- Scans for available package updates via `winget list`
- Displays updatable packages in an interactive terminal UI
- Select individual packages or select all at once
- Runs upgrades silently in the background with live log output
- Auto-elevates to administrator for seamless installs
- Human-readable error messages for common winget exit codes

## Installation

Requires Python 3.10+ and Windows.

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/updatery.git
cd updatery

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
python updatery.py
```

The app will request administrator privileges on launch (required for installing updates). If running inside Windows Terminal, the elevated process opens in a new WT tab with full Unicode support.

### Keybindings

| Key | Action           |
|-----|------------------|
| `R` | Refresh list     |
| `U` | Upgrade selected |
| `A` | Select all       |
| `Q` | Quit             |

## How It Works

1. Runs `winget list` and parses the fixed-width column output
2. Filters to packages from the `winget` source that have an available update
3. Displays them in a Textual `SelectionList`
4. Runs `winget upgrade --id <package> --silent` for each selected package, streaming output to a log panel

## License

MIT
