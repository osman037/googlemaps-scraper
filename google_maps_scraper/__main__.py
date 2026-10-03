"""Allow ``python -m maps_url_scraper ...`` alongside ``python main.py``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
