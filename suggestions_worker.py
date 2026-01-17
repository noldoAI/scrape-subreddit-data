#!/usr/bin/env python3
"""
Background Suggestions Sync Worker

Automatically syncs subreddits from `subreddit_suggestions` collection (populated by
external LLM system like nakle_llm) into the active scraper's queue.

When new suggestions are added to the DB, they are automatically synced within 60 seconds
and prioritized for immediate scraping.

This module can be:
1. Imported and run as a background thread in the API server
2. Run as a standalone script for manual processing

Usage:
    # As background thread (in api.py):
    from suggestions_worker import SuggestionsWorker
    worker = SuggestionsWorker(db)
    worker.start_background()

    # As standalone script:
    python suggestions_worker.py --sync          # Sync once and exit
    python suggestions_worker.py --stats         # Show statistics
    python suggestions_worker.py --daemon        # Run continuously
"""

import os
import sys
import time
import threading
import logging
import argparse
from datetime import datetime, UTC
from typing import Dict, List, Optional

from dotenv import load_dotenv
from pymongo import MongoClient

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SUGGESTIONS_SYNC_CONFIG, COLLECTIONS

# Import Azure logging helper
try:
    from core.azure_logging import setup_azure_logging
    logger = setup_azure_logging('suggestions-worker', level=logging.INFO)
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger('suggestions-worker')


