#!/bin/bash
# Run Voxtype for development

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/app"

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    # Use system Python (not Homebrew) for native extensions (pyaudio, evdev)
    PYTHON=$(/usr/bin/python3 --version >/dev/null 2>&1 && echo /usr/bin/python3 || echo python3)
    if command -v uv &> /dev/null; then
        uv venv --python "$PYTHON"
        uv pip install -r requirements.txt
    else
        $PYTHON -m venv .venv
        .venv/bin/pip install -r requirements.txt
    fi
fi

exec .venv/bin/python3 -m src.main "$@"
