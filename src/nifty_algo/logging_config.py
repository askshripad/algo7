import logging
import os
from datetime import datetime

def setup_logging(log_dir: str, dry_run: bool) -> logging.Logger:
    """Setup logging for algo trading"""
    os.makedirs(log_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    log_path = os.path.join(log_dir, f"algo_trading_{date_str}.log")
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(log_path, encoding='utf-8'),
            logging.StreamHandler()
        ],
        force=True
    )

    logger = logging.getLogger()
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info("="*100)
    logger.info(f"ALGO TRADING SESSION STARTED - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"MODE: {mode}")
    logger.info("="*100)
    return logger