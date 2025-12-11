#!/usr/bin/env python3
"""
Background Embedding Worker

Processes subreddit metadata that has embedding_status: "pending" and generates:
1. Combined embeddings (topic-focused) using nomic-embed-text-v2
2. LLM enrichment (audience profiles) using Azure GPT-4o-mini
3. Persona embeddings (audience-focused) using nomic-embed-text-v2

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

from config import EMBEDDING_CONFIG, EMBEDDING_WORKER_CONFIG, COLLECTIONS, AZURE_OPENAI_CONFIG

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('embedding-worker')

# Thread-safe singleton for Azure OpenAI embedding client
_embedding_client = None
_embedding_client_lock = threading.Lock()
_embedding_client_load_attempted = False

# Thread-safe singleton for LLM enricher
_enricher = None
_enricher_lock = threading.Lock()
_enricher_load_attempted = False


def get_embedding_client():
    """
    Lazy load the Azure OpenAI embedding client (thread-safe singleton).

    Returns client on success, None on failure (graceful degradation).
    """
    global _embedding_client, _embedding_client_load_attempted

    if _embedding_client is not None:
        return _embedding_client

    with _embedding_client_lock:
        # Double-check after acquiring lock
        if _embedding_client is not None:
            return _embedding_client

        if _embedding_client_load_attempted:
            # Already tried and failed
            return None

        _embedding_client_load_attempted = True

        try:
            endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
            api_key = os.getenv("AZURE_OPENAI_API_KEY")

            if not endpoint or not api_key:
                logger.warning("Azure OpenAI not configured - embedding generation disabled")
                logger.warning("Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY environment variables")
                return None

            from openai import AzureOpenAI
            _embedding_client = AzureOpenAI(
                azure_endpoint=endpoint,
                api_key=api_key,
                api_version=AZURE_OPENAI_CONFIG.get("api_version", "2024-02-01")
            )
            logger.info(f"Azure OpenAI embedding client initialized ({EMBEDDING_CONFIG['dimensions']} dimensions)")
            return _embedding_client
        except ImportError:
            logger.warning("openai package not installed, embedding generation disabled")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize Azure OpenAI client: {e}")
            return None


def get_llm_enricher():
    """
    Lazy load the LLM enricher (thread-safe singleton).

    Returns enricher on success, None on failure (graceful degradation).
    """
    global _enricher, _enricher_load_attempted

    if _enricher is not None:
        return _enricher

    with _enricher_lock:
        # Double-check after acquiring lock
        if _enricher is not None:
            return _enricher

        if _enricher_load_attempted:
            # Already tried and failed
            return None

        _enricher_load_attempted = True

        try:
            # Check if Azure OpenAI is configured
            endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
            api_key = os.getenv("AZURE_OPENAI_API_KEY")

            if not endpoint or not api_key:
                logger.warning("Azure OpenAI not configured - LLM enrichment disabled")
                return None

            # Import and initialize enricher
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'discovery'))
            from llm_enrichment import SubredditEnricher
            _enricher = SubredditEnricher()
            logger.info("LLM enricher loaded successfully")
            return _enricher
        except ImportError as e:
            logger.warning(f"LLM enrichment module not found: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to load LLM enricher: {e}")
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


def combine_text_for_persona_embedding(metadata: Dict) -> str:
    """
    Combine text fields optimized for persona-based search.

    Prioritizes LLM-enriched audience signals over raw topic data.
    Structure: audience signals first, then topic context.

    Args:
        metadata: Subreddit metadata document with llm_enrichment

    Returns:
        Combined text string optimized for persona matching
    """
    text_parts = []

    # SECTION 1: LLM-ENRICHED AUDIENCE SIGNALS (highest priority)
    enrichment = metadata.get('llm_enrichment', {})

    if enrichment.get('audience_profile'):
        text_parts.append(f"Audience: {enrichment['audience_profile']}")

    if enrichment.get('audience_types'):
        types_str = ', '.join(enrichment['audience_types'][:6])
        text_parts.append(f"User types: {types_str}")

    if enrichment.get('user_intents'):
        intents_str = ', '.join(enrichment['user_intents'][:6])
        text_parts.append(f"They come here to: {intents_str}")

    if enrichment.get('pain_points'):
        pains_str = ', '.join(enrichment['pain_points'][:6])
        text_parts.append(f"Pain points: {pains_str}")

    if enrichment.get('content_themes'):
        themes_str = ', '.join(enrichment['content_themes'][:6])
        text_parts.append(f"Content themes: {themes_str}")

    # SECTION 2: TOPIC CONTEXT (supporting information)
    if metadata.get('title'):
        text_parts.append(f"Subreddit: {metadata['title']}")

    if metadata.get('public_description'):
        desc = metadata['public_description'][:300].replace('\n', ' ').strip()
        if desc:
            text_parts.append(f"About: {desc}")

    if metadata.get('sample_posts_titles'):
        titles = metadata['sample_posts_titles'][:500]
        if titles:
            text_parts.append(f"Topics: {titles}")

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
            "enriched": 0,
            "persona_generated": 0,
            "last_run": None,
            "model_loaded": False,
            "enricher_loaded": False
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
        Generate embedding vector for a subreddit using Azure OpenAI.

        Args:
            metadata: Subreddit metadata document

        Returns:
            Tuple of (embedding_vector, error_message)
            - (embedding, None) on success
            - (None, error_message) on failure
        """
        client = get_embedding_client()
        if client is None:
            return None, "Azure OpenAI client not initialized (check AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY)"

        try:
            combined_text = combine_text_fields(metadata)
            if not combined_text or not combined_text.strip():
                return None, "No text content available for embedding"

            # Get deployment name from config or env
            deployment = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", AZURE_OPENAI_CONFIG.get("embedding_deployment", "text-embedding-3-small"))

            response = client.embeddings.create(
                input=combined_text,
                model=deployment
            )

            if not response.data or len(response.data) == 0:
                return None, "Azure OpenAI returned empty embedding response"

            embedding = response.data[0].embedding
            return embedding, None
        except Exception as e:
            error_msg = f"Embedding generation failed: {str(e)}"
            logger.error(f"Embedding generation failed for r/{metadata.get('subreddit_name')}: {e}")
            return None, error_msg

    def generate_persona_embedding(self, metadata: Dict) -> tuple[Optional[List[float]], Optional[str]]:
        """
        Generate persona-focused embedding vector for a subreddit using Azure OpenAI.

        Requires llm_enrichment data to be present.

        Args:
            metadata: Subreddit metadata document with llm_enrichment

        Returns:
            Tuple of (embedding_vector, error_message)
        """
        client = get_embedding_client()
        if client is None:
            return None, "Azure OpenAI client not initialized"

        if not metadata.get('llm_enrichment'):
            return None, "No LLM enrichment data available"

        try:
            combined_text = combine_text_for_persona_embedding(metadata)
            if not combined_text or not combined_text.strip():
                return None, "No text content available for persona embedding"

            # Get deployment name from config or env
            deployment = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", AZURE_OPENAI_CONFIG.get("embedding_deployment", "text-embedding-3-small"))

            response = client.embeddings.create(
                input=combined_text,
                model=deployment
            )

            if not response.data or len(response.data) == 0:
                return None, "Azure OpenAI returned empty embedding response"

            embedding = response.data[0].embedding
            return embedding, None
        except Exception as e:
            error_msg = f"Persona embedding generation failed: {str(e)}"
            logger.error(f"Persona embedding failed for r/{metadata.get('subreddit_name')}: {e}")
            return None, error_msg

    def run_llm_enrichment(self, metadata: Dict) -> tuple[Optional[Dict], Optional[str]]:
        """
        Run LLM enrichment to generate audience profile.

        Args:
            metadata: Subreddit metadata document

        Returns:
            Tuple of (enrichment_data, error_message)
        """
        enricher = get_llm_enricher()
        if enricher is None:
            return None, "LLM enricher not available (check Azure OpenAI config)"

        try:
            enrichment = enricher.enrich_subreddit(metadata)
            if enrichment:
                return enrichment, None
            else:
                return None, "Enrichment returned None"
        except Exception as e:
            error_msg = f"LLM enrichment failed: {str(e)}"
            logger.error(f"LLM enrichment failed for r/{metadata.get('subreddit_name')}: {e}")
            return None, error_msg

    def process_one(self, metadata: Dict) -> bool:
        """
        Process a single subreddit: combined embedding, LLM enrichment, persona embedding.

        Pipeline:
        1. Generate combined_embedding (if not exists)
        2. Run LLM enrichment (if not exists)
        3. Generate persona_embedding (if enrichment exists and persona not exists)

        Args:
            metadata: Subreddit metadata document

        Returns:
            True if all steps successful, False otherwise
        """
        subreddit_name = metadata.get("subreddit_name", "unknown")
        doc_id = metadata.get("_id")
        success = True

        try:
            logger.info(f"Processing r/{subreddit_name}...")

            # STEP 1: Combined embedding (topic-focused)
            has_combined = metadata.get('embeddings', {}).get('combined_embedding') is not None

            if not has_combined:
                logger.info(f"  [1/3] Generating combined embedding...")
                embedding, error_msg = self.generate_embedding(metadata)

                if embedding:
                    self.collection.update_one(
                        {"_id": doc_id},
                        {"$set": {
                            "embeddings.combined_embedding": embedding,
                            "embeddings.model": EMBEDDING_CONFIG["model_name"],
                            "embeddings.dimensions": EMBEDDING_CONFIG["dimensions"],
                            "embeddings.generated_at": datetime.now(UTC)
                        }}
                    )
                    logger.info(f"  ✓ Combined embedding saved")
                    self._stats["processed"] += 1
                else:
                    logger.warning(f"  ✗ Combined embedding failed: {error_msg}")
                    success = False
            else:
                logger.info(f"  [1/3] Combined embedding exists - skipped")

            # STEP 2: LLM enrichment (audience profile)
            has_enrichment = metadata.get('llm_enrichment') is not None

            if not has_enrichment:
                logger.info(f"  [2/3] Running LLM enrichment...")
                enrichment, error_msg = self.run_llm_enrichment(metadata)

                if enrichment:
                    self.collection.update_one(
                        {"_id": doc_id},
                        {"$set": {
                            "llm_enrichment": enrichment,
                            "llm_enrichment_at": datetime.now(UTC)
                        }}
                    )
                    # Update local metadata for persona embedding
                    metadata['llm_enrichment'] = enrichment
                    logger.info(f"  ✓ LLM enrichment saved")
                    self._stats["enriched"] += 1
                else:
                    logger.warning(f"  ✗ LLM enrichment failed: {error_msg}")
                    # Not marking as failure - enrichment is optional
            else:
                logger.info(f"  [2/3] LLM enrichment exists - skipped")

            # STEP 3: Persona embedding (audience-focused)
            has_persona = metadata.get('embeddings', {}).get('persona_embedding') is not None
            has_enrichment_now = metadata.get('llm_enrichment') is not None

            if has_enrichment_now and not has_persona:
                logger.info(f"  [3/3] Generating persona embedding...")
                persona_embedding, error_msg = self.generate_persona_embedding(metadata)

                if persona_embedding:
                    self.collection.update_one(
                        {"_id": doc_id},
                        {"$set": {
                            "embeddings.persona_embedding": persona_embedding,
                            "embeddings.persona_generated_at": datetime.now(UTC)
                        }}
                    )
                    logger.info(f"  ✓ Persona embedding saved")
                    self._stats["persona_generated"] += 1
                else:
                    logger.warning(f"  ✗ Persona embedding failed: {error_msg}")
                    # Not marking as failure - persona is optional
            elif not has_enrichment_now:
                logger.info(f"  [3/3] No enrichment data - persona embedding skipped")
            else:
                logger.info(f"  [3/3] Persona embedding exists - skipped")

            # Mark as complete
            self.collection.update_one(
                {"_id": doc_id},
                {"$set": {
                    "embedding_status": "complete",
                    "embedding_completed_at": datetime.now(UTC)
                },
                "$unset": {
                    "embedding_error": "",
                    "embedding_retry_count": ""
                }}
            )

            logger.info(f"✓ Processing complete for r/{subreddit_name}")
            return success

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
            Dict with worker stats including enrichment and persona counts
        """
        client = get_embedding_client()
        enricher = get_llm_enricher()

        # Count documents with various embeddings/enrichments
        total_docs = self.collection.count_documents({})
        with_combined = self.collection.count_documents({
            "embeddings.combined_embedding": {"$exists": True}
        })
        with_enrichment = self.collection.count_documents({
            "llm_enrichment": {"$exists": True, "$ne": None}
        })
        with_persona = self.collection.count_documents({
            "embeddings.persona_embedding": {"$exists": True}
        })

        return {
            "running": self.running,
            "embedding_client_ready": client is not None,
            "enricher_loaded": enricher is not None,
            "total_processed": self._stats["processed"],
            "total_enriched": self._stats["enriched"],
            "total_persona_generated": self._stats["persona_generated"],
            "total_failed": self._stats["failed"],
            "last_run": self._stats["last_run"].isoformat() if self._stats["last_run"] else None,
            "pending_count": self.get_pending_count(),
            "complete_count": self.collection.count_documents({"embedding_status": "complete"}),
            "database_stats": {
                "total_subreddits": total_docs,
                "with_combined_embedding": with_combined,
                "with_llm_enrichment": with_enrichment,
                "with_persona_embedding": with_persona
            },
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
        db_stats = stats.get('database_stats', {})
        total = db_stats.get('total_subreddits', 0)

        print("\n📊 Embedding Worker Statistics")
        print("=" * 60)
        print(f"Azure OpenAI client ready: {stats['embedding_client_ready']}")
        print(f"Enricher loaded:           {stats['enricher_loaded']}")
        print()
        print("Pipeline Status:")
        print(f"  Pending:  {stats['pending_count']}")
        print(f"  Failed:   {failed_count}")
        print(f"  Complete: {stats['complete_count']}")
        print()
        print("Database Coverage:")
        print(f"  Total subreddits:       {total}")
        print(f"  With combined embedding: {db_stats.get('with_combined_embedding', 0)} ({db_stats.get('with_combined_embedding', 0)/max(total,1)*100:.1f}%)")
        print(f"  With LLM enrichment:     {db_stats.get('with_llm_enrichment', 0)} ({db_stats.get('with_llm_enrichment', 0)/max(total,1)*100:.1f}%)")
        print(f"  With persona embedding:  {db_stats.get('with_persona_embedding', 0)} ({db_stats.get('with_persona_embedding', 0)/max(total,1)*100:.1f}%)")
        print()
        print("Session Stats:")
        print(f"  Combined processed:  {stats['total_processed']}")
        print(f"  LLM enriched:        {stats['total_enriched']}")
        print(f"  Persona generated:   {stats['total_persona_generated']}")
        print(f"  Failed:              {stats['total_failed']}")
        print("=" * 60)
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
