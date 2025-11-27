#!/usr/bin/env python3
"""
Reddit Comments Scraper

Continuously scrapes comments for posts in a specified subreddit.
Posts are handled by a separate posts_scraper.py script.

This scraper implements intelligent comment update prioritization:
- HIGHEST: Posts never scraped (initial scrape)
- HIGH: High-activity posts (>100 comments) - update every 2 hours
- MEDIUM: Medium-activity posts (20-100 comments) - update every 6 hours
- LOW: Low-activity posts (<20 comments) - update every 24 hours
"""

import praw
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv
import pymongo
import os
import time
import sys
import argparse
import threading
from rate_limits import check_rate_limit
from praw.models import MoreComments
import logging

# Prometheus metrics
try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    PROMETHEUS_ENABLED = True
except ImportError:
    PROMETHEUS_ENABLED = False

# Import centralized configuration
from config import DATABASE_NAME, COLLECTIONS, DEFAULT_SCRAPER_CONFIG, LOGGING_CONFIG

# Load environment variables
load_dotenv()

# Configure logging with timestamps
logging.basicConfig(
    format=LOGGING_CONFIG["format"],
    datefmt=LOGGING_CONFIG["date_format"],
    level=getattr(logging, LOGGING_CONFIG["level"]),
    force=True  # Override any existing logging configuration
)
logger = logging.getLogger("comments-scraper")

# =============================================================================
# PROMETHEUS METRICS
# =============================================================================
if PROMETHEUS_ENABLED:
    # Comments scraped counter
    COMMENTS_SCRAPED = Counter(
        'reddit_comments_scraped_total',
        'Total comments scraped',
        ['subreddit']
    )

    # Posts processed for comments counter
    POSTS_PROCESSED = Counter(
        'reddit_comments_posts_processed_total',
        'Posts processed for comment scraping',
        ['subreddit']
    )

    # Cycle metrics
    CYCLE_DURATION = Histogram(
        'reddit_comments_cycle_duration_seconds',
        'Duration of comment scrape cycles',
        ['subreddit'],
        buckets=[10, 30, 60, 120, 300, 600, 1200]
    )

    CYCLE_COUNT = Counter(
        'reddit_comments_cycle_count_total',
        'Total comment scrape cycles completed',
        ['subreddit']
    )

    # Posts pending by priority
    POSTS_PENDING = Gauge(
        'reddit_comments_posts_pending',
        'Posts pending comment scraping by priority',
        ['subreddit', 'priority']
    )

    # Scraper errors counter
    SCRAPER_ERRORS = Counter(
        'reddit_comments_scraper_errors_total',
        'Comment scraper errors by type',
        ['subreddit', 'error_type']
    )

    # Verification failures
    VERIFICATION_FAILURES = Counter(
        'reddit_comments_verification_failures_total',
        'Comment verification failures',
        ['subreddit']
    )

    # Last successful scrape timestamp
    LAST_SUCCESS = Gauge(
        'reddit_comments_last_success_timestamp',
        'Unix timestamp of last successful comment scrape',
        ['subreddit']
    )

    def start_metrics_server(port=9100):
        """Start Prometheus metrics HTTP server in background thread."""
        def run_server():
            start_http_server(port)
            logger.info(f"Prometheus metrics server started on port {port}")

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        return thread

# MongoDB setup using centralized config
client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client[DATABASE_NAME]
posts_collection = db[COLLECTIONS["POSTS"]]
comments_collection = db[COLLECTIONS["COMMENTS"]]
errors_collection = db[COLLECTIONS["SCRAPE_ERRORS"]]

# Reddit API setup
reddit = praw.Reddit(
    client_id=os.getenv("R_CLIENT_ID"),
    client_secret=os.getenv("R_CLIENT_SECRET"),
    username=os.getenv("R_USERNAME"),
    password=os.getenv("R_PASSWORD"),
    user_agent=os.getenv("R_USER_AGENT")
)


# ======================= UTILITY FUNCTIONS =======================

