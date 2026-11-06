#!/usr/bin/env python3
"""Rebuilds the local pulse_dev database with a copy of the production tables. Only reads from production.
Laptop tokens stay behind, and prompts aren't copied because they live in R2."""
import os
import pathlib
import subprocess
import urllib.parse

WORKER = pathlib.Path(__file__).resolve().parent.parent
SKIP = {"tokens"}
# Developers first, because other tables point at them.
ORDER = "CASE c.table_name WHEN 'developers' THEN 0 ELSE 1 END, c.table_name"


def production_env():
    secrets = WORKER / ".dev.vars"
    lines = secrets.read_text().splitlines() if secrets.exists() else []
    urls = [line.split("=", 1)[1].strip().strip("\"'") for line in lines if line.startswith("DATABASE_URL=")]
    if not urls:
        raise SystemExit("worker/.dev.vars has no DATABASE_URL, so there's nothing to copy from.")
    url = urllib.parse.urlsplit(urls[0])
    # Separate settings rather than one URL, so the password isn't visible in the process list.
    return {**os.environ, "PGHOST": url.hostname, "PGPORT": str(url.port or 5432), "PGDATABASE": url.path.lstrip("/"),
            "PGUSER": urllib.parse.unquote(url.username), "PGPASSWORD": urllib.parse.unquote(url.password or ""), "PGSSLMODE": "require"}


def local_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("PG")}


def local_tables():
    out = subprocess.run(["psql", "-X", "-At", "-d", "pulse_dev", "-c",
                          "SELECT c.table_name, string_agg(quote_ident(c.column_name), ',' ORDER BY c.ordinal_position) FROM information_schema.columns c "
                          f"WHERE c.table_schema = 'tracker' GROUP BY c.table_name ORDER BY {ORDER}"],
                         env=local_env(), check=True, capture_output=True, text=True).stdout
    return [tuple(line.split("|")) for line in out.splitlines()]


def main():
    source = production_env()  # before the rebuild, so a missing setting leaves the local database alone
    subprocess.run(["bash", str(WORKER / "dev" / "reset-db.sh"), "--empty"], check=True)
    for table, columns in local_tables():
        if table in SKIP:
            continue
        read = subprocess.Popen(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", "SET default_transaction_read_only = on",
                                 "-c", f"COPY (SELECT {columns} FROM tracker.{table}) TO STDOUT"], env=source, stdout=subprocess.PIPE)
        write = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", "pulse_dev", "-c", f"COPY tracker.{table} ({columns}) FROM STDIN"],
                               env=local_env(), stdin=read.stdout)
        read.stdout.close()
        if read.wait() or write.returncode:
            raise SystemExit(f"Couldn't copy {table}, so pulse_dev is incomplete. Run this again, or npm run db:reset for made-up data.")
        print(f"Copied {table}")
    print("pulse_dev now holds a copy of production. Run: npm run dev")


if __name__ == "__main__":
    main()
