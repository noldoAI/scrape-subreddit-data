#!/usr/bin/env python3
"""
Semantic Subreddit Search Engine

Search for subreddits by semantic meaning rather than keywords.
Uses nomic-embed-text-v2 embeddings and MongoDB Atlas Vector Search.

Usage:
    python semantic_search_subreddits.py --query "building b2b saas"
    python semantic_search_subreddits.py --query "cryptocurrency trading" --limit 20
    python semantic_search_subreddits.py --query "indie game dev" --min-subscribers 10000
"""

import os
import sys
import argparse
import logging
from typing import List, Dict, Optional
from dotenv import load_dotenv
from pymongo import MongoClient

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('semantic-search')

# Load environment variables
load_dotenv()

# MongoDB connection
MONGODB_URI = os.getenv('MONGODB_URI')
if not MONGODB_URI:
    logger.error("MONGODB_URI environment variable not set")
    sys.exit(1)

client = MongoClient(MONGODB_URI)
db = client.noldo
collection = db.subreddit_discovery

# Load embedding model
try:
    logger.info("Loading nomic-embed-text-v2 model...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer('nomic-ai/nomic-embed-text-v2', trust_remote_code=True)
    logger.info("✅ Model loaded\n")
except Exception as e:
    logger.error(f"Failed to load model: {e}")
    logger.error("Run: pip install sentence-transformers")
    sys.exit(1)


def search_subreddits(
    query: str,
    limit: int = 10,
    min_subscribers: Optional[int] = 1000,
    max_subscribers: Optional[int] = None,
    exclude_nsfw: bool = True,
    language: Optional[str] = None,
    subreddit_type: str = "public",
    num_candidates: int = 100
) -> List[Dict]:
    """
    Semantic search for subreddits using natural language queries.

    Args:
        query: Natural language search query (e.g., "building b2b saas")
        limit: Number of results to return
        min_subscribers: Minimum subscriber count (None = no filter)
        max_subscribers: Maximum subscriber count (None = no filter)
        exclude_nsfw: Filter out NSFW subreddits
        language: Language filter (e.g., "en")
        subreddit_type: Filter by type (public/private/restricted)
        num_candidates: Number of candidates to consider (higher = more accurate but slower)

    Returns:
        List of matching subreddit dictionaries with similarity scores
    """
    logger.info(f"🔍 Searching for: '{query}'")
    logger.info(f"   Filters: min_subs={min_subscribers}, exclude_nsfw={exclude_nsfw}, type={subreddit_type}\n")

    # Generate query embedding
    query_embedding = model.encode(query, convert_to_numpy=True).tolist()

    # Build MongoDB filters
    filters = {}

    if subreddit_type:
        filters["subreddit_type"] = subreddit_type

    if exclude_nsfw:
        filters["over_18"] = False

    if language:
        filters["lang"] = language

    # Subscriber filters (MongoDB doesn't support range in $vectorSearch filter directly)
    # So we'll apply them in post-filtering if needed
    subscriber_filter = {}
    if min_subscribers is not None:
        subscriber_filter["$gte"] = min_subscribers
    if max_subscribers is not None:
        subscriber_filter["$lte"] = max_subscribers

    if subscriber_filter:
        filters["subscribers"] = subscriber_filter

    # Execute vector search
    try:
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "subreddit_vector_index",
                    "path": "embeddings.combined_embedding",
                    "queryVector": query_embedding,
                    "numCandidates": num_candidates,
                    "limit": limit,
                    "filter": {k: v for k, v in filters.items() if k != "subscribers"}  # Exclude range filters
                }
            },
            {
                "$project": {
                    "subreddit_name": 1,
                    "title": 1,
                    "public_description": 1,
                    "subscribers": 1,
                    "active_user_count": 1,
                    "advertiser_category": 1,
                    "over_18": 1,
                    "subreddit_type": 1,
                    "lang": 1,
                    "url": 1,
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]

        # Add subscriber filter as a $match stage if needed
        if subscriber_filter:
            pipeline.insert(1, {"$match": {"subscribers": subscriber_filter}})

        results = list(collection.aggregate(pipeline))

        logger.info(f"✅ Found {len(results)} results\n")
        return results

    except Exception as e:
        logger.error(f"❌ Search failed: {e}")
        logger.error(f"\n💡 Troubleshooting:")
        logger.error(f"   1. Ensure vector index exists: python setup_vector_index.py --verify-only")
        logger.error(f"   2. Check embeddings exist: python generate_embeddings.py --stats")
        logger.error(f"   3. Verify MongoDB Atlas version supports vector search")
        return []


def print_results(results: List[Dict], detailed: bool = False):
    """
    Pretty print search results.

    Args:
        results: List of search results
        detailed: Show detailed information
    """
    if not results:
        logger.warning("No results found. Try:")
        logger.warning("  1. Different query terms")
        logger.warning("  2. Relaxing filters (--min-subscribers 0)")
        logger.warning("  3. Checking if subreddits exist: python generate_embeddings.py --stats")
        return

    print("\n" + "="*80)
    print(f"SEARCH RESULTS ({len(results)} subreddits)")
    print("="*80 + "\n")

    for i, sub in enumerate(results, 1):
        # Header
        print(f"{i}. r/{sub['subreddit_name']}")
        print(f"   {'─'*70}")

        # Similarity score
        score = sub.get('score', 0)
        score_bar = "█" * int(score * 20) + "░" * (20 - int(score * 20))
        print(f"   Relevance: {score_bar} {score:.3f}")

        # Metadata
        subs = sub.get('subscribers', 0)
        active = sub.get('active_user_count', 0)
        category = sub.get('advertiser_category', 'N/A')

        print(f"   Subscribers: {subs:,}")
        if active:
            print(f"   Active users: {active:,}")
        print(f"   Category: {category}")

        # Title
        if sub.get('title'):
            print(f"\n   \"{sub['title']}\"")

        # Description
        if sub.get('public_description'):
            desc = sub['public_description'][:200]
            if len(sub['public_description']) > 200:
                desc += "..."
            print(f"\n   {desc}")

        # URL
        print(f"\n   🔗 https://reddit.com{sub.get('url', '')}")

        # Detailed info
        if detailed:
            print(f"\n   Type: {sub.get('subreddit_type', 'unknown')}")
            print(f"   NSFW: {sub.get('over_18', False)}")
            print(f"   Language: {sub.get('lang', 'unknown')}")

        print()


def interactive_search():
    """Interactive search mode - keep asking for queries."""
    print("\n" + "="*80)
    print("INTERACTIVE SEMANTIC SUBREDDIT SEARCH")
    print("="*80)
    print("\nEnter your search queries (or 'quit' to exit)")
    print("Examples:")
    print("  - building b2b saas")
    print("  - cryptocurrency trading strategies")
    print("  - indie game development tips")
    print("  - machine learning projects\n")

    while True:
        try:
            query = input("🔍 Search: ").strip()

            if query.lower() in ['quit', 'exit', 'q']:
                print("\n👋 Goodbye!")
                break

            if not query:
                continue

            results = search_subreddits(query, limit=10)
            print_results(results, detailed=False)

        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}\n")


