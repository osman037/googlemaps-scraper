"""Allow ``python -m google_maps_scraper ...`` alongside ``python main.py``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
