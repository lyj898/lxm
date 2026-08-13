"""Create or migrate the SQLite database: `python -m h2bank.initdb`."""

from __future__ import annotations

import argparse
import logging

from .config import load_config
from .db import init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise the h2bank database")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config()
    cfg.pdf_dir.mkdir(parents=True, exist_ok=True)
    conn = init_db(cfg.db_path, cfg.schema_path)
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    n_topics = conn.execute("SELECT COUNT(*) AS c FROM topics").fetchone()["c"]
    conn.close()

    print(f"db:     {cfg.db_path}")
    print(f"tables: {', '.join(tables)}")
    print(f"topics: {n_topics} seeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
