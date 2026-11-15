#!/bin/bash
# Launch the Textual dashboard. It is a viewer: closing it does not stop monitoring.
set -euo pipefail
cd "$(dirname "$0")"

python=${PYTHON:-}
if [ -z "$python" ]; then
    if [ -x .venv/bin/python ]; then
        python=.venv/bin/python
    else
        python=python3
    fi
fi

if ! "$python" -c 'import textual' >/dev/null 2>&1; then
    echo "textual is missing; install the dependencies with:" >&2
    echo "  uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt" >&2
    echo "The dependency-free fallback is: $python monitor.py --demo" >&2
    exit 2
fi

if [ "$#" -eq 0 ]; then
    echo "Live mode: attaching to the running monitor, or starting one from hosts.csv." >&2
    echo "Nothing configured yet? Try: ./run_dashboard.sh --demo" >&2
fi

exec "$python" dashboard.py "$@"