class SuggestionsWorker:
    """
    Background worker that syncs subreddit suggestions to the active scraper queue.

    Flow:
    1. Check subreddit_suggestions for unsynced documents (synced_at doesn't exist)
    2. Extract unique subreddit names
    3. Filter out duplicates already in scraper queue
    4. Add new subreddits to queue + pending_scrape (for priority)
    5. Mark suggestions as synced
    """

    def __init__(self, db, config: Optional[Dict] = None):
        """
        Initialize the suggestions worker.

        Args:
            db: MongoDB database instance
            config: Configuration dict (defaults to SUGGESTIONS_SYNC_CONFIG)
        """
        self.db = db
        self.config = config or SUGGESTIONS_SYNC_CONFIG
        self.running = False
        self._thread = None
        self._stats = {
            "synced": 0,
            "skipped": 0,
            "last_run": None,
            "last_sync_result": None
        }

    def get_pending_suggestions(self) -> List[Dict]:
        """
        Get unsynced suggestions from subreddit_suggestions collection.

        Returns:
            List of suggestion documents that haven't been synced yet
        """
        collection_name = self.config.get("collection_name", "subreddit_suggestions")
        return list(self.db[collection_name].find({
            "synced_at": {"$exists": False}
        }))

    def get_active_scraper(self) -> Optional[Dict]:
        """
        Get the active posts scraper to add subreddits to.

        Returns:
            Scraper document if found, None otherwise
        """
        scraper_type = self.config.get("target_scraper_type", "posts")
        return self.db[COLLECTIONS["SCRAPERS"]].find_one({
            "scraper_type": scraper_type,
            "status": "running"
        })

    def sync_suggestions(self) -> Dict:
        """
        Sync pending suggestions to the active scraper queue.

        This is the main sync method that:
        1. Gets pending (unsynced) suggestions
        2. Finds the active scraper
        3. Extracts unique subreddit names
        4. Filters duplicates
        5. Adds new ones to queue with priority
        6. Marks suggestions as synced

        Returns:
            Dict with sync results: synced count, added subreddits, skipped, etc.
        """
        # 1. Get pending suggestions
        pending = self.get_pending_suggestions()
        if not pending:
            return {"synced": 0, "message": "No pending suggestions"}

        # 2. Get active scraper
        scraper = self.get_active_scraper()
        if not scraper:
            logger.warning("No active scraper found - suggestions will wait until one is running")
            return {"synced": 0, "message": "No active scraper found"}

        scraper_id = scraper.get("subreddit", "unknown")

        # 3. Extract unique subreddit names from all pending documents
        all_suggested = set()
        for doc in pending:
            for sub in doc.get("subreddits", []):
                name = sub.get("name", "").strip()
                if name:
                    all_suggested.add(name.lower())

        if not all_suggested:
            # Mark as synced even with empty subreddits to avoid re-processing
            self._mark_synced(pending, scraper_id)
            return {"synced": 0, "message": "No subreddit names in suggestions"}

        # 4. Filter out duplicates (already in scraper queue)
        existing = set(s.lower() for s in scraper.get("subreddits", []))
        new_subs = [s for s in all_suggested if s not in existing]
        skipped = [s for s in all_suggested if s in existing]

        if not new_subs:
            # All suggested are duplicates - mark as synced
            self._mark_synced(pending, scraper_id)
            self._stats["skipped"] += len(skipped)
            return {
                "synced": 0,
                "skipped": skipped,
                "skipped_count": len(skipped),
                "message": "All suggested subreddits already in queue"
            }

        # 5. Add to scraper queue with priority
        updated_queue = list(existing) + new_subs

        self.db[COLLECTIONS["SCRAPERS"]].update_one(
            {"_id": scraper["_id"]},
            {
                "$set": {
                    "subreddits": updated_queue,
                    "last_updated": datetime.now(UTC)
                },
                "$addToSet": {
                    "pending_scrape": {"$each": new_subs}
                }
            }
        )

        # 6. Clear any old failure counters for new subreddits (fresh start)
        unset_failures = {f"scrape_failures.{sub}": "" for sub in new_subs}
        if unset_failures:
            self.db[COLLECTIONS["SCRAPERS"]].update_one(
                {"_id": scraper["_id"]},
                {"$unset": unset_failures}
            )

        # 7. Mark suggestions as synced
        self._mark_synced(pending, scraper_id)

        # Update stats
        self._stats["synced"] += len(new_subs)
        self._stats["skipped"] += len(skipped)
        self._stats["last_sync_result"] = {
            "added": new_subs,
            "skipped": skipped,
            "scraper": scraper_id,
            "timestamp": datetime.now(UTC).isoformat()
        }

        logger.info(f"Synced {len(new_subs)} subreddits to {scraper_id}: {new_subs}")
        if skipped:
            logger.info(f"Skipped {len(skipped)} duplicates: {skipped}")

        return {
            "synced": len(new_subs),
            "added": new_subs,
            "skipped": skipped,
            "skipped_count": len(skipped),
            "scraper": scraper_id,
            "queue_size": len(updated_queue),
            "message": f"Synced {len(new_subs)} subreddits to queue"
        }

    def _mark_synced(self, docs: List[Dict], scraper_id: str):
        """
        Mark suggestion documents as synced.

        Args:
            docs: List of suggestion documents to mark
            scraper_id: ID of the scraper they were synced to
        """
        if not docs:
            return

        doc_ids = [d["_id"] for d in docs]
        collection_name = self.config.get("collection_name", "subreddit_suggestions")

        self.db[collection_name].update_many(
            {"_id": {"$in": doc_ids}},
            {
                "$set": {
                    "synced_at": datetime.now(UTC),
                    "synced_to_scraper": scraper_id
                }
            }
        )

        logger.debug(f"Marked {len(doc_ids)} suggestion documents as synced")

    def _run_loop(self):
        """Internal method: continuous background processing loop."""
        interval = self.config.get("check_interval", 60)
        logger.info(f"Suggestions worker started (interval: {interval}s)")

        while self.running:
            try:
                result = self.sync_suggestions()

                # Only log if something was synced
                if result.get("synced", 0) > 0:
                    logger.info(f"Sync result: {result}")

                self._stats["last_run"] = datetime.now(UTC)
                time.sleep(interval)

            except Exception as e:
                logger.error(f"Error in suggestions worker loop: {e}")
                time.sleep(30)  # Wait before retry on error

        logger.info("Suggestions worker stopped")

    def start_background(self):
        """Start the worker as a background daemon thread."""
        if self.running:
            logger.warning("Suggestions worker already running")
            return

        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Suggestions worker background thread started")

    def stop(self):
        """Stop the background worker."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Suggestions worker stopped")

    def get_stats(self) -> Dict:
        """
        Get worker statistics.

        Returns:
            Dict with worker stats including sync counts, last run time, pending count
        """
        pending = self.get_pending_suggestions()
        scraper = self.get_active_scraper()

        # Extract subreddit names from pending for preview
        pending_subs = set()
        for doc in pending:
            for sub in doc.get("subreddits", []):
                name = sub.get("name", "").strip()
                if name:
                    pending_subs.add(name.lower())

        return {
            "running": self.running,
            "synced_total": self._stats["synced"],
            "skipped_total": self._stats["skipped"],
            "last_run": self._stats["last_run"].isoformat() if self._stats["last_run"] else None,
            "last_sync_result": self._stats["last_sync_result"],
            "pending_documents": len(pending),
            "pending_subreddits": list(pending_subs),
            "pending_count": len(pending_subs),
            "active_scraper": scraper.get("subreddit") if scraper else None,
            "config": {
                "check_interval": self.config.get("check_interval", 60),
                "target_scraper_type": self.config.get("target_scraper_type", "posts")
            }
        }

    def get_pending_count(self) -> int:
        """Get count of pending suggestion documents."""
        return len(self.get_pending_suggestions())


def main():
    """CLI interface for the suggestions worker."""
    parser = argparse.ArgumentParser(
        description='Background worker for syncing subreddit suggestions to scraper queue'
    )
    parser.add_argument(
        '--sync',
        action='store_true',
        help='Sync pending suggestions once and exit'
    )
    parser.add_argument(
        '--stats',
        action='store_true',
        help='Show sync statistics and exit'
    )
    parser.add_argument(
        '--daemon',
        action='store_true',
        help='Run as continuous daemon (for testing)'
    )
    parser.add_argument(
        '--reset',
        action='store_true',
        help='Reset all synced suggestions to pending (re-sync)'
    )

    args = parser.parse_args()

    # Load environment
    load_dotenv()
    MONGODB_URI = os.getenv('MONGODB_URI')
    if not MONGODB_URI:
        logger.error("MONGODB_URI environment variable not set")
        sys.exit(1)

    # Connect to MongoDB
    client = MongoClient(MONGODB_URI)
    db = client.noldo

    # Create worker
    worker = SuggestionsWorker(db)

    if args.stats:
        # Show statistics
        stats = worker.get_stats()
        print("\n=== Suggestions Worker Statistics ===")
        print(f"Running: {stats['running']}")
        print(f"Total synced: {stats['synced_total']}")
        print(f"Total skipped: {stats['skipped_total']}")
        print(f"Last run: {stats['last_run']}")
        print(f"\nPending documents: {stats['pending_documents']}")
        print(f"Pending subreddits ({stats['pending_count']}):")
        for sub in stats['pending_subreddits']:
            print(f"  - r/{sub}")
        print(f"\nActive scraper: {stats['active_scraper']}")
        print(f"Check interval: {stats['config']['check_interval']}s")

    elif args.reset:
        # Reset synced suggestions
        collection_name = SUGGESTIONS_SYNC_CONFIG.get("collection_name", "subreddit_suggestions")
        result = db[collection_name].update_many(
            {"synced_at": {"$exists": True}},
            {"$unset": {"synced_at": "", "synced_to_scraper": ""}}
        )
        print(f"Reset {result.modified_count} suggestions to pending")

    elif args.sync:
        # Sync once
        print("Syncing pending suggestions...")
        result = worker.sync_suggestions()
        print(f"\nResult: {result}")

    elif args.daemon:
        # Run as daemon
        print("Starting suggestions worker daemon (Ctrl+C to stop)...")
        worker.start_background()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
            worker.stop()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
