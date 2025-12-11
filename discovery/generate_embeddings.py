#!/usr/bin/env python3
"""
Subreddit Embedding Generation Script

Generates semantic embeddings for subreddits using Azure OpenAI text-embedding-3-small.
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
from typing import List, Dict, Optional
from dotenv import load_dotenv
from pymongo import MongoClient

# Add parent directory to path for imports when run from discovery/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import EMBEDDING_CONFIG, AZURE_OPENAI_CONFIG

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

# Collection references - will be set based on --collection flag
COLLECTIONS = {
    'discovery': db.subreddit_discovery,
    'metadata': db.subreddit_metadata
}

# Initialize Azure OpenAI client
azure_client = None
try:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")

    if not endpoint or not api_key:
        logger.error("Azure OpenAI not configured")
        logger.error("Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY environment variables")
        sys.exit(1)

    from openai import AzureOpenAI
    azure_client = AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=AZURE_OPENAI_CONFIG.get("api_version", "2024-02-01")
    )
    logger.info("✅ Azure OpenAI client initialized")
    logger.info(f"   Model: {EMBEDDING_CONFIG['model_name']}")
    logger.info(f"   Dimensions: {EMBEDDING_CONFIG['dimensions']}")
except ImportError:
    logger.error("openai package not installed. Run: pip install openai")
    sys.exit(1)
except Exception as e:
    logger.error(f"Failed to initialize Azure OpenAI client: {e}")
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


def combine_text_for_persona_embedding(subreddit_doc: Dict) -> str:
    """
    Combine text fields optimized for persona-based search.

    Prioritizes LLM-enriched audience signals over raw topic data.
    Structure: audience signals first, then topic context.

    Args:
        subreddit_doc: MongoDB document with subreddit metadata and llm_enrichment

    Returns:
        Combined text string optimized for persona matching
    """
    text_parts = []

    # SECTION 1: LLM-ENRICHED AUDIENCE SIGNALS (highest priority)
    enrichment = subreddit_doc.get('llm_enrichment', {})

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
    if subreddit_doc.get('title'):
        text_parts.append(f"Subreddit: {subreddit_doc['title']}")

    if subreddit_doc.get('public_description'):
        desc = subreddit_doc['public_description'][:300].replace('\n', ' ').strip()
        if desc:
            text_parts.append(f"About: {desc}")

    if subreddit_doc.get('sample_posts_titles'):
        titles = subreddit_doc['sample_posts_titles'][:500]
        if titles:
            text_parts.append(f"Topics: {titles}")

    if subreddit_doc.get('advertiser_category'):
        text_parts.append(f"Category: {subreddit_doc['advertiser_category']}")

    combined = "\n".join(text_parts)

    if not combined.strip():
        logger.warning(f"No text content for r/{subreddit_doc.get('subreddit_name', 'unknown')}")
        return f"Subreddit: {subreddit_doc.get('subreddit_name', 'unknown')}"

    return combined


def generate_subreddit_embedding(subreddit_doc: Dict, embedding_type: str = "combined") -> Optional[List[float]]:
    """
    Generate embedding vector for a subreddit using Azure OpenAI.

    Args:
        subreddit_doc: MongoDB document with subreddit metadata
        embedding_type: "combined" (topic-focused) or "persona" (audience-focused)

    Returns:
        1536-dimensional embedding vector (or None on failure)
    """
    if embedding_type == "persona":
        combined_text = combine_text_for_persona_embedding(subreddit_doc)
    else:
        combined_text = combine_text_fields(subreddit_doc)

    # Get deployment name from config or env
    deployment = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", AZURE_OPENAI_CONFIG.get("embedding_deployment", "text-embedding-3-small"))

    # Generate embedding via Azure OpenAI
    response = azure_client.embeddings.create(
        input=combined_text,
        model=deployment
    )

    if not response.data or len(response.data) == 0:
        raise ValueError("Azure OpenAI returned empty embedding response")

    return response.data[0].embedding


def batch_generate_embeddings(
    batch_size: int = 32,
    force: bool = False,
    subreddit: str = None,
    embedding_type: str = "combined",
    collection_name: str = "metadata"
):
    """
    Generate embeddings for all subreddits in the database.

    Args:
        batch_size: Number of subreddits to process in parallel
        force: Regenerate embeddings even if they already exist
        subreddit: Process only specific subreddit (optional)
        embedding_type: "combined" (topic-focused) or "persona" (audience-focused)
        collection_name: "metadata" or "discovery" (default: metadata)
    """
    collection = COLLECTIONS.get(collection_name, COLLECTIONS['metadata'])

    # Determine embedding field name
    embedding_field = "embeddings.persona_embedding" if embedding_type == "persona" else "embeddings.combined_embedding"

    # Build query
    query = {}
    if not force:
        query[embedding_field] = {"$exists": False}
    if subreddit:
        query["subreddit_name"] = subreddit

    # For persona embeddings, only process subreddits with LLM enrichment
    if embedding_type == "persona" and not subreddit:
        query["llm_enrichment"] = {"$exists": True, "$ne": None}

    # Get subreddits needing embeddings
    subreddits = list(collection.find(query))

    if not subreddits:
        if subreddit:
            logger.warning(f"Subreddit r/{subreddit} not found or already has embeddings (use --force to regenerate)")
        elif embedding_type == "persona":
            logger.info("✅ All enriched subreddits already have persona embeddings (use --force to regenerate)")
            logger.info("   Run 'python enrich_existing.py' first to add LLM enrichment data")
        else:
            logger.info("✅ All subreddits already have embeddings (use --force to regenerate)")
        return

    logger.info(f"\n📊 Generating {embedding_type.upper()} embeddings for {len(subreddits)} subreddits")
    logger.info(f"   Batch size: {batch_size}")
    logger.info(f"   Model: {EMBEDDING_CONFIG['model_name']} ({EMBEDDING_CONFIG['dimensions']} dimensions)")
    logger.info(f"   Embedding type: {embedding_type}")
    logger.info(f"   Estimated time: ~{len(subreddits) * 1 // 60} minutes\n")

    successful = 0
    failed = 0

    for i, sub in enumerate(subreddits, 1):
        subreddit_name = sub.get('subreddit_name', 'unknown')

        try:
            logger.info(f"[{i}/{len(subreddits)}] Processing r/{subreddit_name}")

            # Generate embedding with specified type
            embedding = generate_subreddit_embedding(sub, embedding_type)

            # Store embedding in database (embedding is already a list from Azure OpenAI)
            update_fields = {
                embedding_field: embedding,
                "embeddings.generated_at": datetime.now(UTC),
                "embeddings.model": EMBEDDING_CONFIG["model_name"],
                "embeddings.dimensions": EMBEDDING_CONFIG["dimensions"],
            }

            # Track embedding type
            if embedding_type == "persona":
                update_fields["embeddings.persona_generated_at"] = datetime.now(UTC)

            collection.update_one(
                {"_id": sub["_id"]},
                {"$set": update_fields}
            )

            successful += 1
            logger.info(f"  ✓ {embedding_type.capitalize()} embedding saved ({len(embedding)} dimensions)")

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
    logger.info(f"   Database: {db.name}.{collection.name}")
    logger.info(f"{'='*80}\n")

    # Next steps
    if successful > 0:
        logger.info("🎯 Next steps:")
        logger.info("   1. Create vector search index: python setup_vector_index.py")
        logger.info("   2. Test semantic search: python semantic_search_subreddits.py --query 'building b2b saas'")


def show_statistics(collection_name: str = "metadata"):
    """Show statistics about embeddings in the database."""
    collection = COLLECTIONS.get(collection_name, COLLECTIONS['metadata'])

    total = collection.count_documents({})
    with_combined = collection.count_documents(
        {"embeddings.combined_embedding": {"$exists": True}}
    )
    with_persona = collection.count_documents(
        {"embeddings.persona_embedding": {"$exists": True}}
    )
    with_enrichment = collection.count_documents(
        {"llm_enrichment": {"$exists": True, "$ne": None}}
    )

    logger.info(f"\n📊 Embedding Statistics ({collection.name})")
    logger.info(f"   Total subreddits: {total}")
    if total > 0:
        logger.info(f"   With LLM enrichment: {with_enrichment} ({with_enrichment/total*100:.1f}%)")
        logger.info(f"   With combined embedding: {with_combined} ({with_combined/total*100:.1f}%)")
        logger.info(f"   With persona embedding: {with_persona} ({with_persona/total*100:.1f}%)")

    if with_combined > 0:
        # Sample document to check dimensions
        sample = collection.find_one(
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
    parser.add_argument(
        '--embedding-type',
        type=str,
        choices=['combined', 'persona'],
        default='combined',
        help='Embedding type: "combined" (topic-focused) or "persona" (audience-focused, requires LLM enrichment)'
    )
    parser.add_argument(
        '--collection',
        type=str,
        choices=['discovery', 'metadata'],
        default='metadata',
        help='Which collection to process (default: metadata)'
    )

    args = parser.parse_args()

    if args.stats:
        show_statistics(args.collection)
    else:
        batch_generate_embeddings(
            batch_size=args.batch_size,
            force=args.force,
            subreddit=args.subreddit,
            embedding_type=args.embedding_type,
            collection_name=args.collection
        )


if __name__ == "__main__":
    main()
