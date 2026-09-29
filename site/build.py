"""Build lasto.dev.

    python site/build.py build [--strict] [--out DIR]
    python site/build.py serve [--port 8000] [--watch]

Needs the packages in site/requirements.txt. Never imports the lasto app.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sitegen.cli import main  # noqa: E402

raise SystemExit(main())
