import shutil
from pathlib import Path
import sys

def restore(run_id: str, legacy_root: Path, target_root: Path):
    src = legacy_root / run_id / 'power'
    dst = target_root / run_id / 'power'
    if not src.exists():
        print(f"source {src} does not exist")
        return 2
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for p in src.iterdir():
        if p.is_file() and p.name.startswith('power_'):
            shutil.copy2(p, dst / p.name)
            copied += 1
    print(f"copied {copied} files from {src} to {dst}")
    return 0

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: restore_power_for_run.py RUN_ID')
        sys.exit(2)
    run_id = sys.argv[1]
    legacy_root = Path('output-legacy/v2/drone/run_1760295993_extracted')
    target_root = Path('output/drone')
    sys.exit(restore(run_id, legacy_root, target_root))
