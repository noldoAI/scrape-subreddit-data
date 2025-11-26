#!/usr/bin/env python3
"""
Repair Ghost Posts - Data Integrity Utility

Identifies and repairs "ghost" posts that are marked as scraped but have zero
comments in the database. This addresses the data inconsistency bug where posts
were marked as comments_scraped: True even though no comments were saved.

Usage:
    python tools/repair_ghost_posts.py [--subreddit SUBREDDIT] [--dry-run] [--stats-only]

Options:
    --subreddit SUBREDDIT  Only repair posts from specific subreddit
    --dry-run              Show what would be repaired without making changes
    --stats-only           Only show statistics, don't repair
"""

import pymongo
import os
import sys
import argparse
from datetime import datetime, UTC
from dotenv import load_dotenv

# Add parent directory to path for imports when run from tools/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DATABASE_NAME, COLLECTIONS

# Load environment
load_dotenv()

# MongoDB setup
client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client[DATABASE_NAME]
posts_collection = db[COLLECTIONS["POSTS"]]
comments_collection = db[COLLECTIONS["COMMENTS"]]


def find_ghost_posts(subreddit=None):
    """
    Find posts marked as scraped but with zero comments in database.

    Returns:
        List of post documents that are "ghosts"
    """
    # Build query
    query = {
        "comments_scraped": True  # Marked as scraped
    }

    if subreddit:
        query["subreddit"] = subreddit

    # Get all posts marked as scraped
    marked_scraped = list(posts_collection.find(query))

    # Get all post_ids that have comments
    post_ids_with_comments = set(comments_collection.distinct("post_id"))

    # Find posts marked scraped but with no comments
    ghost_posts = [
        post for post in marked_scraped
        if post["post_id"] not in post_ids_with_comments
    ]

    return ghost_posts


def find_incomplete_posts(subreddit=None):
    """
    Find posts where actual comment count is significantly less than num_comments.

    Returns:
        List of (post_doc, expected_count, actual_count) tuples
    """
    query = {"comments_scraped": True}
    if subreddit:
        query["subreddit"] = subreddit

    marked_scraped = posts_collection.find(query)

    incomplete_posts = []

    for post in marked_scraped:
        post_id = post["post_id"]
        expected_count = post.get("num_comments", 0)

        if expected_count == 0:
            continue  # Skip posts with 0 expected comments

        actual_count = comments_collection.count_documents({"post_id": post_id})

        # Flag if missing more than 10% of comments
        if actual_count < expected_count * 0.9:
            missing_pct = ((expected_count - actual_count) / expected_count) * 100
            incomplete_posts.append((post, expected_count, actual_count, missing_pct))

    return incomplete_posts


def reset_post_flags(post_id):
    """
    Reset scraping flags for a post so it will be re-scraped.

    Args:
        post_id: ID of post to reset
    """
    posts_collection.update_one(
        {"post_id": post_id},
        {"$set": {
            "comments_scraped": False,
            "initial_comments_scraped": False,
            "last_comment_fetch_time": None,
            "comments_scraped_at": None
        }}
    )


def print_statistics(ghost_posts, incomplete_posts, subreddit=None):
    """Print detailed statistics about data integrity issues."""
    print("\n" + "=" * 80)
    print("DATA INTEGRITY REPORT")
    if subreddit:
        print(f"Subreddit: r/{subreddit}")
    else:
        print("All Subreddits")
    print("=" * 80)

    # Total posts stats
    total_query = {"subreddit": subreddit} if subreddit else {}
    total_posts = posts_collection.count_documents(total_query)
    scraped_posts = posts_collection.count_documents({**total_query, "comments_scraped": True})
    total_comments = comments_collection.count_documents({"subreddit": subreddit} if subreddit else {})

    print(f"\n📊 Overall Statistics:")
    print(f"  Total posts: {total_posts:,}")
    print(f"  Posts marked scraped: {scraped_posts:,}")
    print(f"  Total comments: {total_comments:,}")

    # Ghost posts
    print(f"\n👻 Ghost Posts (marked scraped, 0 comments in DB):")
    print(f"  Count: {len(ghost_posts):,}")
    if scraped_posts > 0:
        ghost_pct = (len(ghost_posts) / scraped_posts) * 100
        print(f"  Percentage of scraped posts: {ghost_pct:.1f}%")

    if ghost_posts:
        print(f"\n  Top 10 Ghost Posts:")
        for i, post in enumerate(ghost_posts[:10], 1):
            title = post.get("title", "")[:60]
            post_id = post.get("post_id")
            num_comments = post.get("num_comments", 0)
            print(f"    {i}. {post_id} - {num_comments} comments claimed - \"{title}\"")

    # Incomplete posts
    print(f"\n⚠️  Incomplete Posts (missing >10% of comments):")
    print(f"  Count: {len(incomplete_posts):,}")

    if incomplete_posts:
        print(f"\n  Top 10 Incomplete Posts:")
        for i, (post, expected, actual, missing_pct) in enumerate(incomplete_posts[:10], 1):
            title = post.get("title", "")[:50]
            post_id = post.get("post_id")
            missing = expected - actual
            print(f"    {i}. {post_id} - Expected: {expected}, Actual: {actual}, Missing: {missing} ({missing_pct:.1f}%)")
            print(f"       \"{title}\"")

    print("\n" + "=" * 80)


