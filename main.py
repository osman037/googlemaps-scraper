#!/usr/bin/env python3
"""Entry point: ``python main.py <command> [options]``.

See ``maps_url_scraper/cli.py`` for the command reference and ``README.md``
for the operations guide.
"""

from maps_url_scraper.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
