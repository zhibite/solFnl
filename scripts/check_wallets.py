#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from database import get_db

db = get_db()
wallets = db.list_wallets()
print(f"Total wallets: {len(wallets)}")
print("\nLatest 5:")
for w in wallets[-5:]:
    print(f"  {w['address'][:20]}... - {w['label']}")
print("\nFirst 5:")
for w in wallets[:5]:
    print(f"  {w['address'][:20]}... - {w['label']}")
