"""Run analyses against existing local data; no simulation is started."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, help='Directory containing Boundary, microcensus/Mikrozensus and Neuchatel_10pct')
    parser.add_argument('--steps', nargs='+', type=int, default=list(range(2, 16)))
    args = parser.parse_args()
    if args.base: os.environ['ABMT_DATA_DIR'] = str(args.base.expanduser().resolve())
    root = Path(__file__).resolve().parent
    if args.steps == list(range(2,16)) and (root/'results').exists():
        suffix=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        archive=root/f'results_previous_{suffix}'
        (root/'results').rename(archive)
        print(f'Previous results preserved in {archive.name}; starting a fresh complete output set.',flush=True)
    for step in args.steps:
        matches = list(root.glob(f'ABMT_Neuchatel_Step{step}_*.py'))
        if len(matches) != 1: parser.error(f'Step {step} is not available.')
        print(f'Running Step {step}: {matches[0].name}', flush=True)
        subprocess.run([sys.executable, str(matches[0])], cwd=root, check=True, env=os.environ.copy())
    print('All requested analyses finished. Results: assignment2_analysis/results/.')


if __name__ == '__main__': main()
