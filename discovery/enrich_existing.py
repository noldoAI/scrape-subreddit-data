#!/usr/bin/env python3
"""
Batch Enrichment Script for Existing Subreddits

Enriches subreddits in the database that don't have LLM-generated
audience profiles. Saves enrichment data to MongoDB.

Usage:
    python enrich_existing.py --batch-size 50
    python enrich_existing.py --subreddit SaaS
    python enrich_existing.py --stats
    python enrich_existing.py --collection discovery  # or 'metadata'
"""

import os
import sys
import argparse
import logging
from datetime import datetime, UTC
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient

from llm_enrichment import SubredditEnricher

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('enrich-existing')

# Load environment variables
load_dotenv()

# MongoDB connection
MONGODB_URI = os.getenv('MONGODB_URI')
if not MONGODB_URI:
    logger.error("MONGODB_URI environment variable not set")
    sys.exit(1)

client = MongoClient(MONGODB_URI)
db = client.noldo


def get_collection(collection_name: str):
    """Get the appropriate MongoDB collection."""
    if collection_name == 'discovery':
        return db.subreddit_discovery
    elif collection_name == 'metadata':
        return db.subreddit_metadata
    else:
        raise ValueError(f"Unknown collection: {collection_name}")


def get_unenriched_subreddits(collection, limit: int = 50):
    """
    Get subreddits that don't have LLM enrichment data.

    Args:
        collection: MongoDB collection
        limit: Maximum number to return

    Returns:
        List of subreddit documents
    """
    query = {
        "$or": [
            {"llm_enrichment": {"$exists": False}},
            {"llm_enrichment": None}
        ]
    }

    # Only get documents that have the basic fields needed for enrichment
    projection = {
        "subreddit_name": 1,
        "title": 1,
        "public_description": 1,
        "description": 1,
        "sample_posts_titles": 1,
        "sample_posts": 1,
        "rules_text": 1,
        "advertiser_category": 1,
        "subscribers": 1
    }

    cursor = collection.find(query, projection).limit(limit)
    return list(cursor)


def enrich_and_save(collection, enricher: SubredditEnricher, subreddit_doc: dict) -> bool:
    """
    Enrich a single subreddit and save to database.

    Args:
        collection: MongoDB collection
        enricher: SubredditEnricher instance
        subreddit_doc: Subreddit document from MongoDB

    Returns:
        True if successful, False otherwise
    """
    subreddit_name = subreddit_doc.get('subreddit_name', 'unknown')

    try:
        # Generate enrichment
        enrichment = enricher.enrich_subreddit(subreddit_doc)

        if enrichment:
            # Save to database
            result = collection.update_one(
                {"_id": subreddit_doc["_id"]},
                {
                    "$set": {
                        "llm_enrichment": enrichment,
                        "llm_enrichment_at": datetime.now(UTC)
                    }
                }
            )

            if result.modified_count > 0:
                logger.info(f"Saved enrichment for r/{subreddit_name}")
                return True
            else:
                logger.warning(f"No document modified for r/{subreddit_name}")
                return False
        else:
            logger.warning(f"No enrichment generated for r/{subreddit_name}")
            return False

    except Exception as e:
        logger.error(f"Error processing r/{subreddit_name}: {e}")
        return False


def batch_enrich(
    collection_name: str = 'discovery',
    batch_size: int = 50,
    delay: float = 0.5
):
    """
    Enrich a batch of subreddits without LLM data.

    Args:
        collection_name: 'discovery' or 'metadata'
        batch_size: Number of subreddits to process
        delay: Delay between API calls in seconds
    """
    import time

    collection = get_collection(collection_name)

    # Get unenriched subreddits
    subreddits = get_unenriched_subreddits(collection, batch_size)

    if not subreddits:
        logger.info(f"All subreddits in {collection_name} already have enrichment data!")
        return

    logger.info(f"\nEnriching {len(subreddits)} subreddits from {collection_name}...")
    logger.info("="*60)

    # Initialize enricher
    enricher = SubredditEnricher()

    successful = 0
    failed = 0

    for i, doc in enumerate(subreddits, 1):
        subreddit_name = doc.get('subreddit_name', 'unknown')
        subscribers = doc.get('subscribers', 0)

        logger.info(f"\n[{i}/{len(subreddits)}] r/{subreddit_name} ({subscribers:,} subscribers)")

        if enrich_and_save(collection, enricher, doc):
            successful += 1
        else:
            failed += 1

        # Rate limiting
        if i < len(subreddits):
            time.sleep(delay)

    # Summary
    logger.info("\n" + "="*60)
    logger.info("BATCH ENRICHMENT COMPLETE")
    logger.info("="*60)
    logger.info(f"Successful: {successful}/{len(subreddits)}")
    logger.info(f"Failed: {failed}/{len(subreddits)}")

    # Check remaining
    remaining = collection.count_documents({
        "$or": [
            {"llm_enrichment": {"$exists": False}},
            {"llm_enrichment": None}
        ]
    })
    logger.info(f"Remaining unenriched: {remaining}")


