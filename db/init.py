"""Creates or upgrades the database: python -m db.init"""

from common import config, log
from db import connect


def main() -> None:
    log.setup()
    with connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    print(f"Database ready at {config.db_path()} (schema version {version})")


if __name__ == "__main__":
    main()
