"""Download IBM AMLworld HI-Small from Kaggle into data/raw/.

    uv run python scripts/download_data.py            # skip if already present
    uv run python scripts/download_data.py --force    # re-download

Needs Kaggle credentials: ~/.kaggle/kaggle.json, or KAGGLE_USERNAME + KAGGLE_KEY
in the environment / .env. You must also accept the dataset's terms once on
its Kaggle page.
"""

import argparse
import os
import sys
from pathlib import Path

DATASET = "ealtman2019/ibm-transactions-for-anti-money-laundering-aml"
# HI = higher illicit ratio, Small = ~5M transactions. Only these files are needed.
FILES = ("HI-Small_Trans.csv", "HI-Small_accounts.csv", "HI-Small_Patterns.txt")

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"


def load_dotenv_kaggle_creds(env_path: Path) -> None:
    """Put KAGGLE_* values from .env into os.environ (the Kaggle client reads env vars)."""
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if sep and key in ("KAGGLE_USERNAME", "KAGGLE_KEY") and value:
            os.environ.setdefault(key, value)


def missing_files(raw_dir: Path) -> list[str]:
    return [f for f in FILES if not (raw_dir / f).exists()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--force", action="store_true", help="download even if files exist")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    todo = list(FILES) if args.force else missing_files(RAW_DIR)
    if not todo:
        print(f"All files already present in {RAW_DIR} — skipping. Use --force to re-download.")
        return 0

    load_dotenv_kaggle_creds(REPO_ROOT / ".env")
    # Imported here: `import kaggle` authenticates immediately and exits if creds are missing.
    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    api.authenticate()

    for name in todo:
        print(f"Downloading {name} ...")
        # Kaggle serves big single files zipped; unzip=True extracts in place.
        api.dataset_download_file(DATASET, name, path=str(RAW_DIR), force=args.force, quiet=False)
        zipped = RAW_DIR / f"{name}.zip"
        if zipped.exists():
            import zipfile

            with zipfile.ZipFile(zipped) as zf:
                zf.extractall(RAW_DIR)
            zipped.unlink()

    still_missing = missing_files(RAW_DIR)
    if still_missing:
        print(f"ERROR: still missing after download: {still_missing}", file=sys.stderr)
        return 1

    for name in FILES:
        size_mb = (RAW_DIR / name).stat().st_size / 1_000_000
        print(f"  {name:28s} {size_mb:8.1f} MB")
    print(f"Done. Files are in {RAW_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
