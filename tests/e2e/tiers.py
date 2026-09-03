"""How many rows sit on each tier right now.

python tiers.py <frigate.db> [cold-path-prefix]
"""

import sqlite3
import sys
from collections import Counter

conn = sqlite3.connect(sys.argv[1])
cold_prefix = sys.argv[2] if len(sys.argv) > 2 else "/media/archive/"

for table in ("recordings", "previews"):
    tally = Counter(
        "cold" if path.startswith(cold_prefix) else "hot"
        for (path,) in conn.execute(f"select path from {table}")
    )
    print(f"{table:<12} hot={tally['hot']:<5} cold={tally['cold']}")
