#!/usr/bin/env python3
"""
Test Script for Persona-Based Subreddit Search

Tests the persona-focused embedding search by querying with
natural language persona descriptions and displaying results.

Usage:
    python test_persona_search.py "I'm building SaaS AI video editor for TikTok creators"
    python test_persona_search.py --interactive
    python test_persona_search.py --compare "saas founder"
"""

import os
import sys
import argparse
import logging
from typing import List, Dict

from dotenv import load_dotenv
from pymongo import MongoClient

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('test-persona-search')

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

# Load embedding model
try:
    from sentence_transformers import SentenceTransformer
    logger.info("Loading embedding model...")
    model = SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True)
    logger.info("Model loaded successfully")
except Exception as e:
    logger.error(f"Failed to load model: {e}")
    sys.exit(1)


def search_persona(
    query: str,
    limit: int = 10,
    embedding_type: str = "persona",
    min_subscribers: int = 1000
) -> List[Dict]:
    """
    Search subreddits using persona-based embedding.

    Args:
        query: Persona description (e.g., "I'm building SaaS AI video editor")
        limit: Maximum results to return
        embedding_type: "persona" or "combined"
        min_subscribers: Minimum subscriber filter

    Returns:
        List of matching subreddits with scores
    """
    # Generate query embedding
    query_embedding = model.encode(query, convert_to_numpy=True).tolist()

    # Determine which embedding field to search
    embedding_field = "embeddings.persona_embedding" if embedding_type == "persona" else "embeddings.combined_embedding"
    index_name = "subreddit_persona_vector_index" if embedding_type == "persona" else "subreddit_vector_index"

    # Build aggregation pipeline
    pipeline = [
        {
            "$vectorSearch": {
                "index": index_name,
                "path": embedding_field,
                "queryVector": query_embedding,
                "numCandidates": 100,
                "limit": limit * 2  # Over-fetch for filtering
            }
        },
        {
            "$match": {
                "subscribers": {"$gte": min_subscribers}
            }
        },
        {
            "$project": {
                "subreddit_name": 1,
                "title": 1,
                "public_description": 1,
                "subscribers": 1,
                "advertiser_category": 1,
                "llm_enrichment": 1,
                "score": {"$meta": "vectorSearchScore"}
            }
        },
        {
            "$limit": limit
        }
    ]

    try:
        results = list(subreddit_discovery_collection.aggregate(pipeline))
        return results
    except Exception as e:
        logger.error(f"Search failed: {e}")
        logger.info("Make sure the vector search index exists. Run: python setup_vector_index.py")
        return []


def search_combined(
    query: str,
    limit: int = 10,
    min_subscribers: int = 1000
) -> List[Dict]:
    """Search using combined (topic-focused) embedding."""
    return search_persona(query, limit, "combined", min_subscribers)


def display_results(results: List[Dict], query: str, embedding_type: str = "persona"):
    """Display search results in a formatted way."""
    print("\n" + "="*80)
    print(f"PERSONA SEARCH RESULTS ({embedding_type.upper()} embedding)")
    print("="*80)
    print(f"Query: {query}")
    print(f"Results: {len(results)}")
    print("-"*80)

    for i, r in enumerate(results, 1):
        subreddit_name = r.get('subreddit_name', 'unknown')
        title = r.get('title', '')[:50]
        subscribers = r.get('subscribers', 0)
        score = r.get('score', 0)
        category = r.get('advertiser_category', 'N/A')

        print(f"\n{i}. r/{subreddit_name} (score: {score:.3f})")
        print(f"   Title: {title}")
        print(f"   Subscribers: {subscribers:,}")
        print(f"   Category: {category}")

        # Show LLM enrichment if available
        enrichment = r.get('llm_enrichment', {})
        if enrichment:
            audience = enrichment.get('audience_profile', '')[:80]
            if audience:
                print(f"   Audience: {audience}...")

            user_types = enrichment.get('audience_types', [])[:3]
            if user_types:
                print(f"   User types: {', '.join(user_types)}")

    print("\n" + "="*80)


