"""Package generated aggregate CSVs, figures and method JSONs for joint review.

Run after run_all.py. Reads only results/, never the private input directory or
the public reference cache. The resulting ZIP is ignored by Git.
"""
from pathlib import Path
import zipfile


def collect(root=None):
    root=Path(root) if root is not None else Path(__file__).resolve().parent
    results=root/'results'
    for required in ['pt_subscriptions/pt_subscriptions_method.json',
                     'pt_accessibility_common/pt_accessibility_common_method.json']:
        if not (results/required).is_file():
            raise FileNotFoundError('Run the complete Steps2-15 analysis before collecting review outputs.')
    files=sorted(p for p in results.rglob('*') if p.is_file() and not p.is_symlink()
        and (p.suffix.casefold() in {'.csv','.png'} or
             (p.suffix.casefold()=='.json' and 'method' in p.name)))
    archive=root/'assignment2_review.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as target:
        for path in files:
            target.write(path,path.relative_to(root))
    print(f'Collected {len(files)} aggregate tables, figures and method files: {archive}')
    return archive


if __name__=='__main__':
    collect()
