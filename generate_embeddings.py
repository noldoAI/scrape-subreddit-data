#!/usr/bin/env python3
"""
Subreddit Embedding Generation Script

Generates semantic embeddings for subreddits using nomic-embed-text-v2 model.
Embeddings enable semantic search to find relevant subreddits by meaning rather than keywords.

Usage:
    python generate_embeddings.py --batch-size 32
    python generate_embeddings.py --subreddit "SaaS" --force
"""

import os
import sys
import argparse
import logging
from datetime import datetime, UTC
from typing import List, Dict
from dotenv import load_dotenv
from pymongo import MongoClient
import numpy as np

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('embedding-generator')

# Load environment variables
load_dotenv()

# MongoDB connection
MONGODB_URI = os.getenv('MONGODB_URI')
if not MONGODB_URI:
    logger.error("MONGODB_URI environment variable not set")
    sys.exit(1)

client = MongoClient(MONGODB_URI)
db = client.noldo
subreddit_discovery_collection = db.subreddit_discovery

# Load sentence-transformers model
try:
    logger.info("Loading nomic-embed-text-v2 model...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer('nomic-ai/nomic-embed-text-v2', trust_remote_code=True)
    logger.info("✅ Model loaded successfully")
    logger.info(f"   Model: nomic-embed-text-v2")
    logger.info(f"   Dimensions: 768")
    logger.info(f"   Context window: 8192 tokens")
except Exception as e:
    logger.error(f"Failed to load model: {e}")
    logger.error("Run: pip install sentence-transformers")
    sys.exit(1)


def combine_text_fields(subreddit_doc: Dict) -> str:
    """
    Combine all relevant text fields into a single rich text for embedding.

    Includes: title, descriptions, guidelines, rules, and sample post titles.

    Args:
        subreddit_doc: MongoDB document with subreddit metadata

    Returns:
        Combined text string optimized for semantic embedding
    """
    text_parts = []

    # Title (high importance)
    if subreddit_doc.get('title'):
        text_parts.append(f"Title: {subreddit_doc['title']}")

    # Public description (search-optimized)
    if subreddit_doc.get('public_description'):
        text_parts.append(f"Description: {subreddit_doc['public_description']}")

    # Full description (first 500 chars for context)
    if subreddit_doc.get('description'):
        desc = subreddit_doc['description'][:500].replace('\n', ' ').strip()
        if desc:
            text_parts.append(f"About: {desc}")

    # Post guidelines (detailed topic context)
    if subreddit_doc.get('guidelines_text'):
        guidelines = subreddit_doc['guidelines_text'][:500].replace('\n', ' ').strip()
        if guidelines:
            text_parts.append(f"Guidelines: {guidelines}")

    # Rules (community focus indicators)
    if subreddit_doc.get('rules_text'):
        text_parts.append(f"Rules: {subreddit_doc['rules_text']}")

    # Sample post titles (real discussion topics - first 1000 chars)
    if subreddit_doc.get('sample_posts_titles'):
        sample_titles = subreddit_doc['sample_posts_titles'][:1000]
        if sample_titles:
            text_parts.append(f"Topics: {sample_titles}")

    # Advertiser category (topic hint)
    if subreddit_doc.get('advertiser_category'):
        text_parts.append(f"Category: {subreddit_doc['advertiser_category']}")

    combined = "\n".join(text_parts)

    if not combined.strip():
        logger.warning(f"No text content for r/{subreddit_doc.get('subreddit_name', 'unknown')}")
        return f"Subreddit: {subreddit_doc.get('subreddit_name', 'unknown')}"

    return combined


def generate_subreddit_embedding(subreddit_doc: Dict) -> np.ndarray:
    """
    Generate embedding vector for a subreddit.

    Args:
        subreddit_doc: MongoDB document with subreddit metadata

    Returns:
        768-dimensional embedding vector
    """
    combined_text = combine_text_fields(subreddit_doc)

    # Generate embedding
    embedding = model.encode(combined_text, convert_to_numpy=True)

    return embedding


def batch_generate_embeddings(batch_size: int = 32, force: bool = False, subreddit: str = None):
    """
    Generate embeddings for all subreddits in the database.

    Args:
        batch_size: Number of subreddits to process in parallel
        force: Regenerate embeddings even if they already exist
        subreddit: Process only specific subreddit (optional)
    """
    # Build query
    query = {}
    if not force:
        query["embeddings.combined_embedding"] = {"$exists": False}
    if subreddit:
        query["subreddit_name"] = subreddit

    # Get subreddits needing embeddings
    subreddits = list(subreddit_discovery_collection.find(query))

    if not subreddits:
        if subreddit:
            logger.warning(f"Subreddit r/{subreddit} not found or already has embeddings (use --force to regenerate)")
        else:
            logger.info("✅ All subreddits already have embeddings (use --force to regenerate)")
        return

    logger.info(f"\n📊 Generating embeddings for {len(subreddits)} subreddits")
    logger.info(f"   Batch size: {batch_size}")
    logger.info(f"   Model: nomic-embed-text-v2 (768 dimensions)")
    logger.info(f"   Estimated time: ~{len(subreddits) * 2 // 60} minutes\n")

    successful = 0
    failed = 0

    for i, sub in enumerate(subreddits, 1):
        subreddit_name = sub.get('subreddit_name', 'unknown')

        try:
            logger.info(f"[{i}/{len(subreddits)}] Processing r/{subreddit_name}")

            # Generate embedding
            embedding = generate_subreddit_embedding(sub)

            # Store embedding in database
            subreddit_discovery_collection.update_one(
                {"_id": sub["_id"]},
                {"$set": {
                    "embeddings.combined_embedding": embedding.tolist(),
                    "embeddings.generated_at": datetime.now(UTC),
                    "embeddings.model": "nomic-embed-text-v2",
                    "embeddings.dimensions": 768,
                    "embeddings.context_window": 8192
                }}
            )

            successful += 1
            logger.info(f"  ✓ Embedding generated and saved ({embedding.shape[0]} dimensions)")

            # Progress update every 10 subreddits
            if i % 10 == 0:
                logger.info(f"\n📈 Progress: {i}/{len(subreddits)} ({i/len(subreddits)*100:.1f}%)\n")

        except Exception as e:
            failed += 1
            logger.error(f"  ✗ Error processing r/{subreddit_name}: {e}")

    # Summary
    logger.info(f"\n{'='*80}")
    logger.info(f"✅ Embedding generation complete!")
    logger.info(f"   Successful: {successful}/{len(subreddits)}")
    if failed > 0:
        logger.info(f"   Failed: {failed}/{len(subreddits)}")
    logger.info(f"   Database: {db.name}.{subreddit_discovery_collection.name}")
    logger.info(f"{'='*80}\n")

    # Next steps
    if successful > 0:
        logger.info("🎯 Next steps:")
        logger.info("   1. Create vector search index: python setup_vector_index.py")
        logger.info("   2. Test semantic search: python semantic_search_subreddits.py --query 'building b2b saas'")


def show_statistics():
    """Show statistics about embeddings in the database."""
    total = subreddit_discovery_collection.count_documents({})
    with_embeddings = subreddit_discovery_collection.count_documents(
        {"embeddings.combined_embedding": {"$exists": True}}
    )
    without_embeddings = total - with_embeddings

    logger.info(f"\n📊 Embedding Statistics")
    logger.info(f"   Total subreddits: {total}")
    logger.info(f"   With embeddings: {with_embeddings} ({with_embeddings/total*100:.1f}%)")
    logger.info(f"   Without embeddings: {without_embeddings} ({without_embeddings/total*100:.1f}%)")

    if with_embeddings > 0:
        # Sample document to check dimensions
        sample = subreddit_discovery_collection.find_one(
            {"embeddings.combined_embedding": {"$exists": True}}
        )
        if sample and 'embeddings' in sample:
            dims = len(sample['embeddings']['combined_embedding'])
            model_name = sample['embeddings'].get('model', 'unknown')
            logger.info(f"   Embedding dimensions: {dims}")
            logger.info(f"   Model: {model_name}")


def main():
    parser = argparse.ArgumentParser(
        description='Generate semantic embeddings for subreddit discovery data'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=32,
        help='Batch size for processing (default: 32)'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Regenerate embeddings even if they already exist'
    )
    parser.add_argument(
        '--subreddit',
        type=str,
        help='Process only specific subreddit (e.g., "SaaS")'
    )
    parser.add_argument(
        '--stats',
        action='store_true',
        help='Show embedding statistics and exit'
    )

    args = parser.parse_args()

    if args.stats:
        show_statistics()
    else:
        batch_generate_embeddings(
            batch_size=args.batch_size,
            force=args.force,
            subreddit=args.subreddit
        )


if __name__ == "__main__":
    main()
