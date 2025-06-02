#!/usr/bin/env python3
"""
Unified Reddit Scraper

Continuously scrapes posts, comments, and subreddit metadata for a specified subreddit.
Combines all scraping functionality into one unified system.
"""

import praw
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pymongo
import os
import time
import sys
import argparse
from rate_limits import check_rate_limit
from praw.models import MoreComments

# Load environment variables
load_dotenv()

# MongoDB setup
client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client["seeky_testing"]
posts_collection = db["reddit_posts"]
comments_collection = db["reddit_comments"]
subreddit_collection = db["subreddit_metadata"]

# Reddit API setup
reddit = praw.Reddit(
    client_id=os.getenv("R_CLIENT_ID"),
    client_secret=os.getenv("R_CLIENT_SECRET"),
    username=os.getenv("R_USERNAME"),
    password=os.getenv("R_PASSWORD"),
    user_agent=os.getenv("R_USER_AGENT")
)

# Default configuration
DEFAULT_CONFIG = {
    "scrape_interval": 300,        # 5 minutes between cycles
    "posts_limit": 1000,           # Posts per scrape
    "posts_per_comment_batch": 20, # Comments batch size
    "subreddit_update_interval": 86400,  # 24 hours for subreddit metadata
}


class UnifiedRedditScraper:
    def __init__(self, subreddit_name, config=None):
        self.subreddit_name = subreddit_name
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.cycle_count = 0
        
        print(f"🔗 Authenticated as: {reddit.user.me()}")
        print(f"🎯 Target subreddit: r/{self.subreddit_name}")
        print(f"⚙️  Configuration: {self.config}")
    
    # ======================= POSTS SCRAPING =======================
    
    def scrape_hot_posts(self, limit=1000):
        """Scrape hot posts from the target subreddit."""
        print(f"\n--- Scraping {limit} hot posts from r/{self.subreddit_name} ---")
        
        check_rate_limit(reddit)
        
        try:
            posts = reddit.subreddit(self.subreddit_name).hot(limit=limit)
            posts_list = []
            
            for i, post in enumerate(posts):
                if i % 100 == 0 and i > 0:
                    check_rate_limit(reddit)
                
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
                    "scraped_at": datetime.utcnow(),
                    "selftext": post.selftext[:1000] if post.selftext else "",
                    "is_self": post.is_self,
                    "upvote_ratio": post.upvote_ratio,
                    "distinguished": post.distinguished,
                    "stickied": post.stickied,
                    "over_18": post.over_18,
                    "spoiler": post.spoiler,
                    "locked": post.locked,
                    "comments_scraped": False,
                    "last_comment_fetch_time": None,
                    "initial_comments_scraped": False
                }
                posts_list.append(post_data)
            
            print(f"Successfully scraped {len(posts_list)} posts")
            return posts_list
            
        except Exception as e:
            print(f"Error scraping posts: {e}")
            return []
    
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
                print(f"Bulk operation: {result.upserted_count} new posts, {result.modified_count} updated posts")
                return result.upserted_count
            
            return 0
            
        except Exception as e:
            print(f"Error saving posts: {e}")
            return 0
    
    # ======================= COMMENTS SCRAPING =======================
    
    def get_posts_needing_comment_updates(self, limit=20):
        """Get posts that need comment scraping or updates."""
        try:
            current_time = datetime.utcnow()
            six_hours_ago = current_time - timedelta(hours=6)
            twenty_four_hours_ago = current_time - timedelta(hours=24)
            
            posts = list(posts_collection.find({
                "subreddit": self.subreddit_name,  # Only posts from our target subreddit
                "$or": [
                    {"initial_comments_scraped": {"$ne": True}},
                    {
                        "created_datetime": {"$gte": twenty_four_hours_ago},
                        "$or": [
                            {"last_comment_fetch_time": {"$exists": False}},
                            {"last_comment_fetch_time": {"$lte": six_hours_ago}},
                            {"last_comment_fetch_time": None}
                        ]
                    },
                    {
                        "created_datetime": {"$lt": twenty_four_hours_ago},
                        "$or": [
                            {"last_comment_fetch_time": {"$exists": False}},
                            {"last_comment_fetch_time": {"$lte": twenty_four_hours_ago}},
                            {"last_comment_fetch_time": None}
                        ]
                    }
                ]
            }).sort([
                ("initial_comments_scraped", 1),
                ("created_utc", -1)
            ]).limit(limit))
            
            print(f"Found {len(posts)} posts needing comment updates")
            return posts
            
        except Exception as e:
            print(f"Error fetching posts needing updates: {e}")
            return []
    
    def get_existing_comment_ids(self, post_id):
        """Get existing comment IDs for a post to avoid duplicates."""
        try:
            existing_comments = comments_collection.find(
                {"post_id": post_id}, 
                {"comment_id": 1}
            )
            return {doc["comment_id"] for doc in existing_comments}
        except Exception as e:
            print(f"Error getting existing comment IDs: {e}")
            return set()
    
    def scrape_post_comments(self, post_id):
        """Scrape comments for a post, only collecting new ones."""
        print(f"\n--- Scraping comments for post {post_id} ---")
        
        check_rate_limit(reddit)
        
        try:
            existing_comment_ids = self.get_existing_comment_ids(post_id)
            print(f"Found {len(existing_comment_ids)} existing comments")
            
            submission = reddit.submission(id=post_id)
            submission.comments.replace_more(limit=10)
            
            comments_data = []
            new_comments_count = 0
            
            def process_comment(comment, parent_id=None, depth=0):
                nonlocal new_comments_count
                
                if isinstance(comment, MoreComments):
                    return
                
                try:
                    if comment.id in existing_comment_ids:
                        if hasattr(comment, 'replies') and comment.replies:
                            for reply in comment.replies:
                                process_comment(reply, parent_id=comment.id, depth=depth + 1)
                        return
                    
                    comment_data = {
                        "comment_id": comment.id,
                        "post_id": post_id,
                        "parent_id": parent_id,
                        "parent_type": "post" if parent_id is None else "comment",
                        "author": str(comment.author) if comment.author else "[deleted]",
                        "body": comment.body if hasattr(comment, 'body') else "",
                        "score": comment.score if hasattr(comment, 'score') else 0,
                        "created_utc": comment.created_utc if hasattr(comment, 'created_utc') else 0,
                        "created_datetime": datetime.fromtimestamp(comment.created_utc) if hasattr(comment, 'created_utc') else None,
                        "depth": depth,
                        "is_submitter": comment.is_submitter if hasattr(comment, 'is_submitter') else False,
                        "distinguished": comment.distinguished if hasattr(comment, 'distinguished') else None,
                        "stickied": comment.stickied if hasattr(comment, 'stickied') else False,
                        "edited": bool(comment.edited) if hasattr(comment, 'edited') else False,
                        "controversiality": comment.controversiality if hasattr(comment, 'controversiality') else 0,
                        "scraped_at": datetime.utcnow(),
                        "subreddit": self.subreddit_name,
                        "gilded": comment.gilded if hasattr(comment, 'gilded') else 0,
                        "total_awards_received": comment.total_awards_received if hasattr(comment, 'total_awards_received') else 0
                    }
                    
                    comments_data.append(comment_data)
                    new_comments_count += 1
                    
                    if hasattr(comment, 'replies') and comment.replies:
                        for reply in comment.replies:
                            process_comment(reply, parent_id=comment.id, depth=depth + 1)
                            
                except Exception as e:
                    print(f"Error processing comment: {e}")
            
            for comment in submission.comments:
                process_comment(comment, parent_id=None, depth=0)
            
            print(f"Found {new_comments_count} new comments")
            return comments_data
            
        except Exception as e:
            print(f"Error scraping comments: {e}")
            return []
    
    def save_comments_to_db(self, comments_list):
        """Save comments to MongoDB."""
        if not comments_list:
            return 0
        
        try:
            comments_collection.create_index("comment_id", unique=True)
            comments_collection.create_index("post_id")
            comments_collection.create_index("parent_id")
            
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
                print(f"Saved {result.upserted_count} new comments, updated {result.modified_count}")
                return result.upserted_count
            
            return 0
            
        except Exception as e:
            print(f"Error saving comments: {e}")
            return 0
    
    def mark_posts_comments_updated(self, post_ids, is_initial_scrape=False):
        """Mark posts as having their comments updated."""
        if not post_ids:
            return
        
        try:
            bulk_operations = []
            update_time = datetime.utcnow()
            
            for post_id in post_ids:
                update_data = {
                    "comments_scraped": True,
                    "last_comment_fetch_time": update_time
                }
                
                if is_initial_scrape:
                    update_data["initial_comments_scraped"] = True
                    update_data["comments_scraped_at"] = update_time
                
                bulk_operations.append(
                    pymongo.UpdateOne(
                        {"post_id": post_id},
                        {"$set": update_data}
                    )
                )
            
            if bulk_operations:
                result = posts_collection.bulk_write(bulk_operations, ordered=False)
                action = "initially scraped" if is_initial_scrape else "updated"
                print(f"Marked {result.modified_count} posts as {action}")
                
        except Exception as e:
            print(f"Error marking posts as updated: {e}")
    
    def scrape_comments_for_posts(self):
        """Main comment scraping function."""
        print(f"\n{'='*60}")
        print("COMMENT SCRAPING PHASE")
        print(f"{'='*60}")
        
        posts = self.get_posts_needing_comment_updates(self.config["posts_per_comment_batch"])
        
        if not posts:
            print("No posts need comment updates.")
            return 0, 0
        
        total_comments = 0
        posts_processed = 0
        initial_scrape_posts = []
        update_posts = []
        all_comments = []
        
        for post in posts:
            try:
                post_id = post["post_id"]
                is_initial = not post.get("initial_comments_scraped", False)
                action = "Initial scrape" if is_initial else "Update"
                
                print(f"\n{action} for: {post['title'][:50]}...")
                
                comments = self.scrape_post_comments(post_id)
                
                if comments or is_initial:
                    all_comments.extend(comments)
                    if is_initial:
                        initial_scrape_posts.append(post_id)
                    else:
                        update_posts.append(post_id)
                    posts_processed += 1
                
                time.sleep(2)  # Be respectful
                
            except Exception as e:
                print(f"Error processing post: {e}")
                continue
        
        if all_comments:
            total_comments = self.save_comments_to_db(all_comments)
        
        if initial_scrape_posts:
            self.mark_posts_comments_updated(initial_scrape_posts, is_initial_scrape=True)
        if update_posts:
            self.mark_posts_comments_updated(update_posts, is_initial_scrape=False)
        
        print(f"\nComment scraping completed: {posts_processed} posts ({len(initial_scrape_posts)} initial, {len(update_posts)} updates), {total_comments} new comments")
        return posts_processed, total_comments
    
    # ======================= SUBREDDIT METADATA =======================
    
    def should_update_subreddit_metadata(self):
        """Check if subreddit metadata should be updated."""
        try:
            latest_metadata = subreddit_collection.find_one(
                {"subreddit_name": self.subreddit_name},
                sort=[("last_updated", -1)]
            )
            
            if not latest_metadata:
                print(f"No existing metadata for r/{self.subreddit_name}")
                return True
            
            last_updated = latest_metadata.get("last_updated")
            if not last_updated:
                return True
            
            time_since_update = (datetime.utcnow() - last_updated).total_seconds()
            should_update = time_since_update >= self.config["subreddit_update_interval"]
            
            if should_update:
                print(f"Subreddit metadata last updated {time_since_update/3600:.1f} hours ago - updating")
            else:
                time_until_update = (self.config["subreddit_update_interval"] - time_since_update) / 3600
                print(f"Subreddit metadata updated {time_since_update/3600:.1f} hours ago - next update in {time_until_update:.1f} hours")
            
            return should_update
            
        except Exception as e:
            print(f"Error checking subreddit update status: {e}")
            return True
    
    def scrape_subreddit_metadata(self):
        """Scrape subreddit metadata."""
        print(f"\n--- Scraping metadata for r/{self.subreddit_name} ---")
        
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
                "active_user_count": subreddit.accounts_active,
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
                "scraped_at": datetime.utcnow(),
                "last_updated": datetime.utcnow()
            }
            
            print(f"Subscribers: {metadata['subscribers']:,}, Active: {metadata['active_user_count']:,}")
            return metadata
            
        except Exception as e:
            print(f"Error scraping subreddit metadata: {e}")
            return None
    
    def save_subreddit_metadata(self, metadata):
        """Save subreddit metadata to MongoDB."""
        if not metadata:
            return False
        
        try:
            subreddit_collection.create_index("subreddit_name", unique=True)
            
            result = subreddit_collection.update_one(
                {"subreddit_name": metadata["subreddit_name"]},
                {"$set": metadata},
                upsert=True
            )
            
            if result.upserted_id:
                print(f"Inserted new subreddit metadata")
            else:
                print(f"Updated existing subreddit metadata")
            
            return True
            
        except Exception as e:
            print(f"Error saving subreddit metadata: {e}")
            return False
    
    def update_subreddit_metadata_if_needed(self):
        """Update subreddit metadata if enough time has passed."""
        if self.should_update_subreddit_metadata():
            metadata = self.scrape_subreddit_metadata()
            if metadata:
                return self.save_subreddit_metadata(metadata)
        return False
    
    # ======================= MAIN SCRAPING LOOP =======================
    
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
                "last_comment_fetch_time": {"$gte": datetime.utcnow() - timedelta(hours=24)}
            })
            total_comments = comments_collection.count_documents({"subreddit": self.subreddit_name})
            
            # Subreddit metadata stats
            subreddit_metadata = subreddit_collection.find_one({"subreddit_name": self.subreddit_name})
            
            return {
                "subreddit": self.subreddit_name,
                "total_posts": total_posts,
                "posts_with_initial_comments": posts_with_initial_comments,
                "posts_without_initial_comments": posts_without_initial_comments,
                "posts_with_recent_updates": posts_with_recent_updates,
                "total_comments": total_comments,
                "initial_completion_rate": (posts_with_initial_comments / total_posts * 100) if total_posts > 0 else 0,
                "subreddit_metadata_exists": subreddit_metadata is not None,
                "subreddit_last_updated": subreddit_metadata.get("last_updated") if subreddit_metadata else None
            }
        except Exception as e:
            print(f"Error getting stats: {e}")
            return {}
    
    def print_stats(self):
        """Print current scraping statistics."""
        stats = self.get_scraping_stats()
        if stats:
            print(f"\n{'='*60}")
            print(f"SCRAPING STATISTICS FOR r/{stats['subreddit']}")
            print(f"{'='*60}")
            print(f"Total posts: {stats['total_posts']}")
            print(f"Posts with initial comments: {stats['posts_with_initial_comments']}")
            print(f"Posts without initial comments: {stats['posts_without_initial_comments']}")
            print(f"Posts with recent updates: {stats['posts_with_recent_updates']}")
            print(f"Total comments: {stats['total_comments']}")
            print(f"Initial completion rate: {stats['initial_completion_rate']:.1f}%")
            print(f"Subreddit metadata: {'✓' if stats['subreddit_metadata_exists'] else '✗'}")
            if stats['subreddit_last_updated']:
                hours_ago = (datetime.utcnow() - stats['subreddit_last_updated']).total_seconds() / 3600
                print(f"Metadata last updated: {hours_ago:.1f} hours ago")
            print(f"{'='*60}")
    
    def run_continuous_scraping(self):
        """Main continuous scraping loop."""
        print(f"\n🚀 Starting unified Reddit scraping for r/{self.subreddit_name}")
        print(f"⏰ Scrape interval: {self.config['scrape_interval']} seconds")
        print(f"📊 Posts per scrape: {self.config['posts_limit']}")
        print(f"💬 Comments batch size: {self.config['posts_per_comment_batch']} posts")
        print(f"🏢 Subreddit metadata interval: {self.config['subreddit_update_interval']/3600:.1f} hours")
        print("Press Ctrl+C to stop\n")
        
        try:
            while True:
                self.cycle_count += 1
                start_time = time.time()
                
                print(f"\n{'='*80}")
                print(f"SCRAPE CYCLE #{self.cycle_count} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*80}")
                
                # PHASE 1: Scrape Posts
                print(f"\n{'='*60}")
                print("POST SCRAPING PHASE")
                print(f"{'='*60}")
                
                posts = self.scrape_hot_posts(self.config["posts_limit"])
                new_posts = self.save_posts_to_db(posts)
                
                # PHASE 2: Scrape Comments
                posts_processed, total_comments = self.scrape_comments_for_posts()
                
                # PHASE 3: Update Subreddit Metadata (if needed)
                print(f"\n{'='*60}")
                print("SUBREDDIT METADATA PHASE")
                print(f"{'='*60}")
                
                subreddit_updated = self.update_subreddit_metadata_if_needed()
                
                # Summary
                elapsed_time = time.time() - start_time
                print(f"\n{'='*60}")
                print("CYCLE SUMMARY")
                print(f"{'='*60}")
                print(f"Posts scraped: {len(posts)} ({new_posts} new)")
                print(f"Comments processed: {posts_processed} posts, {total_comments} new comments")
                print(f"Subreddit metadata: {'Updated' if subreddit_updated else 'No update needed'}")
                print(f"Cycle completed in {elapsed_time:.2f} seconds")
                
                # Wait before next cycle
                print(f"\nWaiting {self.config['scrape_interval']} seconds before next cycle...")
                time.sleep(self.config["scrape_interval"])
                
        except KeyboardInterrupt:
            print(f"\n\nScraping stopped by user after {self.cycle_count} cycles")
        except Exception as e:
            print(f"Unexpected error: {e}")
            print("Restarting in 60 seconds...")
            time.sleep(60)
            self.run_continuous_scraping()


