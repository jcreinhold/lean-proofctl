"""Module entrypoint for ``python -m lean_proofctl``."""

from lean_proofctl.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
