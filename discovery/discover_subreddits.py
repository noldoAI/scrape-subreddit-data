#!/usr/bin/env python3
"""
Subreddit Discovery Script

Searches Reddit for subreddits matching specific topics and collects comprehensive metadata
including rules, guidelines, and sample posts for semantic search.

Usage:
    python discover_subreddits.py --query "saas" --limit 50
    python discover_subreddits.py --query "startup,entrepreneur,business" --limit 100
"""

import os
import sys
import argparse
import logging
from datetime import datetime, UTC
from typing import List, Dict
from dotenv import load_dotenv
import praw
from pymongo import MongoClient

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('subreddit-discovery')

# Load environment variables
load_dotenv()

# MongoDB connection
MONGODB_URI = os.getenv('MONGODB_URI')
if not MONGODB_URI:
    logger.error("MONGODB_URI environment variable not set")
    sys.exit(1)

client = MongoClient(MONGODB_URI)
db = client.noldo
subreddit_metadata_collection = db.subreddit_metadata

# Reddit API connection
try:
    reddit = praw.Reddit(
        client_id=os.getenv('R_CLIENT_ID'),
        client_secret=os.getenv('R_CLIENT_SECRET'),
        username=os.getenv('R_USERNAME'),
        password=os.getenv('R_PASSWORD'),
        user_agent=os.getenv('R_USER_AGENT')
    )
    logger.info(f"🔗 Authenticated as: {reddit.user.me()}")
except Exception as e:
    logger.error(f"Failed to authenticate with Reddit API: {e}")
    sys.exit(1)


def scrape_enhanced_subreddit_metadata(subreddit_name: str) -> Dict:
    """
    Scrape comprehensive subreddit metadata including rules, guidelines, and sample posts.

    Args:
        subreddit_name: Name of the subreddit (without r/)

    Returns:
        Dictionary with complete metadata or None if error
    """
    try:
        subreddit = reddit.subreddit(subreddit_name)

        # Basic metadata
        metadata = {
            "subreddit_name": subreddit_name,
            "display_name": subreddit.display_name,
            "title": subreddit.title,
            "public_description": subreddit.public_description,
            "description": subreddit.description,
            "url": subreddit.url,
            "subscribers": subreddit.subscribers,
            "active_user_count": getattr(subreddit, 'active_user_count', None),
            "over_18": subreddit.over18,
            "lang": subreddit.lang,
            "created_utc": subreddit.created_utc,
            "created_datetime": datetime.fromtimestamp(subreddit.created_utc),
            "submission_type": subreddit.submission_type,
            "advertiser_category": subreddit.advertiser_category,
            "subreddit_type": subreddit.subreddit_type,
            "discovered_at": datetime.now(UTC),
            "last_updated": datetime.now(UTC)
        }

        # Collect community rules
        rules = []
        rules_text_parts = []
        try:
            for rule in subreddit.rules:
                rule_dict = {
                    "short_name": rule.short_name,
                    "description": rule.description,
                    "kind": rule.kind,
                    "violation_reason": getattr(rule, 'violation_reason', None)
                }
                rules.append(rule_dict)
                rules_text_parts.append(f"{rule.short_name}: {rule.description}")
            logger.info(f"  ✓ Collected {len(rules)} rules")
        except Exception as e:
            logger.warning(f"  ⚠ Could not fetch rules: {e}")

        metadata["rules"] = rules
        metadata["rules_text"] = " | ".join(rules_text_parts) if rules_text_parts else ""

        # Collect post guidelines
        try:
            post_reqs = subreddit.post_requirements()
            metadata["guidelines_text"] = post_reqs.get("guidelines_text", "")
            metadata["guidelines_display_policy"] = post_reqs.get("guidelines_display_policy", None)
            if metadata["guidelines_text"]:
                logger.info(f"  ✓ Collected post guidelines ({len(metadata['guidelines_text'])} chars)")
        except Exception as e:
            logger.warning(f"  ⚠ Could not fetch post requirements: {e}")
            metadata["guidelines_text"] = ""
            metadata["guidelines_display_policy"] = None

        # Collect sample posts
        sample_posts = []
        sample_titles = []
        try:
            for post in subreddit.top(time_filter="month", limit=20):
                sample_post = {
                    "title": post.title,
                    "selftext_excerpt": post.selftext[:200] if post.selftext else "",
                    "score": post.score,
                    "num_comments": post.num_comments,
                    "created_utc": post.created_utc
                }
                sample_posts.append(sample_post)
                sample_titles.append(post.title)
            logger.info(f"  ✓ Collected {len(sample_posts)} sample posts")
        except Exception as e:
            logger.warning(f"  ⚠ Could not fetch sample posts: {e}")

        metadata["sample_posts"] = sample_posts
        metadata["sample_posts_titles"] = " | ".join(sample_titles) if sample_titles else ""

        return metadata

    except Exception as e:
        logger.error(f"  ✗ Error scraping r/{subreddit_name}: {e}")
        return None