def enrich_single(collection_name: str, subreddit_name: str, force: bool = False):
    """
    Enrich a single subreddit by name.

    Args:
        collection_name: 'discovery' or 'metadata'
        subreddit_name: Name of the subreddit
        force: Re-enrich even if data exists
    """
    collection = get_collection(collection_name)

    # Find the subreddit
    query = {"subreddit_name": subreddit_name}
    if not force:
        query["$or"] = [
            {"llm_enrichment": {"$exists": False}},
            {"llm_enrichment": None}
        ]

    doc = collection.find_one({"subreddit_name": subreddit_name})

    if not doc:
        logger.error(f"Subreddit r/{subreddit_name} not found in {collection_name}")
        return

    if doc.get('llm_enrichment') and not force:
        logger.info(f"r/{subreddit_name} already has enrichment data (use --force to re-enrich)")
        return

    logger.info(f"Enriching r/{subreddit_name}...")

    enricher = SubredditEnricher()
    if enrich_and_save(collection, enricher, doc):
        logger.info("Done!")
    else:
        logger.error("Enrichment failed!")


def show_stats(collection_name: str):
    """Show enrichment statistics for a collection."""
    collection = get_collection(collection_name)

    total = collection.count_documents({})
    with_enrichment = collection.count_documents({
        "llm_enrichment": {"$exists": True, "$ne": None}
    })
    without_enrichment = total - with_enrichment

    print(f"\n{'='*60}")
    print(f"ENRICHMENT STATISTICS: {collection_name}")
    print(f"{'='*60}")
    print(f"Total subreddits: {total}")
    print(f"With enrichment: {with_enrichment} ({with_enrichment/total*100:.1f}%)" if total > 0 else "With enrichment: 0")
    print(f"Without enrichment: {without_enrichment} ({without_enrichment/total*100:.1f}%)" if total > 0 else "Without enrichment: 0")

    if with_enrichment > 0:
        # Sample enriched document
        sample = collection.find_one({"llm_enrichment": {"$exists": True, "$ne": None}})
        if sample and 'llm_enrichment' in sample:
            print(f"\nSample enrichment (r/{sample.get('subreddit_name', 'unknown')}):")
            print(f"  Audience: {sample['llm_enrichment'].get('audience_profile', 'N/A')[:80]}...")
            print(f"  Types: {sample['llm_enrichment'].get('audience_types', [])}")
            print(f"  Model: {sample['llm_enrichment'].get('model', 'N/A')}")


def main():
    parser = argparse.ArgumentParser(
        description='Enrich existing subreddits with LLM-generated audience profiles'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=50,
        help='Number of subreddits to process (default: 50)'
    )
    parser.add_argument(
        '--subreddit',
        type=str,
        help='Enrich a specific subreddit by name'
    )
    parser.add_argument(
        '--collection',
        type=str,
        choices=['discovery', 'metadata'],
        default='metadata',
        help='Which collection to enrich (default: metadata)'
    )
    parser.add_argument(
        '--stats',
        action='store_true',
        help='Show enrichment statistics and exit'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Re-enrich even if data already exists (for --subreddit)'
    )
    parser.add_argument(
        '--delay',
        type=float,
        default=0.5,
        help='Delay between API calls in seconds (default: 0.5)'
    )

    args = parser.parse_args()

    if args.stats:
        show_stats(args.collection)
        return

    if args.subreddit:
        enrich_single(args.collection, args.subreddit, args.force)
    else:
        batch_enrich(args.collection, args.batch_size, args.delay)


if __name__ == "__main__":
    main()
