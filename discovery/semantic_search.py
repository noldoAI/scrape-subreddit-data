#!/usr/bin/env python3
"""
Semantic Subreddit Search Engine

Search for subreddits by semantic meaning rather than keywords.
Uses Azure OpenAI text-embedding-3-small and MongoDB Atlas Vector Search.

Usage:
    python discovery/semantic_search.py --query "building b2b saas"
    python discovery/semantic_search.py --query "cryptocurrency trading" --limit 20
    python discovery/semantic_search.py --query "indie game dev" --min-subscribers 10000
    python discovery/semantic_search.py --query "stocks" --source all  # Search both collections
"""

import os
import sys
import argparse
import logging
from typing import List, Dict, Optional
from dotenv import load_dotenv
from pymongo import MongoClient

# Add parent directory to path for imports when run from discovery/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DISCOVERY_CONFIG, EMBEDDING_WORKER_CONFIG, EMBEDDING_CONFIG, AZURE_OPENAI_CONFIG, COLLECTIONS

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

# Collection configurations
SEARCH_SOURCES = {
    "discovery": {
        "collection": db.subreddit_discovery,
        "index_name": DISCOVERY_CONFIG["vector_index_name"],
        "description": "Discovered subreddits"
    },
    "active": {
        "collection": db[COLLECTIONS["SUBREDDIT_METADATA"]],
        "index_name": EMBEDDING_WORKER_CONFIG["metadata_vector_index_name"],
        "description": "Actively scraped subreddits"
    }
}

# Initialize Azure OpenAI client for query embedding
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
    logger.info(f"✅ Azure OpenAI client initialized ({EMBEDDING_CONFIG['model_name']})\n")
except ImportError:
    logger.error("openai package not installed. Run: pip install openai")
    sys.exit(1)
except Exception as e:
    logger.error(f"Failed to initialize Azure OpenAI client: {e}")
    sys.exit(1)


def generate_query_embedding(query: str) -> List[float]:
    """Generate embedding for a search query using Azure OpenAI."""
    deployment = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", AZURE_OPENAI_CONFIG.get("embedding_deployment", "text-embedding-3-small"))
    response = azure_client.embeddings.create(
        input=query,
        model=deployment
    )
    return response.data[0].embedding


