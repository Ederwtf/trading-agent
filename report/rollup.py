"""
Rollup del journal (M3). SIN LLM.

El journal se commitea en cada corrida (~40 archivos/día); sin mantenimiento el repo se
infla y `load_latest_journal` se hace lento. Este script archiva los journals con más de
`ROLLUP_DAYS` días en `journal/archive/YYYY-MM.jsonl` (una línea JSON por journal) y borra
los archivos sueltos. El dashboard lee live + archivo, así que el histórico se conserva.

Idempotente y barato: no hace nada hasta que hay archivos suficientemente viejos. Pensado
para correr en cada corrida del workflow (es no-op la mayoría de las veces).

Uso:  python report/rollup.py
"""

import glob
import json
import os
import sys
from datetime import datetime, timedelta

for _s in (sys.stdout, sys.stderr):
    rc = getattr(_s, "reconfigure", None)
    if rc:
        rc(encoding="utf-8")

ROLLUP_DAYS = int(os.getenv("ROLLUP_DAYS", "30"))
ARCHIVE_DIR = os.path.join("journal", "archive")


def _journal_date(path: str):
    """Fecha del journal desde su contenido (timestamp) o, si falla, desde el nombre."""
    try:
        with open(path, encoding="utf-8") as f:
            ts = json.load(f).get("timestamp", "")
        if ts:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
    base = os.path.basename(path)          # SYMBOL_YYYY-MM-DD_HHMMSS.json
    try:
        part = base.split("_", 1)[1]
        return datetime.strptime(part[:10], "%Y-%m-%d")
    except Exception:
        return None


def rollup() -> None:
    cutoff = datetime.now() - timedelta(days=ROLLUP_DAYS)
    to_archive = {}   # 'YYYY-MM' → lista de (path, dict)
    for path in glob.glob("journal/*.json"):
        if os.path.basename(path) == "state.json":
            continue
        d = _journal_date(path)
        if d is None or d >= cutoff:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        month = d.strftime("%Y-%m")
        to_archive.setdefault(month, []).append((path, data))

    if not to_archive:
        print(f"  [rollup] nada que archivar (umbral {ROLLUP_DAYS} días).")
        return

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    moved = 0
    for month, items in sorted(to_archive.items()):
        arch = os.path.join(ARCHIVE_DIR, f"{month}.jsonl")
        with open(arch, "a", encoding="utf-8") as out:
            for path, data in items:
                data.pop("_file", None)
                out.write(json.dumps(data, default=str, ensure_ascii=False) + "\n")
        for path, _ in items:
            try:
                os.remove(path)
                moved += 1
            except Exception:
                pass
        print(f"  [rollup] {month}: archivados {len(items)} → {arch}")
    print(f"  [rollup] total {moved} journals archivados.")


if __name__ == "__main__":
    rollup()
