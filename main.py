#!/usr/bin/env python3
"""Entry point: ``python main.py <command> [options]``.

See ``google_maps_scraper/cli.py`` for the command reference and ``README.md``
for the operations guide.
"""

from google_maps_scraper.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
