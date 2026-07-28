#!/usr/bin/env python3
"""ClickHouse data warehouse query tool.

Usage:
    python query.py --sql "SELECT * FROM market_data LIMIT 10"
    python query.py --list-tables

Environment:
    CLICKHOUSE_HOST, CLICKHOUSE_PORT, CLICKHOUSE_DATABASE,
    CLICKHOUSE_USER, CLICKHOUSE_PASSWORD
"""

import argparse
import json
import os
import sys


def get_clickhouse_client():
    """Get ClickHouse client with environment config."""
    host = os.environ.get("CLICKHOUSE_HOST", "")
    if not host:
        return None

    try:
        from clickhouse_connect import get_client
        client = get_client(
            host=host,
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
            database=os.environ.get("CLICKHOUSE_DATABASE", "ashare"),
        )
        return client
    except ImportError:
        return None
    except Exception as e:
        print(json.dumps({"available": False, "reason": f"Connection failed: {e}"}))
        return None


def list_tables():
    """List all tables in the configured database."""
    client = get_clickhouse_client()
    if client is None:
        print(json.dumps({
            "available": False,
            "reason": "CLICKHOUSE_HOST not configured or clickhouse-connect not installed"
        }))
        return

    db = os.environ.get("CLICKHOUSE_DATABASE", "ashare")
    try:
        tables = client.query(
            f"SELECT name, engine, total_rows, total_bytes FROM system.tables "
            f"WHERE database = '{db}' ORDER BY name"
        )
        result = []
        for row in tables.named_results():
            # Get column info for each table
            cols = client.query(
                f"SELECT name, type FROM system.columns "
                f"WHERE database = '{db}' AND table = '{row['name']}' "
                f"ORDER BY position"
            )
            result.append({
                "table": row["name"],
                "engine": row["engine"],
                "rows": row["total_rows"],
                "columns": [{"name": c["name"], "type": c["type"]} for c in cols.named_results()]
            })
        print(json.dumps(result, ensure_ascii=False, default=str))
    except Exception as e:
        print(json.dumps({"available": False, "reason": str(e)}))
    finally:
        client.close()


def query_warehouse(sql):
    """Execute a SQL query against ClickHouse."""
    client = get_clickhouse_client()
    if client is None:
        print(json.dumps({
            "available": False,
            "reason": "CLICKHOUSE_HOST not configured or clickhouse-connect not installed"
        }))
        return

    try:
        result = client.query(sql)
        rows = list(result.named_results())
        print(json.dumps(rows, ensure_ascii=False, default=str))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description="ClickHouse data warehouse query tool")
    parser.add_argument("--sql", type=str, help="SQL query to execute")
    parser.add_argument("--list-tables", action="store_true", help="List all available tables")
    args = parser.parse_args()

    if args.list_tables:
        list_tables()
    elif args.sql:
        query_warehouse(args.sql)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()