def discover_subreddits(query: str, limit: int = 50) -> List[Dict]:
    """
    Search Reddit for subreddits matching query and collect their metadata.

    Args:
        query: Search query (e.g., "saas", "startup")
        limit: Maximum number of results

    Returns:
        List of discovered subreddit metadata dictionaries
    """
    logger.info(f"\n🔍 Searching for subreddits matching: '{query}' (limit: {limit})")

    try:
        # Search subreddits
        search_results = list(reddit.subreddits.search(query, limit=limit))
        logger.info(f"📊 Found {len(search_results)} subreddits\n")

        discovered = []

        for i, subreddit in enumerate(search_results, 1):
            logger.info(f"[{i}/{len(search_results)}] Processing r/{subreddit.display_name}")
            logger.info(f"  Subscribers: {subreddit.subscribers:,}")

            # Scrape comprehensive metadata
            metadata = scrape_enhanced_subreddit_metadata(subreddit.display_name)

            if metadata:
                # Store in MongoDB
                try:
                    subreddit_metadata_collection.update_one(
                        {"subreddit_name": metadata["subreddit_name"]},
                        {"$set": metadata},
                        upsert=True
                    )
                    logger.info(f"  ✓ Saved to database\n")
                    discovered.append(metadata)
                except Exception as e:
                    logger.error(f"  ✗ Database error: {e}\n")
            else:
                logger.warning(f"  ⚠ Skipped (metadata collection failed)\n")

        logger.info(f"\n✅ Discovery complete!")
        logger.info(f"📊 Successfully processed: {len(discovered)}/{len(search_results)} subreddits")

        return discovered

    except Exception as e:
        logger.error(f"Error during discovery: {e}")
        return []


def bulk_discover(queries: List[str], limit: int = 50):
    """
    Discover subreddits for multiple search queries.

    Args:
        queries: List of search queries
        limit: Maximum results per query
    """
    logger.info(f"\n🚀 Starting bulk discovery for {len(queries)} queries\n")
    logger.info(f"Queries: {', '.join(queries)}\n")
    logger.info("=" * 80 + "\n")

    all_discovered = []

    for query in queries:
        discovered = discover_subreddits(query, limit)
        all_discovered.extend(discovered)
        logger.info(f"\n{'=' * 80}\n")

    # Remove duplicates based on subreddit_name
    unique_subreddits = {d['subreddit_name']: d for d in all_discovered}

    logger.info(f"\n✅ Bulk discovery complete!")
    logger.info(f"📊 Total unique subreddits discovered: {len(unique_subreddits)}")
    logger.info(f"💾 All data saved to MongoDB: {db.name}.{subreddit_metadata_collection.name}")


def main():
    parser = argparse.ArgumentParser(
        description='Discover and scrape comprehensive metadata for subreddits'
    )
    parser.add_argument(
        '--query',
        type=str,
        required=True,
        help='Search query or comma-separated list of queries (e.g., "saas" or "startup,entrepreneur,business")'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=50,
        help='Maximum number of results per query (default: 50)'
    )

    args = parser.parse_args()

    # Parse queries (support comma-separated list)
    queries = [q.strip() for q in args.query.split(',')]

    if len(queries) == 1:
        # Single query
        discover_subreddits(queries[0], args.limit)
    else:
        # Multiple queries
        bulk_discover(queries, args.limit)


if __name__ == "__main__":
    main()