def search_collection(
    collection,
    index_name: str,
    query_embedding: List[float],
    limit: int = 10,
    min_subscribers: Optional[int] = 1000,
    max_subscribers: Optional[int] = None,
    exclude_nsfw: bool = True,
    language: Optional[str] = None,
    subreddit_type: str = "public",
    num_candidates: int = 100,
    source_label: str = "unknown"
) -> List[Dict]:
    """
    Search a single collection for subreddits.

    Args:
        collection: MongoDB collection to search
        index_name: Vector search index name
        query_embedding: Pre-computed query embedding
        limit: Number of results to return
        min_subscribers: Minimum subscriber count
        max_subscribers: Maximum subscriber count
        exclude_nsfw: Filter out NSFW subreddits
        language: Language filter
        subreddit_type: Subreddit type filter
        num_candidates: Number of candidates for vector search
        source_label: Label for the source collection

    Returns:
        List of matching subreddit dictionaries with similarity scores
    """
    # Build MongoDB filters
    filters = {}

    if subreddit_type:
        filters["subreddit_type"] = subreddit_type

    if exclude_nsfw:
        filters["over_18"] = False

    if language:
        filters["lang"] = language

    # Subscriber filters
    subscriber_filter = {}
    if min_subscribers is not None:
        subscriber_filter["$gte"] = min_subscribers
    if max_subscribers is not None:
        subscriber_filter["$lte"] = max_subscribers

    if subscriber_filter:
        filters["subscribers"] = subscriber_filter

    try:
        pipeline = [
            {
                "$vectorSearch": {
                    "index": index_name,
                    "path": "embeddings.combined_embedding",
                    "queryVector": query_embedding,
                    "numCandidates": num_candidates,
                    "limit": limit,
                    "filter": {k: v for k, v in filters.items() if k != "subscribers"}
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

        # Add source label to each result
        for r in results:
            r["source"] = source_label

        return results

    except Exception as e:
        logger.warning(f"Search failed on {collection.name}: {e}")
        return []


def search_subreddits(
    query: str,
    source: str = "discovery",
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
        source: Search source - "discovery", "active", or "all"
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
    logger.info(f"   Source: {source}")
    logger.info(f"   Filters: min_subs={min_subscribers}, exclude_nsfw={exclude_nsfw}, type={subreddit_type}\n")

    # Generate query embedding using Azure OpenAI
    query_embedding = generate_query_embedding(query)

    all_results = []

    # Determine which collections to search
    sources_to_search = ["discovery", "active"] if source == "all" else [source]

    for src in sources_to_search:
        if src not in SEARCH_SOURCES:
            logger.warning(f"Unknown source: {src}")
            continue

        src_config = SEARCH_SOURCES[src]
        logger.info(f"   Searching {src_config['description']} ({src_config['collection'].name})...")

        results = search_collection(
            collection=src_config["collection"],
            index_name=src_config["index_name"],
            query_embedding=query_embedding,
            limit=limit,
            min_subscribers=min_subscribers,
            max_subscribers=max_subscribers,
            exclude_nsfw=exclude_nsfw,
            language=language,
            subreddit_type=subreddit_type,
            num_candidates=num_candidates,
            source_label=src
        )

        all_results.extend(results)
        logger.info(f"   Found {len(results)} results from {src}")

    # Deduplicate if searching multiple sources (keep highest score)
    if source == "all" and all_results:
        seen = {}
        for r in all_results:
            name = r["subreddit_name"]
            if name not in seen or r["score"] > seen[name]["score"]:
                seen[name] = r
        all_results = sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:limit]

    logger.info(f"\n✅ Total results: {len(all_results)}\n")
    return all_results


def print_results(results: List[Dict], detailed: bool = False, show_source: bool = False):
    """
    Pretty print search results.

    Args:
        results: List of search results
        detailed: Show detailed information
        show_source: Show which collection the result came from
    """
    if not results:
        logger.warning("No results found. Try:")
        logger.warning("  1. Different query terms")
        logger.warning("  2. Relaxing filters (--min-subscribers 0)")
        logger.warning("  3. Checking if subreddits exist: python generate_embeddings.py --stats")
        logger.warning("  4. Try --source all to search both collections")
        return

    print("\n" + "="*80)
    print(f"SEARCH RESULTS ({len(results)} subreddits)")
    print("="*80 + "\n")

    for i, sub in enumerate(results, 1):
        # Header
        source_label = f" [{sub.get('source', 'unknown')}]" if show_source else ""
        print(f"{i}. r/{sub['subreddit_name']}{source_label}")
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
            if show_source:
                print(f"   Source: {sub.get('source', 'unknown')}")

        print()


def interactive_search(source: str = "discovery"):
    """Interactive search mode - keep asking for queries."""
    print("\n" + "="*80)
    print("INTERACTIVE SEMANTIC SUBREDDIT SEARCH")
    print("="*80)
    print(f"\nSource: {source}")
    print("Enter your search queries (or 'quit' to exit)")
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

            results = search_subreddits(query, source=source, limit=10)
            print_results(results, detailed=False, show_source=(source == "all"))

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
  python semantic_search_subreddits.py --query "stocks" --source all  # Search both collections
  python semantic_search_subreddits.py --query "gaming" --source active  # Search only active scrapers
  python semantic_search_subreddits.py --interactive

Sources:
  discovery  - Subreddits discovered via discover_subreddits.py (default)
  active     - Subreddits actively being scraped (from reddit_scraper.py)
  all        - Search both collections and deduplicate
        """
    )
    parser.add_argument(
        '--query',
        type=str,
        help='Search query (e.g., "building b2b saas")'
    )
    parser.add_argument(
        '--source',
        type=str,
        default='discovery',
        choices=['discovery', 'active', 'all'],
        help='Search source: discovery (default), active, or all'
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
        interactive_search(source=args.source)
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
        source=args.source,
        limit=args.limit,
        min_subscribers=min_subs,
        max_subscribers=args.max_subscribers,
        exclude_nsfw=not args.include_nsfw,
        language=args.language,
        subreddit_type=subreddit_type
    )

    # Display results
    print_results(results, detailed=args.detailed, show_source=(args.source == "all"))


if __name__ == "__main__":
    main()
