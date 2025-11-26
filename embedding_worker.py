#!/usr/bin/env python3
"""
Background Embedding Worker

Processes subreddit metadata that has embedding_status: "pending" and generates
semantic embeddings using nomic-embed-text-v2 model.

This module can be:
1. Imported and run as a background thread in the API server
2. Run as a standalone script for manual processing

Usage:
    # As background thread (in api.py):
    from embedding_worker import EmbeddingWorker
    worker = EmbeddingWorker(db)
    worker.start_background()

    # As standalone script:
    python embedding_worker.py --process-all
    python embedding_worker.py --subreddit wallstreetbets
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

from config import EMBEDDING_CONFIG, EMBEDDING_WORKER_CONFIG, COLLECTIONS

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('embedding-worker')

# Thread-safe singleton for embedding model
_model = None
_model_lock = threading.Lock()
_model_load_attempted = False


def get_embedding_model():
    """
    Lazy load the embedding model (thread-safe singleton).

    Returns model on success, None on failure (graceful degradation).
    """
    global _model, _model_load_attempted

    if _model is not None:
        return _model

    with _model_lock:
        # Double-check after acquiring lock
        if _model is not None:
            return _model

        if _model_load_attempted:
            # Already tried and failed
            return None

        _model_load_attempted = True

        try:
            logger.info("Loading nomic-embed-text-v2 model (this may take 10-30 seconds)...")
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(
                EMBEDDING_CONFIG["model_name"],
                trust_remote_code=EMBEDDING_CONFIG.get("trust_remote_code", True)
            )
            logger.info(f"Embedding model loaded successfully ({EMBEDDING_CONFIG['dimensions']} dimensions)")
            return _model
        except ImportError:
            logger.warning("sentence-transformers not installed, embedding generation disabled")
            return None
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            return None


def combine_text_fields(metadata: Dict) -> str:
    """
    Combine all relevant text fields into a single rich text for embedding.

    Reuses the same logic as generate_embeddings.py for consistency.

    Args:
        metadata: Subreddit metadata document from MongoDB

    Returns:
        Combined text string optimized for semantic embedding
    """
    text_parts = []

    # Title (high importance)
    if metadata.get('title'):
        text_parts.append(f"Title: {metadata['title']}")

    # Public description (search-optimized)
    if metadata.get('public_description'):
        text_parts.append(f"Description: {metadata['public_description']}")

    # Full description (first 500 chars for context)
    if metadata.get('description'):
        desc = metadata['description'][:500].replace('\n', ' ').strip()
        if desc:
            text_parts.append(f"About: {desc}")

    # Post guidelines (detailed topic context)
    if metadata.get('guidelines_text'):
        guidelines = metadata['guidelines_text'][:500].replace('\n', ' ').strip()
        if guidelines:
            text_parts.append(f"Guidelines: {guidelines}")

    # Rules (community focus indicators)
    if metadata.get('rules_text'):
        text_parts.append(f"Rules: {metadata['rules_text']}")

    # Sample post titles (real discussion topics - first 1000 chars)
    if metadata.get('sample_posts_titles'):
        sample_titles = metadata['sample_posts_titles'][:1000]
        if sample_titles:
            text_parts.append(f"Topics: {sample_titles}")

    # Advertiser category (topic hint)
    if metadata.get('advertiser_category'):
        text_parts.append(f"Category: {metadata['advertiser_category']}")

    combined = "\n".join(text_parts)

    if not combined.strip():
        return f"Subreddit: {metadata.get('subreddit_name', 'unknown')}"

    return combined


class EmbeddingWorker:
    """
    Background worker that processes pending embeddings for subreddit metadata.

    Usage:
        worker = EmbeddingWorker(db)
        worker.start_background()  # Start as daemon thread
        # or
        worker.process_all_pending()  # Process all pending (blocking)
    """

    def __init__(self, db):
        """
        Initialize the embedding worker.

        Args:
            db: MongoDB database instance
        """
        self.db = db
        self.collection = db[COLLECTIONS["SUBREDDIT_METADATA"]]
        self.config = EMBEDDING_WORKER_CONFIG
        self.running = False
        self._thread = None
        self._stats = {
            "processed": 0,
            "failed": 0,
            "last_run": None,
            "model_loaded": False
        }

    def get_pending(self, limit: int = None) -> List[Dict]:
        """
        Get subreddit metadata documents with pending embeddings.

        Args:
            limit: Maximum number of documents to return (default: batch_size from config)

        Returns:
            List of metadata documents needing embeddings
        """
        limit = limit or self.config.get("batch_size", 10)

        # Query for pending or failed (with retry)
        query = {
            "$or": [
                {"embedding_status": "pending"},
                {
                    "embedding_status": "failed",
                    "embedding_retry_count": {"$lt": self.config.get("max_retries", 3)}
                }
            ]
        }

        # Sort by requested time (oldest first)
        cursor = self.collection.find(query).sort("embedding_requested_at", 1).limit(limit)
        return list(cursor)

    def generate_embedding(self, metadata: Dict) -> tuple[Optional[List[float]], Optional[str]]:
        """
        Generate embedding vector for a subreddit.

        Args:
            metadata: Subreddit metadata document

        Returns:
            Tuple of (embedding_vector, error_message)
            - (embedding, None) on success
            - (None, error_message) on failure
        """
        model = get_embedding_model()
        if model is None:
            return None, "Embedding model not loaded (check sentence-transformers installation)"

        try:
            combined_text = combine_text_fields(metadata)
            if not combined_text or not combined_text.strip():
                return None, "No text content available for embedding"

            embedding = model.encode(combined_text, convert_to_numpy=True)
            if embedding is None:
                return None, "Model encode() returned None"
            return embedding.tolist(), None
        except Exception as e:
            error_msg = f"Encoding failed: {str(e)}"
            logger.error(f"Embedding generation failed for r/{metadata.get('subreddit_name')}: {e}")
            return None, error_msg

    def process_one(self, metadata: Dict) -> bool:
        """
        Process a single subreddit's embedding.

        Args:
            metadata: Subreddit metadata document

        Returns:
            True if successful, False otherwise
        """
        subreddit_name = metadata.get("subreddit_name", "unknown")
        doc_id = metadata.get("_id")

        try:
            logger.info(f"Generating embedding for r/{subreddit_name}...")

            embedding, error_msg = self.generate_embedding(metadata)

            if embedding:
                # Success - save embedding and mark complete
                self.collection.update_one(
                    {"_id": doc_id},
                    {"$set": {
                        "embedding_status": "complete",
                        "embedding_completed_at": datetime.now(UTC),
                        "embeddings": {
                            "combined_embedding": embedding,
                            "model": EMBEDDING_CONFIG["model_name"],
                            "dimensions": EMBEDDING_CONFIG["dimensions"],
                            "generated_at": datetime.now(UTC)
                        }
                    },
                    "$unset": {
                        "embedding_error": "",
                        "embedding_retry_count": ""
                    }}
                )
                logger.info(f"✓ Embedding saved for r/{subreddit_name} ({EMBEDDING_CONFIG['dimensions']} dimensions)")
                self._stats["processed"] += 1
                return True
            else:
                # Failed - increment retry count with specific error
                retry_count = metadata.get("embedding_retry_count", 0) + 1
                self.collection.update_one(
                    {"_id": doc_id},
                    {"$set": {
                        "embedding_status": "failed",
                        "embedding_error": error_msg or "Unknown error",
                        "embedding_retry_count": retry_count
                    }}
                )
                logger.warning(f"✗ Embedding failed for r/{subreddit_name}: {error_msg} (retry {retry_count})")
                self._stats["failed"] += 1
                return False

        except Exception as e:
            # Error - mark as failed
            retry_count = metadata.get("embedding_retry_count", 0) + 1
            self.collection.update_one(
                {"_id": doc_id},
                {"$set": {
                    "embedding_status": "failed",
                    "embedding_error": str(e),
                    "embedding_retry_count": retry_count
                }}
            )
            logger.error(f"✗ Error processing r/{subreddit_name}: {e}")
            self._stats["failed"] += 1
            return False

    def process_batch(self) -> Dict:
        """
        Process a batch of pending embeddings.

        Returns:
            Dict with processing statistics
        """
        pending = self.get_pending()

        if not pending:
            return {"processed": 0, "failed": 0, "pending": 0}

        logger.info(f"Processing {len(pending)} pending embeddings...")

        processed = 0
        failed = 0

        for metadata in pending:
            if self.process_one(metadata):
                processed += 1
            else:
                failed += 1

            # Small delay between processing
            time.sleep(0.5)

        self._stats["last_run"] = datetime.now(UTC)

        return {
            "processed": processed,
            "failed": failed,
            "remaining": self.get_pending_count()
        }

    def process_all_pending(self) -> Dict:
        """
        Process all pending embeddings (blocking).

        Returns:
            Dict with total processing statistics
        """
        total_processed = 0
        total_failed = 0

        while True:
            result = self.process_batch()
            total_processed += result["processed"]
            total_failed += result["failed"]

            if result["processed"] == 0 and result["failed"] == 0:
                break

            # Brief pause between batches
            time.sleep(1)

        return {
            "total_processed": total_processed,
            "total_failed": total_failed
        }

    def get_pending_count(self) -> int:
        """Get count of documents with pending embeddings."""
        return self.collection.count_documents({
            "$or": [
                {"embedding_status": "pending"},
                {
                    "embedding_status": "failed",
                    "embedding_retry_count": {"$lt": self.config.get("max_retries", 3)}
                }
            ]
        })

    def get_stats(self) -> Dict:
        """
        Get worker statistics.

        Returns:
            Dict with worker stats
        """
        model = get_embedding_model()

        return {
            "running": self.running,
            "model_loaded": model is not None,
            "total_processed": self._stats["processed"],
            "total_failed": self._stats["failed"],
            "last_run": self._stats["last_run"].isoformat() if self._stats["last_run"] else None,
            "pending_count": self.get_pending_count(),
            "complete_count": self.collection.count_documents({"embedding_status": "complete"}),
            "config": {
                "check_interval": self.config.get("check_interval", 60),
                "batch_size": self.config.get("batch_size", 10)
            }
        }

    def _run_loop(self):
        """Internal method: continuous background processing loop."""
        logger.info(f"Embedding worker started (interval: {self.config.get('check_interval', 60)}s)")

        while self.running:
            try:
                # Check if there's pending work
                pending_count = self.get_pending_count()

                if pending_count > 0:
                    logger.info(f"Found {pending_count} pending embeddings")
                    self.process_batch()

                # Sleep until next check
                time.sleep(self.config.get("check_interval", 60))

            except Exception as e:
                logger.error(f"Error in embedding worker loop: {e}")
                time.sleep(30)  # Wait before retry

        logger.info("Embedding worker stopped")

    def start_background(self):
        """Start the worker as a background daemon thread."""
        if self.running:
            logger.warning("Worker already running")
            return

        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Embedding worker background thread started")

    def stop(self):
        """Stop the background worker."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Embedding worker stopped")