def main():
    parser = argparse.ArgumentParser(description="Unified Reddit Scraper")
    parser.add_argument("subreddit", help="Subreddit name to scrape (without r/)")
    parser.add_argument("--stats", action="store_true", help="Show statistics only")
    parser.add_argument("--comments-only", action="store_true", help="Run comment scraping only")
    parser.add_argument("--metadata-only", action="store_true", help="Update subreddit metadata only")
    parser.add_argument("--posts-limit", type=int, default=1000, help="Number of posts to scrape per cycle")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between scrape cycles")
    parser.add_argument("--comment-batch", type=int, default=20, help="Number of posts to process for comments per cycle")
    
    args = parser.parse_args()
    
    # Custom configuration from command line
    config = {
        "posts_limit": args.posts_limit,
        "scrape_interval": args.interval,
        "posts_per_comment_batch": args.comment_batch,
    }
    
    # Create scraper instance
    scraper = UnifiedRedditScraper(args.subreddit, config)
    
    if args.stats:
        scraper.print_stats()
    elif args.comments_only:
        print(f"Running comment scraping only for r/{args.subreddit}...")
        posts_processed, total_comments = scraper.scrape_comments_for_posts()
        print(f"Completed: {posts_processed} posts, {total_comments} comments")
    elif args.metadata_only:
        print(f"Updating subreddit metadata for r/{args.subreddit}...")
        updated = scraper.update_subreddit_metadata_if_needed()
        print(f"Metadata: {'Updated' if updated else 'No update needed'}")
    else:
        # Show initial stats then run continuous scraping
        scraper.print_stats()
        scraper.run_continuous_scraping()


if __name__ == "__main__":
    main() 