def repair_ghost_posts(ghost_posts, dry_run=False):
    """
    Reset flags for ghost posts so they can be re-scraped.

    Args:
        ghost_posts: List of ghost post documents
        dry_run: If True, only show what would be done
    """
    if not ghost_posts:
        print("\n✅ No ghost posts found - database is clean!")
        return

    print(f"\n🔧 Repairing {len(ghost_posts)} ghost posts...")

    if dry_run:
        print("   [DRY RUN - No changes will be made]")

    repaired = 0
    for post in ghost_posts:
        post_id = post["post_id"]
        title = post.get("title", "")[:60]

        if not dry_run:
            reset_post_flags(post_id)
            repaired += 1

        print(f"  {'[DRY RUN] Would reset' if dry_run else '✓ Reset'}: {post_id} - \"{title}\"")

    if not dry_run:
        print(f"\n✅ Successfully repaired {repaired} ghost posts")
        print("   These posts will be re-scraped on the next scraper cycle.")
    else:
        print(f"\n[DRY RUN] Would repair {len(ghost_posts)} posts")


def repair_incomplete_posts(incomplete_posts, dry_run=False):
    """
    Reset flags for incomplete posts so comments can be re-scraped.

    Args:
        incomplete_posts: List of (post, expected, actual, missing_pct) tuples
        dry_run: If True, only show what would be done
    """
    if not incomplete_posts:
        print("\n✅ No incomplete posts found!")
        return

    print(f"\n🔧 Repairing {len(incomplete_posts)} incomplete posts...")

    if dry_run:
        print("   [DRY RUN - No changes will be made]")

    repaired = 0
    for post, expected, actual, missing_pct in incomplete_posts:
        post_id = post["post_id"]
        title = post.get("title", "")[:50]
        missing = expected - actual

        if not dry_run:
            # Reset only the initial_comments_scraped flag to force re-scrape
            posts_collection.update_one(
                {"post_id": post_id},
                {"$set": {
                    "initial_comments_scraped": False,
                    "last_comment_fetch_time": None
                }}
            )
            repaired += 1

        print(f"  {'[DRY RUN] Would reset' if dry_run else '✓ Reset'}: {post_id} - Missing {missing} ({missing_pct:.1f}%)")
        print(f"     \"{title}\"")

    if not dry_run:
        print(f"\n✅ Successfully repaired {repaired} incomplete posts")
        print("   These posts will be re-scraped on the next scraper cycle.")
    else:
        print(f"\n[DRY RUN] Would repair {len(incomplete_posts)} posts")


def main():
    parser = argparse.ArgumentParser(
        description="Repair ghost posts and incomplete comment data",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--subreddit", help="Only repair posts from specific subreddit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be repaired without making changes")
    parser.add_argument("--stats-only", action="store_true", help="Only show statistics, don't repair")
    parser.add_argument("--include-incomplete", action="store_true", help="Also repair posts with incomplete comments (>10%% missing)")

    args = parser.parse_args()

    print("🔍 Scanning database for data integrity issues...")

    # Find problematic posts
    ghost_posts = find_ghost_posts(args.subreddit)
    incomplete_posts = find_incomplete_posts(args.subreddit) if args.include_incomplete else []

    # Print statistics
    print_statistics(ghost_posts, incomplete_posts, args.subreddit)

    # Repair if not stats-only
    if not args.stats_only:
        print("\n" + "=" * 80)
        print("REPAIR PROCESS")
        print("=" * 80)

        # Repair ghost posts
        repair_ghost_posts(ghost_posts, args.dry_run)

        # Repair incomplete posts if requested
        if args.include_incomplete:
            repair_incomplete_posts(incomplete_posts, args.dry_run)

        if not args.dry_run:
            print("\n✅ Repair complete! Run the scraper to re-scrape fixed posts.")
        else:
            print("\n💡 Run without --dry-run to actually repair the posts.")
    else:
        print("\n💡 Run without --stats-only to repair the issues found.")


if __name__ == "__main__":
    main()
