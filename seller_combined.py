#!/usr/bin/env python3
# ================================================================
#  seller_combined.py — Runs B2B Seller + Freelance Seller
#  Both run in parallel threads in a single Railway worker
#  Saves one Railway service slot
# ================================================================

import threading
import logging

log = logging.getLogger(__name__)

def run_b2b():
    import seller_b2b
    seller_b2b.run()

def run_freelance():
    import seller_freelance
    seller_freelance.run()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    log.info("=" * 60)
    log.info("  SELLER TEAM — BOTH SELLERS STARTING")
    log.info("  B2B Seller + Freelance Seller running in parallel")
    log.info("=" * 60)

    t1 = threading.Thread(target=run_b2b,      name="B2B-Seller",      daemon=True)
    t2 = threading.Thread(target=run_freelance, name="Freelance-Seller", daemon=True)

    t1.start()
    t2.start()

    t1.join()
    t2.join()