def main():
    """CLI interface for the embedding worker."""
    parser = argparse.ArgumentParser(
        description='Background worker for generating subreddit embeddings'
    )
    parser.add_argument(
        '--process-all',
        action='store_true',
        help='Process all pending embeddings and exit'
    )
    parser.add_argument(
        '--subreddit',
        type=str,
        help='Process specific subreddit only'
    )
    parser.add_argument(
        '--stats',
        action='store_true',
        help='Show embedding statistics and exit'
    )
    parser.add_argument(
        '--daemon',
        action='store_true',
        help='Run as continuous daemon (for testing)'
    )
    parser.add_argument(
        '--reset-failed',
        action='store_true',
        help='Reset all failed embeddings to pending for retry'
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
    worker = EmbeddingWorker(db)

    if args.stats:
        stats = worker.get_stats()
        failed_count = worker.collection.count_documents({"embedding_status": "failed"})
        print("\n📊 Embedding Worker Statistics")
        print("=" * 50)
        print(f"Model loaded: {stats['model_loaded']}")
        print(f"Pending: {stats['pending_count']}")
        print(f"Failed: {failed_count}")
        print(f"Complete: {stats['complete_count']}")
        print(f"Total processed: {stats['total_processed']}")
        print(f"Total failed: {stats['total_failed']}")
        print("=" * 50)
        return

    if args.reset_failed:
        # Reset all failed embeddings to pending
        result = worker.collection.update_many(
            {"embedding_status": "failed"},
            {
                "$set": {"embedding_status": "pending"},
                "$unset": {"embedding_error": "", "embedding_retry_count": ""}
            }
        )
        print(f"\n✅ Reset {result.modified_count} failed embeddings to pending")
        return

    if args.subreddit:
        # Process specific subreddit
        doc = worker.collection.find_one({"subreddit_name": args.subreddit})
        if not doc:
            logger.error(f"Subreddit r/{args.subreddit} not found in database")
            sys.exit(1)

        # Force pending status
        worker.collection.update_one(
            {"_id": doc["_id"]},
            {"$set": {"embedding_status": "pending"}}
        )
        doc["embedding_status"] = "pending"

        success = worker.process_one(doc)
        sys.exit(0 if success else 1)

    if args.process_all:
        # Process all pending
        result = worker.process_all_pending()
        print(f"\n✅ Processing complete!")
        print(f"   Processed: {result['total_processed']}")
        print(f"   Failed: {result['total_failed']}")
        return

    if args.daemon:
        # Run as daemon (for testing)
        worker.start_background()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            worker.stop()
        return

    # Default: show help
    parser.print_help()


if __name__ == "__main__":
    main()