def retry_with_backoff(max_retries=3, backoff_factor=2):
    """
    Decorator to retry function on failure with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        backoff_factor: Multiplier for exponential backoff (2 = 2s, 4s, 8s)
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        # Last attempt failed, re-raise exception
                        logger.error(f"{func.__name__} failed after {max_retries} attempts: {e}")
                        raise
                    wait_time = backoff_factor ** attempt
                    logger.warning(f"{func.__name__} failed (attempt {attempt+1}/{max_retries}): {e}")
                    logger.info(f"Retrying in {wait_time}s...")
                    time.sleep(wait_time)
            return None
        return wrapper
    return decorator


def log_scrape_error(subreddit, post_id, error_type, error_message, retry_count=0):
    """
    Log scraping errors to MongoDB for tracking and debugging.

    Args:
        subreddit: Name of the subreddit
        post_id: Post ID that failed
        error_type: Type of error (e.g., 'comment_scrape', 'save_failure')
        error_message: Error message/details
        retry_count: Number of retries attempted
    """
    try:
        error_doc = {
            "subreddit": subreddit,
            "post_id": post_id,
            "error_type": error_type,
            "error_message": str(error_message),
            "retry_count": retry_count,
            "timestamp": datetime.now(UTC),
            "resolved": False
        }
        errors_collection.insert_one(error_doc)
    except Exception as e:
        logger.error(f"Failed to log error to database: {e}")


class RedditCommentsScraper:
    def __init__(self, subreddit_name, config=None):
        self.subreddit_name = subreddit_name
        self.config = {**DEFAULT_SCRAPER_CONFIG, **(config or {})}
        self.cycle_count = 0
        self.api_calls_this_cycle = 0  # Track API calls per cycle

        logger.info(f"🔗 Authenticated as: {reddit.user.me()}")
        logger.info(f"🎯 Target subreddit: r/{self.subreddit_name}")
        logger.info(f"⚙️  Configuration: {self.config}")

    # ======================= COMMENTS SCRAPING =======================

    def get_posts_needing_comment_updates(self):
        """
        Get posts that need their comments scraped, using intelligent prioritization.

        Priority order:
        1. Posts never scraped (initial_comments_scraped = False or missing)
        2. High-activity posts (>100 comments) - update every 2 hours
        3. Medium-activity posts (20-100 comments) - update every 6 hours
        4. Low-activity posts (<20 comments) - update every 24 hours
        """
        batch_size = self.config.get("posts_per_comment_batch", 12)

        try:
            # Time thresholds for update frequency
            now = datetime.now(UTC)
            # Store as naive datetime to match database format
            two_hours_ago = (now - timedelta(hours=2)).replace(tzinfo=None)
            six_hours_ago = (now - timedelta(hours=6)).replace(tzinfo=None)
            twenty_four_hours_ago = (now - timedelta(hours=24)).replace(tzinfo=None)

            # Complex query for priority-based selection
            query = {
                "subreddit": self.subreddit_name,
                "$or": [
                    # Priority 1: Never scraped
                    {"initial_comments_scraped": {"$in": [False, None]}},
                    {"initial_comments_scraped": {"$exists": False}},
                    # Priority 2: High activity (>100 comments) - update every 2 hours
                    {
                        "num_comments": {"$gt": 100},
                        "$or": [
                            {"last_comment_fetch_time": {"$lt": two_hours_ago}},
                            {"last_comment_fetch_time": {"$exists": False}}
                        ]
                    },
                    # Priority 3: Medium activity (20-100 comments) - update every 6 hours
                    {
                        "num_comments": {"$gte": 20, "$lte": 100},
                        "$or": [
                            {"last_comment_fetch_time": {"$lt": six_hours_ago}},
                            {"last_comment_fetch_time": {"$exists": False}}
                        ]
                    },
                    # Priority 4: Low activity (<20 comments) - update every 24 hours
                    {
                        "num_comments": {"$lt": 20},
                        "$or": [
                            {"last_comment_fetch_time": {"$lt": twenty_four_hours_ago}},
                            {"last_comment_fetch_time": {"$exists": False}}
                        ]
                    }
                ]
            }

            # Sort: unscraped first, then by comment count (highest first), then by creation time (newest first)
            posts = list(posts_collection.find(query).sort([
                ("initial_comments_scraped", 1),  # False/None first
                ("num_comments", -1),              # More comments = higher priority
                ("created_utc", -1)                # Newer posts first
            ]).limit(batch_size))

            if posts:
                unscraped = sum(1 for p in posts if not p.get("initial_comments_scraped", False))
                high_activity = sum(1 for p in posts if p.get("num_comments", 0) > 100 and p.get("initial_comments_scraped", False))
                logger.info(f"Found {len(posts)} posts needing comment updates ({unscraped} unscraped, {high_activity} high-activity)")

            return posts

        except Exception as e:
            logger.error(f"Error getting posts for comment updates: {e}")
            return []

    def get_existing_comment_ids(self, post_id):
        """Get existing comment IDs for a post to avoid duplicates."""
        try:
            existing = comments_collection.find(
                {"post_id": post_id},
                {"comment_id": 1}
            )
            return set(doc["comment_id"] for doc in existing)
        except Exception as e:
            logger.error(f"Error getting existing comment IDs: {e}")
            return set()

    @retry_with_backoff(max_retries=3, backoff_factor=2)
    def scrape_post_comments(self, post_id, existing_comment_ids=None):
        """
        Scrape comments for a specific post with depth limiting.

        Args:
            post_id: Reddit post ID
            existing_comment_ids: Set of comment IDs already in database (for deduplication)

        Returns:
            List of comment dictionaries
        """
        if existing_comment_ids is None:
            existing_comment_ids = set()

        check_rate_limit(reddit)
        self.api_calls_this_cycle += 1

        try:
            submission = reddit.submission(id=post_id)

            # Configure comment expansion based on config
            replace_more_limit = self.config.get("replace_more_limit", 0)
            max_depth = self.config.get("max_comment_depth", 3)

            # replace_more_limit: 0 = skip MoreComments entirely (fastest)
            #                     None = expand all (slowest)
            #                     N = expand up to N MoreComments
            submission.comments.replace_more(limit=replace_more_limit)
            self.api_calls_this_cycle += 1

            comments_list = []

            def process_comment(comment, depth=0):
                """Recursively process comments up to max_depth."""
                # Skip if we've already scraped this comment
                if comment.id in existing_comment_ids:
                    return

                # Skip if beyond max depth
                if depth > max_depth:
                    return

                # Skip MoreComments objects
                if isinstance(comment, MoreComments):
                    return

                try:
                    comment_data = {
                        "comment_id": comment.id,
                        "post_id": post_id,
                        "subreddit": self.subreddit_name,
                        "body": comment.body,
                        "author": str(comment.author) if comment.author else "[deleted]",
                        "score": comment.score,
                        "created_utc": comment.created_utc,
                        "created_datetime": datetime.fromtimestamp(comment.created_utc),
                        "parent_id": comment.parent_id,
                        "depth": depth,
                        "is_submitter": comment.is_submitter,
                        "stickied": comment.stickied,
                        "distinguished": comment.distinguished,
                        "edited": comment.edited if comment.edited else False,
                        "controversiality": comment.controversiality,
                        "gilded": comment.gilded,
                        "total_awards_received": getattr(comment, 'total_awards_received', 0),
                        "scraped_at": datetime.now(UTC)
                    }
                    comments_list.append(comment_data)

                    # Process replies (recursive, with depth tracking)
                    if hasattr(comment, 'replies') and depth < max_depth:
                        for reply in comment.replies:
                            process_comment(reply, depth + 1)

                except Exception as e:
                    logger.warning(f"Error processing comment {comment.id}: {e}")

            # Process all top-level comments
            for comment in submission.comments:
                process_comment(comment, depth=0)

            return comments_list

        except Exception as e:
            logger.error(f"Error scraping comments for post {post_id}: {e}")
            raise  # Re-raise for retry decorator

    @retry_with_backoff(max_retries=3, backoff_factor=2)
    def save_comments_to_db(self, comments_list):
        """Save comments to MongoDB with bulk operations."""
        if not comments_list:
            return 0

        try:
            comments_collection.create_index("comment_id", unique=True)

            bulk_operations = []
            for comment in comments_list:
                bulk_operations.append(
                    pymongo.UpdateOne(
                        {"comment_id": comment["comment_id"]},
                        {"$set": comment},
                        upsert=True
                    )
                )

            if bulk_operations:
                result = comments_collection.bulk_write(bulk_operations, ordered=False)
                return result.upserted_count

            return 0

        except Exception as e:
            logger.error(f"Error saving comments: {e}")
            raise  # Re-raise for retry decorator

    def mark_posts_comments_updated(self, post_ids, is_initial=False):
        """Mark posts as having their comments updated."""
        try:
            update_fields = {
                "last_comment_fetch_time": datetime.now(UTC).replace(tzinfo=None),
                "comments_scraped": True
            }

            if is_initial:
                update_fields["initial_comments_scraped"] = True
                update_fields["comments_scraped_at"] = datetime.now(UTC)

            result = posts_collection.update_many(
                {"post_id": {"$in": post_ids}},
                {"$set": update_fields}
            )

            logger.debug(f"Marked {result.modified_count} posts as updated")
            return result.modified_count

        except Exception as e:
            logger.error(f"Error marking posts as updated: {e}")
            return 0

    def scrape_comments_for_posts(self):
        """
        Main comment scraping method - scrapes comments for a batch of posts.

        Returns:
            Tuple of (posts_processed, total_comments)
        """
        logger.info(f"\n{'='*60}")
        logger.info("COMMENT SCRAPING PHASE")
        logger.info(f"{'='*60}")

        posts = self.get_posts_needing_comment_updates()

        if not posts:
            logger.info("No posts need comment updates at this time")
            return 0, 0

        total_comments = 0
        posts_processed = 0
        successful_post_ids = []
        initial_scrape_post_ids = []
        verify_before_marking = self.config.get("verify_before_marking", True)

        for i, post in enumerate(posts):
            post_id = post["post_id"]
            num_comments = post.get("num_comments", 0)
            is_initial = not post.get("initial_comments_scraped", False)

            logger.info(f"\n[{i+1}/{len(posts)}] Scraping comments for post {post_id} ({num_comments} comments, {'initial' if is_initial else 'update'})")

            try:
                # Get existing comments to avoid duplicates
                existing_ids = self.get_existing_comment_ids(post_id)

                # Scrape comments
                comments = self.scrape_post_comments(post_id, existing_ids)

                if comments:
                    # Save to database
                    saved_count = self.save_comments_to_db(comments)

                    # Verify comments were actually saved (if enabled)
                    if verify_before_marking:
                        actual_count = comments_collection.count_documents({"post_id": post_id})
                        if actual_count == 0 and num_comments > 0:
                            logger.error(f"VERIFICATION FAILED - 0 comments in DB for post {post_id}")
                            log_scrape_error(
                                self.subreddit_name, post_id,
                                "verification_failed",
                                f"Expected comments but found 0 in database after save"
                            )
                            continue  # Skip marking as scraped

                    total_comments += saved_count
                    logger.info(f"  → Saved {saved_count} new comments (skipped {len(comments) - saved_count} existing)")
                else:
                    logger.info(f"  → No new comments to save")

                # Track successful scrapes
                successful_post_ids.append(post_id)
                if is_initial:
                    initial_scrape_post_ids.append(post_id)
                posts_processed += 1

            except Exception as e:
                logger.error(f"Failed to scrape comments for post {post_id}: {e}")
                log_scrape_error(self.subreddit_name, post_id, "comment_scrape_failed", str(e))
                continue

            # Be respectful of rate limits
            time.sleep(2)

        # Mark posts as updated (only successful ones)
        if successful_post_ids:
            # Mark all successful posts
            self.mark_posts_comments_updated(successful_post_ids, is_initial=False)

            # Additionally mark initial scrapes
            if initial_scrape_post_ids:
                self.mark_posts_comments_updated(initial_scrape_post_ids, is_initial=True)

        logger.info(f"\nComment scraping complete: {posts_processed} posts, {total_comments} new comments")
        return posts_processed, total_comments

    # ======================= METRICS TRACKING =======================

    def update_scraper_metrics(self, posts_processed, comments_count, cycle_duration):
        """Update scraper metrics in MongoDB after each cycle"""
        try:
            # Get current scraper document
            scraper_doc = db[COLLECTIONS["SCRAPERS"]].find_one({"subreddit": self.subreddit_name})
            if not scraper_doc:
                logger.warning("Scraper document not found, skipping metrics update")
                return

            metrics = scraper_doc.get("metrics", {})

            # Update cumulative totals
            total_comments = metrics.get("total_comments_collected", 0) + comments_count
            total_cycles = metrics.get("total_cycles", 0) + 1

            # Calculate rates (based on scraper lifetime)
            comments_per_hour = 0
            if scraper_doc.get("created_at"):
                created_at = scraper_doc["created_at"]
                # Handle both timezone-aware and naive datetimes
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                runtime_hours = (datetime.now(UTC) - created_at).total_seconds() / 3600
                if runtime_hours > 0:
                    comments_per_hour = total_comments / runtime_hours

            # Calculate average cycle duration
            prev_avg = metrics.get("avg_cycle_duration", 0)
            avg_cycle_duration = ((prev_avg * (total_cycles - 1)) + cycle_duration) / total_cycles if total_cycles > 0 else cycle_duration

            # Update database
            db[COLLECTIONS["SCRAPERS"]].update_one(
                {"subreddit": self.subreddit_name},
                {"$set": {
                    "metrics.total_comments_collected": total_comments,
                    "metrics.total_cycles": total_cycles,
                    "metrics.last_cycle_posts_processed": posts_processed,
                    "metrics.last_cycle_comments": comments_count,
                    "metrics.last_cycle_time": datetime.now(UTC),
                    "metrics.last_cycle_duration": round(cycle_duration, 1),
                    "metrics.comments_per_hour": round(comments_per_hour, 1),
                    "metrics.avg_cycle_duration": round(avg_cycle_duration, 1)
                }}
            )

            logger.info(f"✅ Metrics updated: {total_comments:,} comments ({comments_per_hour:.1f}/hr), {total_cycles} cycles")

        except Exception as e:
            logger.error(f"Error updating scraper metrics: {e}")

    # ======================= STATISTICS =======================

    def get_scraping_stats(self):
        """Get current scraping statistics."""
        try:
            total_posts = posts_collection.count_documents({"subreddit": self.subreddit_name})
            posts_with_initial_comments = posts_collection.count_documents({
                "subreddit": self.subreddit_name,
                "initial_comments_scraped": True
            })
            posts_without_initial_comments = posts_collection.count_documents({
                "subreddit": self.subreddit_name,
                "$or": [
                    {"initial_comments_scraped": {"$exists": False}},
                    {"initial_comments_scraped": False}
                ]
            })
            posts_with_recent_updates = posts_collection.count_documents({
                "subreddit": self.subreddit_name,
                "last_comment_fetch_time": {"$gte": (datetime.now(UTC) - timedelta(hours=24)).replace(tzinfo=None)}
            })
            total_comments = comments_collection.count_documents({"subreddit": self.subreddit_name})

            return {
                "subreddit": self.subreddit_name,
                "total_posts": total_posts,
                "posts_with_initial_comments": posts_with_initial_comments,
                "posts_without_initial_comments": posts_without_initial_comments,
                "posts_with_recent_updates": posts_with_recent_updates,
                "total_comments": total_comments,
                "initial_completion_rate": (posts_with_initial_comments / total_posts * 100) if total_posts > 0 else 0
            }
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {}

    def print_stats(self):
        """Print current scraping statistics."""
        stats = self.get_scraping_stats()
        if stats:
            logger.info(f"\n{'='*60}")
            logger.info(f"COMMENT SCRAPING STATISTICS FOR r/{stats['subreddit']}")
            logger.info(f"{'='*60}")
            logger.info(f"Total posts: {stats['total_posts']}")
            logger.info(f"Posts with initial comments: {stats['posts_with_initial_comments']}")
            logger.info(f"Posts without initial comments: {stats['posts_without_initial_comments']}")
            logger.info(f"Posts with recent updates: {stats['posts_with_recent_updates']}")
            logger.info(f"Total comments: {stats['total_comments']}")
            logger.info(f"Initial completion rate: {stats['initial_completion_rate']:.1f}%")
            logger.info(f"{'='*60}")

    # ======================= MAIN SCRAPING LOOP =======================

    def run_continuous_scraping(self):
        """Main continuous scraping loop."""
        logger.info(f"\n🚀 Starting comments scraping for r/{self.subreddit_name}")
        logger.info(f"⏰ Scrape interval: {self.config['scrape_interval']} seconds")
        logger.info(f"💬 Comments batch size: {self.config['posts_per_comment_batch']} posts")
        logger.info(f"📏 Max comment depth: {self.config.get('max_comment_depth', 3)}")
        logger.info("Press Ctrl+C to stop\n")

        try:
            while True:
                self.cycle_count += 1
                start_time = time.time()

                logger.info(f"\n{'='*80}")
                logger.info(f"SCRAPE CYCLE #{self.cycle_count} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"{'='*80}")

                # Reset API call counter for this cycle
                self.api_calls_this_cycle = 0

                # Scrape comments for posts
                posts_processed, total_comments = self.scrape_comments_for_posts()

                # Calculate time taken
                elapsed_time = time.time() - start_time

                # Summary
                logger.info(f"\n{'='*60}")
                logger.info("CYCLE SUMMARY")
                logger.info(f"{'='*60}")
                logger.info(f"Posts processed: {posts_processed}")
                logger.info(f"New comments: {total_comments}")
                logger.info(f"Cycle completed in {elapsed_time:.2f} seconds")
                logger.info(f"API calls this cycle: ~{self.api_calls_this_cycle}")
                logger.info(f"Estimated QPM: ~{self.api_calls_this_cycle / max(elapsed_time / 60, 1):.1f}")

                # Update metrics in database
                self.update_scraper_metrics(
                    posts_processed=posts_processed,
                    comments_count=total_comments,
                    cycle_duration=elapsed_time
                )

                # Update Prometheus metrics
                if PROMETHEUS_ENABLED:
                    CYCLE_DURATION.labels(subreddit=self.subreddit_name).observe(elapsed_time)
                    CYCLE_COUNT.labels(subreddit=self.subreddit_name).inc()
                    POSTS_PROCESSED.labels(subreddit=self.subreddit_name).inc(posts_processed)
                    COMMENTS_SCRAPED.labels(subreddit=self.subreddit_name).inc(total_comments)
                    LAST_SUCCESS.labels(subreddit=self.subreddit_name).set(time.time())

                # Wait before next cycle
                logger.info(f"\nWaiting {self.config['scrape_interval']} seconds before next cycle...")
                time.sleep(self.config['scrape_interval'])

        except KeyboardInterrupt:
            logger.info(f"\n\nScraping stopped by user after {self.cycle_count} cycles")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            logger.info("Restarting in 60 seconds...")
            time.sleep(60)
            self.run_continuous_scraping()  # Restart on error


def main():
    parser = argparse.ArgumentParser(description="Reddit Comments Scraper")
    parser.add_argument("subreddit", help="Subreddit name to scrape (without r/)")
    parser.add_argument("--stats", action="store_true", help="Show statistics only")
    parser.add_argument("--single-run", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between scrape cycles")
    parser.add_argument("--comment-batch", type=int, default=12, help="Number of posts to process for comments per cycle")
    parser.add_argument("--max-depth", type=int, default=3, help="Maximum comment nesting depth (0-indexed)")
    parser.add_argument("--metrics-port", type=int, default=9100, help="Port for Prometheus metrics server")

    args = parser.parse_args()

    # Start Prometheus metrics server if available
    if PROMETHEUS_ENABLED and not args.stats:
        start_metrics_server(args.metrics_port)
        logger.info(f"Prometheus metrics available at http://localhost:{args.metrics_port}/metrics")

    # Custom configuration from command line
    config = {
        "scrape_interval": args.interval,
        "posts_per_comment_batch": args.comment_batch,
        "max_comment_depth": args.max_depth,
    }

    # Create scraper instance
    scraper = RedditCommentsScraper(args.subreddit, config)

    if args.stats:
        scraper.print_stats()
    elif args.single_run:
        logger.info(f"Running single comment scraping cycle for r/{args.subreddit}...")
        posts_processed, total_comments = scraper.scrape_comments_for_posts()
        logger.info(f"Completed: {posts_processed} posts, {total_comments} comments")
    else:
        # Show initial stats then run continuous scraping
        scraper.print_stats()
        scraper.run_continuous_scraping()


if __name__ == "__main__":
    main()
