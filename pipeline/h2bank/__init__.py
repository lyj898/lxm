"""h2bank - pipeline for building an H2 Mathematics (9758) question bank.

Stages are runnable as modules and are all idempotent:

    python -m h2bank.initdb
    python -m h2bank.crawl   [--cap N] [--discover-only] [--download-only]
    python -m h2bank.split
    python -m h2bank.tag     [--no-llm]
"""

__version__ = "0.1.0"
