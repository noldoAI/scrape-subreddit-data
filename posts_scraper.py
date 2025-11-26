#!/usr/bin/env python3
"""
Reddit Posts Scraper

Continuously scrapes posts and subreddit metadata for a specified subreddit.
Comments are handled by a separate comments_scraper.py script.
"""

import praw
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv
import pymongo
import os
import time
import sys
import argparse
from rate_limits import check_rate_limit
import logging

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
logger = logging.getLogger("posts-scraper")

# MongoDB setup using centralized config
client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client[DATABASE_NAME]
posts_collection = db[COLLECTIONS["POSTS"]]
subreddit_collection = db[COLLECTIONS["SUBREDDIT_METADATA"]]
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


class RedditPostsScraper:
    def __init__(self, subreddit_name, config=None):
        self.subreddit_name = subreddit_name
        self.config = {**DEFAULT_SCRAPER_CONFIG, **(config or {})}
        self.cycle_count = 0
        self.api_calls_this_cycle = 0  # Track API calls per cycle

        logger.info(f"🔗 Authenticated as: {reddit.user.me()}")
        logger.info(f"🎯 Target subreddit: r/{self.subreddit_name}")
        logger.info(f"⚙️  Configuration: {self.config}")
        logger.info(f"📊 Sorting methods: {', '.join(self.config.get('sorting_methods', ['hot']))}")

    def is_first_run(self):
        """Check if this is the first run for this subreddit (no posts in database)."""
        try:
            post_count = posts_collection.count_documents({"subreddit": self.subreddit_name})
            return post_count == 0
        except Exception as e:
            logger.error(f"Error checking first run status: {e}")
            return False  # Default to regular operation if check fails

    # ======================= POSTS SCRAPING =======================

    def scrape_posts_by_sort(self, sort_method="hot", limit=1000):
        """Scrape posts from the target subreddit using specified sorting method."""
        logger.info(f"\n--- Scraping {limit} {sort_method} posts from r/{self.subreddit_name} ---")

        check_rate_limit(reddit)
        self.api_calls_this_cycle += 1  # Track API call

        try:
            subreddit = reddit.subreddit(self.subreddit_name)

            # Get posts using the specified sorting method
            if sort_method == "hot":
                posts = subreddit.hot(limit=limit)
            elif sort_method == "new":
                posts = subreddit.new(limit=limit)
            elif sort_method == "rising":
                posts = subreddit.rising(limit=limit)
            elif sort_method == "top":
                # Use initial_top_time_filter on first run for historical data
                is_first_run = self.is_first_run()
                if is_first_run:
                    time_filter = self.config.get("initial_top_time_filter", "month")
                    logger.info(f"First run detected - using '{time_filter}' time filter for historical data")
                else:
                    time_filter = self.config.get("top_time_filter", "day")
                posts = subreddit.top(time_filter=time_filter, limit=limit)
            elif sort_method == "controversial":
                time_filter = self.config.get("controversial_time_filter", "day")
                posts = subreddit.controversial(time_filter=time_filter, limit=limit)
            else:
                logger.error(f"Unknown sort method: {sort_method}, defaulting to hot")
                posts = subreddit.hot(limit=limit)

            posts_list = []

            for i, post in enumerate(posts):
                if i % 100 == 0 and i > 0:
                    check_rate_limit(reddit)
                    self.api_calls_this_cycle += 1

                post_data = {
                    "title": post.title,
                    "url": post.url,
                    "reddit_url": f"https://reddit.com{post.permalink}",
                    "score": post.score,
                    "num_comments": post.num_comments,
                    "created_utc": post.created_utc,
                    "created_datetime": datetime.fromtimestamp(post.created_utc),
                    "author": str(post.author) if post.author else "[deleted]",
                    "subreddit": self.subreddit_name,
                    "post_id": post.id,
                    "scraped_at": datetime.now(UTC),
                    "selftext": post.selftext if post.selftext else "",  # No truncation
                    "is_self": post.is_self,
                    "upvote_ratio": post.upvote_ratio,
                    "distinguished": post.distinguished,
                    "stickied": post.stickied,
                    "over_18": post.over_18,
                    "spoiler": post.spoiler,
                    "locked": post.locked,
                    "sort_method": sort_method,  # Track which sort this came from
                    "comments_scraped": False,
                    "last_comment_fetch_time": None,
                    "initial_comments_scraped": False
                }
                posts_list.append(post_data)

            logger.info(f"Successfully scraped {len(posts_list)} {sort_method} posts")
            return posts_list

        except Exception as e:
            logger.error(f"Error scraping {sort_method} posts: {e}")
            return []

    def scrape_all_posts(self):
        """Scrape posts using multiple sorting methods and deduplicate."""
        sorting_methods = self.config.get("sorting_methods", ["hot"])
        sort_limits = self.config.get("sort_limits", {})

        logger.info(f"\n{'='*60}")
        logger.info(f"MULTI-SORT POST SCRAPING: {', '.join(sorting_methods)}")
        logger.info(f"{'='*60}")

        all_posts = []
        seen_post_ids = set()

        for sort_method in sorting_methods:
            limit = sort_limits.get(sort_method, self.config["posts_limit"])
            posts = self.scrape_posts_by_sort(sort_method, limit)

            # Deduplicate - only add posts we haven't seen yet
            new_posts = 0
            for post in posts:
                if post["post_id"] not in seen_post_ids:
                    all_posts.append(post)
                    seen_post_ids.add(post["post_id"])
                    new_posts += 1

            logger.info(f"  → {new_posts} new posts from {sort_method} (duplicates: {len(posts) - new_posts})")
            time.sleep(2)  # Be respectful between sort methods

        logger.info(f"\nTotal unique posts collected: {len(all_posts)}")
        logger.info(f"API calls this cycle so far: {self.api_calls_this_cycle}")
        return all_posts
    
    def save_posts_to_db(self, posts_list):
        """Save posts to MongoDB with bulk operations."""
        if not posts_list:
            return 0
        
        try:
            posts_collection.create_index("post_id", unique=True)
            
            # Get existing posts to preserve comment tracking fields
            post_ids = [post["post_id"] for post in posts_list]
            existing_posts = {
                doc["post_id"]: doc 
                for doc in posts_collection.find(
                    {"post_id": {"$in": post_ids}}, 
                    {"post_id": 1, "comments_scraped": 1, "comments_scraped_at": 1, 
                     "last_comment_fetch_time": 1, "initial_comments_scraped": 1}
                )
            }
            
            bulk_operations = []
            for post in posts_list:
                post_id = post["post_id"]
                
                # Preserve existing comment tracking fields
                if post_id in existing_posts:
                    existing = existing_posts[post_id]
                    post["comments_scraped"] = existing.get("comments_scraped", False)
                    post["comments_scraped_at"] = existing.get("comments_scraped_at")
                    post["last_comment_fetch_time"] = existing.get("last_comment_fetch_time")
                    post["initial_comments_scraped"] = existing.get("initial_comments_scraped", False)
                
                bulk_operations.append(
                    pymongo.UpdateOne(
                        {"post_id": post_id},
                        {"$set": post},
                        upsert=True
                    )
                )
            
            if bulk_operations:
                result = posts_collection.bulk_write(bulk_operations, ordered=False)
                logger.info(f"Bulk operation: {result.upserted_count} new posts, {result.modified_count} updated posts")
                return result.upserted_count
            
            return 0

        except Exception as e:
            logger.error(f"Error saving posts: {e}")
            return 0

    # ======================= SUBREDDIT METADATA =======================
    
    def should_update_subreddit_metadata(self):
        """Check if subreddit metadata should be updated."""
        try:
            latest_metadata = subreddit_collection.find_one(
                {"subreddit_name": self.subreddit_name},
                sort=[("last_updated", -1)]
            )
            
            if not latest_metadata:
                logger.info(f"No existing metadata for r/{self.subreddit_name}")
                return True
            
            last_updated = latest_metadata.get("last_updated")
            if not last_updated:
                return True
            
            # Handle both timezone-aware and timezone-naive datetimes
            current_time = datetime.now(UTC)
            if last_updated.tzinfo is None:
                # Database has timezone-naive datetime, convert it to UTC
                last_updated = last_updated.replace(tzinfo=UTC)
            
            time_since_update = (current_time - last_updated).total_seconds()
            should_update = time_since_update >= self.config["subreddit_update_interval"]
            
            if should_update:
                logger.info(f"Subreddit metadata last updated {time_since_update/3600:.1f} hours ago - updating")
            else:
                time_until_update = (self.config["subreddit_update_interval"] - time_since_update) / 3600
                logger.info(f"Subreddit metadata updated {time_since_update/3600:.1f} hours ago - next update in {time_until_update:.1f} hours")
            
            return should_update
            
        except Exception as e:
            logger.error(f"Error checking subreddit update status: {e}")
            return True
    
    def scrape_subreddit_metadata(self):
        """Scrape subreddit metadata."""
        logger.info(f"\n--- Scraping metadata for r/{self.subreddit_name} ---")
        
        check_rate_limit(reddit)
        
        try:
            subreddit = reddit.subreddit(self.subreddit_name)
            
            metadata = {
                "subreddit_name": self.subreddit_name,
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
                "submission_text": subreddit.submit_text,
                "submission_text_label": subreddit.submit_text_label,
                "user_is_moderator": subreddit.user_is_moderator,
                "user_is_subscriber": subreddit.user_is_subscriber,
                "quarantine": subreddit.quarantine,
                "advertiser_category": subreddit.advertiser_category,
                "is_enrolled_in_new_modmail": subreddit.is_enrolled_in_new_modmail,
                "primary_color": subreddit.primary_color,
                "show_media": subreddit.show_media,
                "show_media_preview": subreddit.show_media_preview,
                "spoilers_enabled": subreddit.spoilers_enabled,
                "allow_videos": subreddit.allow_videos,
                "allow_images": subreddit.allow_images,
                "allow_polls": subreddit.allow_polls,
                "allow_discovery": subreddit.allow_discovery,
                "allow_prediction_contributors": subreddit.allow_prediction_contributors,
                "has_menu_widget": subreddit.has_menu_widget,
                "icon_img": subreddit.icon_img,
                "community_icon": subreddit.community_icon,
                "banner_img": subreddit.banner_img,
                "banner_background_image": subreddit.banner_background_image,
                "mobile_banner_image": subreddit.mobile_banner_image,
                "scraped_at": datetime.now(UTC),
                "last_updated": datetime.now(UTC)
            }

            # ===== ENHANCED METADATA FOR SEMANTIC SEARCH =====

            # 1. Collect community rules (context-rich topic indicators)
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
                logger.info(f"Collected {len(rules)} rules")
            except Exception as e:
                logger.warning(f"Could not fetch rules: {e}")

            metadata["rules"] = rules
            metadata["rules_text"] = " | ".join(rules_text_parts) if rules_text_parts else ""

            # 2. Collect post guidelines (detailed topic context)
            try:
                post_reqs = subreddit.post_requirements()
                metadata["guidelines_text"] = post_reqs.get("guidelines_text", "")
                metadata["guidelines_display_policy"] = post_reqs.get("guidelines_display_policy", None)
                if metadata["guidelines_text"]:
                    logger.info(f"Collected post guidelines ({len(metadata['guidelines_text'])} chars)")
            except Exception as e:
                logger.warning(f"Could not fetch post requirements: {e}")
                metadata["guidelines_text"] = ""
                metadata["guidelines_display_policy"] = None

            # 3. Collect sample posts (real discussion topics for semantic understanding)
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
                logger.info(f"Collected {len(sample_posts)} sample posts")
            except Exception as e:
                logger.warning(f"Could not fetch sample posts: {e}")

            metadata["sample_posts"] = sample_posts
            metadata["sample_posts_titles"] = " | ".join(sample_titles) if sample_titles else ""

            # 4. Additional metadata fields
            metadata["subreddit_type"] = subreddit.subreddit_type  # public/private/restricted
            metadata["description_html"] = getattr(subreddit, "description_html", None)
            metadata["header_title"] = getattr(subreddit, "header_title", None)

            subscribers = metadata['subscribers'] or 0
            active_users = metadata['active_user_count'] or 0
            logger.info(f"Subscribers: {subscribers:,}, Active: {active_users:,}")
            logger.info(f"Enhanced metadata collected: {len(rules)} rules, {len(sample_posts)} posts, guidelines: {bool(metadata['guidelines_text'])}")
            return metadata
            
        except Exception as e:
            logger.error(f"Error scraping subreddit metadata: {e}")
            return None
    
    def save_subreddit_metadata(self, metadata):
        """Save subreddit metadata to MongoDB with embedding status flag.

        Only sets embedding_status to 'pending' if text fields used for
        embedding generation have changed (avoids unnecessary re-embedding).
        """
        if not metadata:
            return False

        try:
            subreddit_collection.create_index("subreddit_name", unique=True)

            # Fields used for embedding generation
            embedding_fields = [
                'title', 'public_description', 'description',
                'guidelines_text', 'rules_text', 'sample_posts_titles',
                'advertiser_category'
            ]

            # Check if embedding-relevant fields have changed
            existing = subreddit_collection.find_one(
                {"subreddit_name": metadata["subreddit_name"]},
                {field: 1 for field in embedding_fields}
            )

            needs_embedding = False
            if not existing:
                # New document - needs embedding
                needs_embedding = True
            else:
                # Check if any embedding field changed
                for field in embedding_fields:
                    old_val = existing.get(field, '')
                    new_val = metadata.get(field, '')
                    if old_val != new_val:
                        needs_embedding = True
                        logger.debug(f"Embedding field '{field}' changed")
                        break

            # Only set pending if content changed
            if needs_embedding:
                metadata["embedding_status"] = "pending"
                metadata["embedding_requested_at"] = datetime.now(UTC)

            result = subreddit_collection.update_one(
                {"subreddit_name": metadata["subreddit_name"]},
                {"$set": metadata},
                upsert=True
            )

            if result.upserted_id:
                logger.info(f"Inserted new subreddit metadata (embedding: pending)")
            elif needs_embedding:
                logger.info(f"Updated subreddit metadata - content changed (embedding: pending)")
            else:
                logger.info(f"Updated subreddit metadata - no content change (embedding: skipped)")

            return True

        except Exception as e:
            logger.error(f"Error saving subreddit metadata: {e}")
            return False
    
    def update_subreddit_metadata_if_needed(self):
        """Update subreddit metadata if enough time has passed."""
        if self.should_update_subreddit_metadata():
            metadata = self.scrape_subreddit_metadata()
            if metadata:
                return self.save_subreddit_metadata(metadata)
        return False

    # ======================= METRICS TRACKING =======================

    def update_scraper_metrics(self, posts_count, new_posts_count, cycle_duration):
        """Update scraper metrics in MongoDB after each cycle"""
        try:
            # Get current scraper document
            scraper_doc = db[COLLECTIONS["SCRAPERS"]].find_one({"subreddit": self.subreddit_name})
            if not scraper_doc:
                logger.warning("Scraper document not found, skipping metrics update")
                return

            metrics = scraper_doc.get("metrics", {})

            # Update cumulative totals
            total_posts = metrics.get("total_posts_collected", 0) + new_posts_count
            total_cycles = metrics.get("total_cycles", 0) + 1

            # Calculate rates (based on scraper lifetime)
            posts_per_hour = 0
            if scraper_doc.get("created_at"):
                created_at = scraper_doc["created_at"]
                # Handle both timezone-aware and naive datetimes
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                runtime_hours = (datetime.now(UTC) - created_at).total_seconds() / 3600
                if runtime_hours > 0:
                    posts_per_hour = total_posts / runtime_hours

            # Calculate average cycle duration
            prev_avg = metrics.get("avg_cycle_duration", 0)
            avg_cycle_duration = ((prev_avg * (total_cycles - 1)) + cycle_duration) / total_cycles if total_cycles > 0 else cycle_duration

            # Update database
            db[COLLECTIONS["SCRAPERS"]].update_one(
                {"subreddit": self.subreddit_name},
                {"$set": {
                    "metrics.total_posts_collected": total_posts,
                    "metrics.total_cycles": total_cycles,
                    "metrics.last_cycle_posts": posts_count,
                    "metrics.last_cycle_time": datetime.now(UTC),
                    "metrics.last_cycle_duration": round(cycle_duration, 1),
                    "metrics.posts_per_hour": round(posts_per_hour, 1),
                    "metrics.avg_cycle_duration": round(avg_cycle_duration, 1)
                }}
            )

            logger.info(f"✅ Metrics updated: {total_posts:,} posts ({posts_per_hour:.1f}/hr), {total_cycles} cycles")

        except Exception as e:
            logger.error(f"Error updating scraper metrics: {e}")

    # ======================= MAIN SCRAPING LOOP =======================
    
    def get_scraping_stats(self):
        """Get current scraping statistics (posts only - comments handled by separate scraper)."""
        try:
            total_posts = posts_collection.count_documents({"subreddit": self.subreddit_name})

            # Subreddit metadata stats
            subreddit_metadata = subreddit_collection.find_one({"subreddit_name": self.subreddit_name})

            return {
                "subreddit": self.subreddit_name,
                "total_posts": total_posts,
                "subreddit_metadata_exists": subreddit_metadata is not None,
                "subreddit_last_updated": subreddit_metadata.get("last_updated") if subreddit_metadata else None
            }
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {}
    
    def print_stats(self):
        """Print current scraping statistics (posts only)."""
        stats = self.get_scraping_stats()
        if stats:
            logger.info(f"\n{'='*60}")
            logger.info(f"POSTS SCRAPER STATISTICS FOR r/{stats['subreddit']}")
            logger.info(f"{'='*60}")
            logger.info(f"Total posts: {stats['total_posts']}")
            logger.info(f"Subreddit metadata: {'✓' if stats['subreddit_metadata_exists'] else '✗'}")
            if stats['subreddit_last_updated']:
                # Handle both timezone-aware and timezone-naive datetimes
                current_time = datetime.now(UTC)
                last_updated = stats['subreddit_last_updated']
                if last_updated.tzinfo is None:
                    # Database has timezone-naive datetime, convert it to UTC
                    last_updated = last_updated.replace(tzinfo=UTC)
                hours_ago = (current_time - last_updated).total_seconds() / 3600
                logger.info(f"Metadata last updated: {hours_ago:.1f} hours ago")
            logger.info(f"(Comment stats available via comments_scraper.py --stats)")
            logger.info(f"{'='*60}")
    
    def run_continuous_scraping(self):
        """Main continuous scraping loop."""
        logger.info(f"\n🚀 Starting posts scraping for r/{self.subreddit_name}")
        logger.info(f"⏰ Scrape interval: {self.config['scrape_interval']} seconds")
        logger.info(f"📊 Posts per scrape: {self.config['posts_limit']}")
        logger.info(f"🏢 Subreddit metadata interval: {self.config['subreddit_update_interval']/3600:.1f} hours")
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

                # PHASE 1: Scrape Posts (Multi-Sort)
                logger.info(f"\n{'='*60}")
                logger.info("POST SCRAPING PHASE (MULTI-SORT)")
                logger.info(f"{'='*60}")

                posts = self.scrape_all_posts()  # Use new multi-sort method
                new_posts = self.save_posts_to_db(posts)

                # PHASE 2: Update subreddit metadata (every 24 hours)
                subreddit_updated = self.update_subreddit_metadata_if_needed()
                
                # Calculate time taken
                elapsed_time = time.time() - start_time
                
                # Summary
                logger.info(f"\n{'='*60}")
                logger.info("CYCLE SUMMARY")
                logger.info(f"{'='*60}")
                logger.info(f"Posts scraped: {len(posts)} unique ({new_posts} new)")
                logger.info(f"Subreddit metadata: {'Updated' if subreddit_updated else 'No update needed'}")
                logger.info(f"Cycle completed in {elapsed_time:.2f} seconds")
                logger.info(f"API calls this cycle: ~{self.api_calls_this_cycle}")
                logger.info(f"Estimated QPM: ~{self.api_calls_this_cycle / max(elapsed_time / 60, 1):.1f}")

                # Update metrics in database
                self.update_scraper_metrics(
                    posts_count=len(posts),
                    new_posts_count=new_posts,
                    cycle_duration=elapsed_time
                )

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
    parser = argparse.ArgumentParser(description="Reddit Posts Scraper")
    parser.add_argument("subreddit", help="Subreddit name to scrape (without r/)")
    parser.add_argument("--stats", action="store_true", help="Show statistics only")
    parser.add_argument("--metadata-only", action="store_true", help="Update subreddit metadata only")
    parser.add_argument("--posts-limit", type=int, default=1000, help="Number of posts to scrape per cycle")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between scrape cycles")
    parser.add_argument("--sorting-methods", type=str, default="new,hot,rising", help="Comma-separated sorting methods (new,hot,rising,top,controversial)")

    args = parser.parse_args()

    # Parse sorting methods
    sorting_methods = [s.strip() for s in args.sorting_methods.split(",")]

    # Custom configuration from command line
    config = {
        "posts_limit": args.posts_limit,
        "scrape_interval": args.interval,
        "sorting_methods": sorting_methods,
    }

    # Create scraper instance
    scraper = RedditPostsScraper(args.subreddit, config)

    if args.stats:
        scraper.print_stats()
    elif args.metadata_only:
        logger.info(f"Updating subreddit metadata for r/{args.subreddit}...")
        updated = scraper.update_subreddit_metadata_if_needed()
        logger.info(f"Metadata: {'Updated' if updated else 'No update needed'}")
    else:
        # Show initial stats then run continuous scraping
        scraper.print_stats()
        scraper.run_continuous_scraping()


if __name__ == "__main__":
    main() 