def compare_search(query: str, limit: int = 10):
    """Compare persona vs combined embedding results."""
    print("\n" + "="*80)
    print("COMPARISON: PERSONA vs COMBINED EMBEDDINGS")
    print("="*80)
    print(f"Query: {query}")
    print("-"*80)

    # Search with persona embedding
    print("\n📊 PERSONA EMBEDDING (audience-focused):")
    persona_results = search_persona(query, limit, "persona")
    if persona_results:
        for i, r in enumerate(persona_results[:5], 1):
            name = r.get('subreddit_name', '?')
            score = r.get('score', 0)
            subs = r.get('subscribers', 0)
            print(f"   {i}. r/{name} (score: {score:.3f}, {subs:,} subs)")
    else:
        print("   No persona embeddings found. Run: python generate_embeddings.py --embedding-type persona")

    # Search with combined embedding
    print("\n📊 COMBINED EMBEDDING (topic-focused):")
    combined_results = search_combined(query, limit)
    if combined_results:
        for i, r in enumerate(combined_results[:5], 1):
            name = r.get('subreddit_name', '?')
            score = r.get('score', 0)
            subs = r.get('subscribers', 0)
            print(f"   {i}. r/{name} (score: {score:.3f}, {subs:,} subs)")
    else:
        print("   No combined embeddings found. Run: python generate_embeddings.py")

    # Show differences
    if persona_results and combined_results:
        persona_names = {r['subreddit_name'] for r in persona_results[:5]}
        combined_names = {r['subreddit_name'] for r in combined_results[:5]}

        unique_persona = persona_names - combined_names
        unique_combined = combined_names - persona_names

        if unique_persona or unique_combined:
            print("\n📊 DIFFERENCES:")
            if unique_persona:
                print(f"   Only in PERSONA: {', '.join(unique_persona)}")
            if unique_combined:
                print(f"   Only in COMBINED: {', '.join(unique_combined)}")

    print("\n" + "="*80)


def interactive_mode():
    """Run in interactive mode for testing multiple queries."""
    print("\n" + "="*80)
    print("PERSONA SEARCH - INTERACTIVE MODE")
    print("="*80)
    print("Enter persona descriptions to search (type 'quit' to exit)")
    print("Examples:")
    print("  - I'm building SaaS AI video editor for TikTok creators")
    print("  - I run a newsletter for indie hackers")
    print("  - I'm launching a fitness app for busy professionals")
    print("-"*80)

    while True:
        try:
            query = input("\nEnter persona: ").strip()

            if query.lower() in ['quit', 'exit', 'q']:
                print("Goodbye!")
                break

            if not query:
                continue

            results = search_persona(query, limit=10)
            display_results(results, query)

        except KeyboardInterrupt:
            print("\nGoodbye!")
            break


def show_stats():
    """Show embedding statistics."""
    total = subreddit_discovery_collection.count_documents({})
    with_persona = subreddit_discovery_collection.count_documents({
        "embeddings.persona_embedding": {"$exists": True}
    })
    with_combined = subreddit_discovery_collection.count_documents({
        "embeddings.combined_embedding": {"$exists": True}
    })
    with_enrichment = subreddit_discovery_collection.count_documents({
        "llm_enrichment": {"$exists": True, "$ne": None}
    })

    print("\n" + "="*60)
    print("EMBEDDING STATISTICS")
    print("="*60)
    print(f"Total subreddits: {total}")
    print(f"With LLM enrichment: {with_enrichment} ({with_enrichment/total*100:.1f}%)" if total > 0 else "With LLM enrichment: 0")
    print(f"With persona embedding: {with_persona} ({with_persona/total*100:.1f}%)" if total > 0 else "With persona embedding: 0")
    print(f"With combined embedding: {with_combined} ({with_combined/total*100:.1f}%)" if total > 0 else "With combined embedding: 0")
    print("="*60)

    if with_enrichment == 0:
        print("\n⚠️  No LLM enrichment data found!")
        print("   Run: python enrich_existing.py --batch-size 50")

    if with_persona == 0:
        print("\n⚠️  No persona embeddings found!")
        print("   Run: python generate_embeddings.py --embedding-type persona")


def main():
    parser = argparse.ArgumentParser(
        description='Test persona-based subreddit search'
    )
    parser.add_argument(
        'query',
        nargs='?',
        type=str,
        help='Persona description to search (e.g., "I\'m building SaaS for startups")'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=10,
        help='Number of results to return (default: 10)'
    )
    parser.add_argument(
        '--interactive',
        action='store_true',
        help='Run in interactive mode'
    )
    parser.add_argument(
        '--compare',
        action='store_true',
        help='Compare persona vs combined embedding results'
    )
    parser.add_argument(
        '--stats',
        action='store_true',
        help='Show embedding statistics'
    )
    parser.add_argument(
        '--min-subscribers',
        type=int,
        default=1000,
        help='Minimum subscriber count (default: 1000)'
    )

    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    if args.interactive:
        interactive_mode()
        return

    if not args.query:
        parser.print_help()
        print("\nExamples:")
        print('  python test_persona_search.py "I\'m building SaaS AI video editor for TikTok creators"')
        print('  python test_persona_search.py --interactive')
        print('  python test_persona_search.py --compare "startup founder tools"')
        print('  python test_persona_search.py --stats')
        return

    if args.compare:
        compare_search(args.query, args.limit)
    else:
        results = search_persona(
            args.query,
            limit=args.limit,
            min_subscribers=args.min_subscribers
        )
        display_results(results, args.query)


if __name__ == "__main__":
    main()
