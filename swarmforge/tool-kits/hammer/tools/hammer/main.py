import sys
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit(f"hammer richiede Python 3.11 o successivo, trovato {sys.version.split()[0]}")
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from hammer.cli import main

sys.exit(main())
