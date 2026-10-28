#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入 pool addresses 到数据库.

用法:
  cd app
  python ../scripts/import_pool_addresses.py ../docs/pool-addresses-*.json
"""

import json
import sys
import io
from pathlib import Path

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# Add app dir to path
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from database import get_db


def import_addresses(json_file: str, prefix: str = "pool-"):
    """Import addresses from JSON file into database."""
    print(f"Loading {json_file}...")
    
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    addresses = data.get("addresses", [])
    total = data.get("total", 0)
    
    print(f"Found {len(addresses)} addresses (reported total: {total})")
    print(f"Exported at: {data.get('exported_at', 'unknown')}")
    
    db = get_db()

    items = [{"address": addr.strip(), "label": f"{prefix}{i:05d}",
              "enabled": True}
             for i, addr in enumerate(addresses, 1)]
    print("Importing in one transaction...")
    res = db.import_wallets(items)

    print(f"\n✓ Import complete:")
    print(f"  Added:   {res['added']}")
    print(f"  Updated: {res['updated']}")
    print(f"  Invalid: {res['invalid']}")
    print(f"  Skipped: {res['skipped']}")
    print(f"  Total:   {len(items)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_pool_addresses.py <json_file> [prefix]")
        print("Example: python import_pool_addresses.py ../docs/pool-addresses-*.json pool-")
        sys.exit(1)
    
    json_file = sys.argv[1]
    prefix = sys.argv[2] if len(sys.argv) > 2 else "pool-"
    
    if not Path(json_file).exists():
        print(f"Error: {json_file} not found")
        sys.exit(1)
    
    import_addresses(json_file, prefix)
