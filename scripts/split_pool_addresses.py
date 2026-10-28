#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆分 pool addresses JSON 文件成多个小文件，每文件 3000 个地址.

按照代码格式: [{"address":"...","label":"..."}]

用法:
  cd scripts
  python split_pool_addresses.py ../docs/pool-addresses-*.json
"""

import json
import sys
import io
from pathlib import Path
from datetime import datetime

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def split_addresses(json_file: str, chunk_size: int = 3000, prefix: str = "pool-"):
    """Split addresses from JSON file into multiple files."""
    print(f"Loading {json_file}...")
    
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    addresses = data.get("addresses", [])
    total = len(addresses)
    
    print(f"Found {total} addresses")
    print(f"Exported at: {data.get('exported_at', 'unknown')}")
    print(f"\nSplitting into chunks of {chunk_size}...")
    
    # Get output directory (same as input file)
    input_path = Path(json_file)
    output_dir = input_path.parent
    
    # Extract timestamp from filename if exists
    filename_base = input_path.stem
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    
    num_chunks = (total + chunk_size - 1) // chunk_size
    
    for chunk_idx in range(num_chunks):
        start_idx = chunk_idx * chunk_size
        end_idx = min((chunk_idx + 1) * chunk_size, total)
        chunk_addresses = addresses[start_idx:end_idx]
        
        # Format as required: [{"address":"...","label":"..."}]
        formatted_data = []
        for i, addr in enumerate(chunk_addresses, start=start_idx + 1):
            formatted_data.append({
                "address": addr.strip(),
                "label": f"{prefix}{i:05d}"
            })
        
        # Create output filename
        chunk_num = chunk_idx + 1
        output_file = output_dir / f"pool-wallets-chunk-{chunk_num:03d}-{timestamp}.json"
        
        # Write JSON
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(formatted_data, f, indent=2, ensure_ascii=False)
        
        print(f"  [chunk {chunk_num:2d}] {output_file.name}")
        print(f"           {len(chunk_addresses):5d} addresses (#{start_idx+1:5d}-#{end_idx:5d})")
    
    print(f"\nDone! Created {num_chunks} files.")
    print(f"Output directory: {output_dir}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python split_pool_addresses.py <json_file> [chunk_size] [prefix]")
        print("Example: python split_pool_addresses.py pool-addresses-*.json 3000 pool-")
        sys.exit(1)
    
    json_file = sys.argv[1]
    chunk_size = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    prefix = sys.argv[3] if len(sys.argv) > 3 else "pool-"
    
    if not Path(json_file).exists():
        print(f"Error: {json_file} not found")
        sys.exit(1)
    
    split_addresses(json_file, chunk_size, prefix)
