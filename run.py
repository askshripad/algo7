import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from nifty_algo.main import main

if __name__ == "__main__":
    try:
        main()
    finally:
        # log_end_of_day_summary and db close are inside main's finally
        pass