def main():
    parser = argparse.ArgumentParser(
        description='Semantic search for subreddits using natural language queries',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python semantic_search_subreddits.py --query "building b2b saas"
  python semantic_search_subreddits.py --query "crypto trading" --limit 20 --min-subscribers 10000
  python semantic_search_subreddits.py --interactive
        """
    )
    parser.add_argument(
        '--query',
        type=str,
        help='Search query (e.g., "building b2b saas")'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=10,
        help='Number of results (default: 10)'
    )
    parser.add_argument(
        '--min-subscribers',
        type=int,
        default=1000,
        help='Minimum subscriber count (default: 1000, use 0 for no filter)'
    )
    parser.add_argument(
        '--max-subscribers',
        type=int,
        help='Maximum subscriber count (default: no limit)'
    )
    parser.add_argument(
        '--include-nsfw',
        action='store_true',
        help='Include NSFW subreddits (default: excluded)'
    )
    parser.add_argument(
        '--language',
        type=str,
        help='Language filter (e.g., "en")'
    )
    parser.add_argument(
        '--type',
        type=str,
        default='public',
        choices=['public', 'private', 'restricted', 'all'],
        help='Subreddit type filter (default: public)'
    )
    parser.add_argument(
        '--detailed',
        action='store_true',
        help='Show detailed information'
    )
    parser.add_argument(
        '--interactive',
        action='store_true',
        help='Interactive search mode'
    )

    args = parser.parse_args()

    # Interactive mode
    if args.interactive:
        interactive_search()
        return

    # Single query mode
    if not args.query:
        parser.error("--query required (or use --interactive)")

    # Convert args
    min_subs = None if args.min_subscribers == 0 else args.min_subscribers
    subreddit_type = None if args.type == 'all' else args.type

    # Search
    results = search_subreddits(
        query=args.query,
        limit=args.limit,
        min_subscribers=min_subs,
        max_subscribers=args.max_subscribers,
        exclude_nsfw=not args.include_nsfw,
        language=args.language,
        subreddit_type=subreddit_type
    )

    # Display results
    print_results(results, detailed=args.detailed)


if __name__ == "__main__":
    main()
