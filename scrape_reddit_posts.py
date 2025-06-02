import praw
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv
import pymongo
import os
import time
from rate_limits import check_rate_limit
from praw.models import MoreComments

# Import centralized configuration
from config import DATABASE_NAME, COLLECTIONS, DEFAULT_SCRAPER_CONFIG

load_dotenv()

# MongoDB setup using centralized config
client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client[DATABASE_NAME]
posts_collection = db[COLLECTIONS["POSTS"]]
comments_collection = db[COLLECTIONS["COMMENTS"]]

reddit = praw.Reddit(
    client_id=os.getenv("R_CLIENT_ID"),
    client_secret=os.getenv("R_CLIENT_SECRET"),
    username=os.getenv("R_USERNAME"),
    password=os.getenv("R_PASSWORD"),
    user_agent=os.getenv("R_USER_AGENT")
)

print(f"Authenticated as: {reddit.user.me()}")

# Configuration using centralized values
SUB = "wallstreetbets"
SCRAPE_INTERVAL = 10  # 5 minutes between full cycles
POSTS_LIMIT = DEFAULT_SCRAPER_CONFIG["posts_limit"]
POSTS_PER_COMMENT_BATCH = DEFAULT_SCRAPER_CONFIG["posts_per_comment_batch"]


def scrape_hot_posts(subreddit_name, limit=1000):
    """
    Scrape hot posts from a subreddit with rate limiting.
    
    Args:
        subreddit_name (str): Name of the subreddit
        limit (int): Number of posts to fetch
    
    Returns:
        list: List of post dictionaries
    """
    print(f"\n--- Scraping {limit} hot posts from r/{subreddit_name} ---")
    
    # Check rate limits before making API calls
    check_rate_limit(reddit)
    
    try:
        posts = reddit.subreddit(subreddit_name).hot(limit=limit)
        posts_list = []
        
        for i, post in enumerate(posts):
            # Check rate limits every 100 posts
            if i % 100 == 0 and i > 0:
                check_rate_limit(reddit)
            
            post_data = {
                "title": post.title,
                "url": post.url,
                "reddit_url": f"https://reddit.com{post.permalink}",  # Full Reddit post URL
                "score": post.score,
                "num_comments": post.num_comments,
                "created_utc": post.created_utc,
                "created_datetime": datetime.fromtimestamp(post.created_utc),
                "author": str(post.author) if post.author else "[deleted]",
                "subreddit": subreddit_name,
                "post_id": post.id,
                "scraped_at": datetime.now(UTC),
                "selftext": post.selftext[:1000] if post.selftext else "",  # Limit text length
                "is_self": post.is_self,
                "upvote_ratio": post.upvote_ratio,
                "distinguished": post.distinguished,
                "stickied": post.stickied,
                "over_18": post.over_18,
                "spoiler": post.spoiler,
                "locked": post.locked,
                "comments_scraped": False,  # Initialize as not scraped
                "last_comment_fetch_time": None,  # Track when comments were last fetched
                "initial_comments_scraped": False  # Track if we've done the initial scrape
            }
            posts_list.append(post_data)
        
        print(f"Successfully scraped {len(posts_list)} posts")
        return posts_list
        
    except Exception as e:
        print(f"Error scraping posts: {e}")
        return []


def save_posts_to_db(posts_list):
    """
    Save posts to MongoDB with bulk operations for better performance.
    
    Args:
        posts_list (list): List of post dictionaries
    
    Returns:
        int: Number of new posts inserted
    """
    if not posts_list:
        return 0
    
    try:
        # Create index on post_id for efficient duplicate checking
        posts_collection.create_index("post_id", unique=True)
        
        # Get existing posts to preserve comments_scraped status
        post_ids = [post["post_id"] for post in posts_list]
        existing_posts = {
            doc["post_id"]: doc 
            for doc in posts_collection.find(
                {"post_id": {"$in": post_ids}}, 
                {"post_id": 1, "comments_scraped": 1, "comments_scraped_at": 1, 
                 "last_comment_fetch_time": 1, "initial_comments_scraped": 1}
            )
        }
        
        # Prepare bulk operations
        bulk_operations = []
        
        for post in posts_list:
            post_id = post["post_id"]
            
            # Preserve existing comment tracking fields if they exist
            if post_id in existing_posts:
                existing = existing_posts[post_id]
                post["comments_scraped"] = existing.get("comments_scraped", False)
                post["comments_scraped_at"] = existing.get("comments_scraped_at")
                post["last_comment_fetch_time"] = existing.get("last_comment_fetch_time")
                post["initial_comments_scraped"] = existing.get("initial_comments_scraped", False)
            
            # Add upsert operation to bulk
            bulk_operations.append(
                pymongo.UpdateOne(
                    {"post_id": post_id},
                    {"$set": post},
                    upsert=True
                )
            )
        
        # Execute bulk operation
        if bulk_operations:
            result = posts_collection.bulk_write(bulk_operations, ordered=False)
            new_posts = result.upserted_count
            modified_posts = result.modified_count
            
            print(f"Bulk operation completed: {new_posts} new posts, {modified_posts} updated posts")
            return new_posts
        else:
            return 0
        
    except Exception as e:
        print(f"Error saving posts to database: {e}")
        return 0


def get_posts_needing_comment_updates(limit=20):
    """
    Get posts that need comment scraping or updates.
    Prioritizes:
    1. Posts that have never been scraped
    2. Posts that haven't been updated in the last 6 hours (for active posts)
    3. Posts that haven't been updated in 24 hours (for older posts)
    
    Args:
        limit (int): Maximum number of posts to return
    
    Returns:
        list: List of post documents that need comment updates
    """
    try:
        current_time = datetime.now(UTC)
        six_hours_ago = current_time - timedelta(hours=6)
        twenty_four_hours_ago = current_time - timedelta(hours=24)
        
        # Convert to timezone-naive for database comparison (since existing data might be timezone-naive)
        current_time_naive = current_time.replace(tzinfo=None)
        six_hours_ago_naive = six_hours_ago.replace(tzinfo=None)
        twenty_four_hours_ago_naive = twenty_four_hours_ago.replace(tzinfo=None)
        
        # Query for posts needing updates, prioritized by urgency
        posts = list(posts_collection.find({
            "$or": [
                # Never had initial comments scraped
                {"initial_comments_scraped": {"$ne": True}},
                # Recent posts (< 24h old) that haven't been updated in 6 hours  
                {
                    "created_datetime": {"$gte": twenty_four_hours_ago_naive},
                    "$or": [
                        {"last_comment_fetch_time": {"$exists": False}},
                        {"last_comment_fetch_time": {"$lte": six_hours_ago_naive}},
                        {"last_comment_fetch_time": None}
                    ]
                },
                # Older posts that haven't been updated in 24 hours
                {
                    "created_datetime": {"$lt": twenty_four_hours_ago_naive},
                    "$or": [
                        {"last_comment_fetch_time": {"$exists": False}},
                        {"last_comment_fetch_time": {"$lte": twenty_four_hours_ago_naive}},
                        {"last_comment_fetch_time": None}
                    ]
                }
            ]
        }).sort([
            ("initial_comments_scraped", 1),  # Unscraped posts first
            ("created_utc", -1)               # Then newest first
        ]).limit(limit))
        
        print(f"Found {len(posts)} posts needing comment updates")
        return posts
        
    except Exception as e:
        print(f"Error fetching posts needing updates: {e}")
        return []


def get_existing_comment_ids(post_id):
    """
    Get existing comment IDs for a post to avoid duplicates.
    
    Args:
        post_id (str): Reddit post ID
    
    Returns:
        set: Set of existing comment IDs
    """
    try:
        existing_comments = comments_collection.find(
            {"post_id": post_id}, 
            {"comment_id": 1}
        )
        return {doc["comment_id"] for doc in existing_comments}
    except Exception as e:
        print(f"Error getting existing comment IDs for post {post_id}: {e}")
        return set()


def scrape_post_comments(post_id):
    """
    Scrape comments for a specific post, only collecting new comments not already in DB.
    
    Args:
        post_id (str): Reddit post ID
    
    Returns:
        list: List of new comment dictionaries
    """
    print(f"\n--- Scraping comments for post {post_id} ---")
    
    # Check rate limits before making API calls
    check_rate_limit(reddit)
    
    try:
        # Get existing comment IDs to avoid duplicates
        existing_comment_ids = get_existing_comment_ids(post_id)
        print(f"Found {len(existing_comment_ids)} existing comments for this post")
        
        # Get the submission
        submission = reddit.submission(id=post_id)
        
        # Replace "MoreComments" objects with actual comments (limited)
        submission.comments.replace_more(limit=10)  # Limit to avoid too many API calls
        
        comments_data = []
        new_comments_count = 0
        
        def process_comment(comment, parent_id=None, depth=0):
            """Recursively process comments and their replies"""
            nonlocal new_comments_count
            
            if isinstance(comment, MoreComments):
                return
            
            try:
                # Skip if comment already exists in database
                if comment.id in existing_comment_ids:
                    # Still process replies in case there are new ones
                    if hasattr(comment, 'replies') and comment.replies:
                        for reply in comment.replies:
                            process_comment(reply, parent_id=comment.id, depth=depth + 1)
                    return
                
                # This is a new comment, process it
                comment_data = {
                    "comment_id": comment.id,
                    "post_id": post_id,
                    "parent_id": parent_id,  # None for top-level comments, comment_id for replies
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
                    "scraped_at": datetime.now(UTC),
                    "subreddit": submission.subreddit.display_name,
                    "gilded": comment.gilded if hasattr(comment, 'gilded') else 0,
                    "total_awards_received": comment.total_awards_received if hasattr(comment, 'total_awards_received') else 0
                }
                
                comments_data.append(comment_data)
                new_comments_count += 1
                
                # Process replies recursively
                if hasattr(comment, 'replies') and comment.replies:
                    for reply in comment.replies:
                        process_comment(reply, parent_id=comment.id, depth=depth + 1)
                        
            except Exception as e:
                print(f"Error processing comment {getattr(comment, 'id', 'unknown')}: {e}")
        
        # Process all top-level comments
        for comment in submission.comments:
            process_comment(comment, parent_id=None, depth=0)
        
        print(f"Found {new_comments_count} new comments (out of {len(comments_data)} processed)")
        return comments_data
        
    except Exception as e:
        print(f"Error scraping comments for post {post_id}: {e}")
        return []


def save_comments_to_db(comments_list):
    """
    Save comments to MongoDB with bulk operations for better performance.
    
    Args:
        comments_list (list): List of comment dictionaries
    
    Returns:
        int: Number of new comments inserted
    """
    if not comments_list:
        return 0
    
    try:
        # Create indexes for efficient operations
        comments_collection.create_index("comment_id", unique=True)
        comments_collection.create_index("post_id")
        comments_collection.create_index("parent_id")
        
        # Prepare bulk operations
        bulk_operations = []
        
        for comment in comments_list:
            bulk_operations.append(
                pymongo.UpdateOne(
                    {"comment_id": comment["comment_id"]},
                    {"$set": comment},
                    upsert=True
                )
            )
        
        # Execute bulk operation
        if bulk_operations:
            result = comments_collection.bulk_write(bulk_operations, ordered=False)
            new_comments = result.upserted_count
            modified_comments = result.modified_count
            
            print(f"Bulk operation completed: {new_comments} new comments, {modified_comments} updated comments")
            return new_comments
        else:
            return 0
        
    except Exception as e:
        print(f"Error saving comments to database: {e}")
        return 0


def mark_post_comments_scraped(post_id):
    """
    Mark a post as having its comments scraped.
    
    Args:
        post_id (str): Reddit post ID
    """
    try:
        posts_collection.update_one(
            {"post_id": post_id},
            {"$set": {
                "comments_scraped": True,
                "comments_scraped_at": datetime.now(UTC)
            }}
        )
    except Exception as e:
        print(f"Error marking post {post_id} as scraped: {e}")


def mark_posts_comments_updated(post_ids, is_initial_scrape=False):
    """
    Mark posts as having their comments updated and record the fetch time.
    
    Args:
        post_ids (list): List of Reddit post IDs
        is_initial_scrape (bool): Whether this was the first time scraping comments
    """
    if not post_ids:
        return
    
    try:
        # Prepare bulk operations
        bulk_operations = []
        update_time = datetime.now(UTC)
        
        for post_id in post_ids:
            update_data = {
                "comments_scraped": True,
                "last_comment_fetch_time": update_time
            }
            
            # Mark initial scrape as complete if this is the first time
            if is_initial_scrape:
                update_data["initial_comments_scraped"] = True
                update_data["comments_scraped_at"] = update_time
            
            bulk_operations.append(
                pymongo.UpdateOne(
                    {"post_id": post_id},
                    {"$set": update_data}
                )
            )
        
        # Execute bulk operation
        if bulk_operations:
            result = posts_collection.bulk_write(bulk_operations, ordered=False)
            action = "initially scraped" if is_initial_scrape else "updated"
            print(f"Marked {result.modified_count} posts as comments {action}")
        
    except Exception as e:
        print(f"Error marking posts as updated: {e}")


def scrape_comments_for_posts():
    """
    Scrape comments for posts that need updates (initial scrape or periodic refresh).
    
    Returns:
        tuple: (posts_processed, total_comments)
    """
    print(f"\n{'='*60}")
    print("COMMENT SCRAPING PHASE")
    print(f"{'='*60}")
    
    # Get posts that need comment scraping or updates
    posts = get_posts_needing_comment_updates(POSTS_PER_COMMENT_BATCH)
    
    if not posts:
        print("No posts found that need comment updates.")
        return 0, 0
    
    total_comments = 0
    posts_processed = 0
    initial_scrape_posts = []
    update_posts = []
    all_comments = []
    
    # Process all posts and collect comments
    for post in posts:
        try:
            post_id = post["post_id"]
            is_initial = not post.get("initial_comments_scraped", False)
            action = "Initial scrape" if is_initial else "Update"
            
            print(f"\n{action} for post: {post['title'][:50]}...")
            
            # Scrape comments for this post (will only get new ones)
            comments = scrape_post_comments(post_id)
            
            if comments or is_initial:  # Always mark initial scrapes even if no comments
                all_comments.extend(comments)
                if is_initial:
                    initial_scrape_posts.append(post_id)
                else:
                    update_posts.append(post_id)
                posts_processed += 1
            
            # Small delay between posts to be respectful
            time.sleep(2)
            
        except Exception as e:
            print(f"Error processing post {post.get('post_id', 'unknown')}: {e}")
            continue
    
    # Bulk save all comments at once
    if all_comments:
        new_comments = save_comments_to_db(all_comments)
        total_comments = new_comments
    
    # Bulk mark posts as updated with appropriate flags
    if initial_scrape_posts:
        mark_posts_comments_updated(initial_scrape_posts, is_initial_scrape=True)
    if update_posts:
        mark_posts_comments_updated(update_posts, is_initial_scrape=False)
    
    initial_count = len(initial_scrape_posts)
    update_count = len(update_posts)
    print(f"\nComment scraping completed: {posts_processed} posts ({initial_count} initial, {update_count} updates), {total_comments} new comments")
    return posts_processed, total_comments


def continuous_scrape():
    """
    Continuously scrape posts and comments in the correct order.
    """
    print(f"Starting unified Reddit scraping for r/{SUB}")
    print(f"Scrape interval: {SCRAPE_INTERVAL} seconds")
    print(f"Posts per scrape: {POSTS_LIMIT}")
    print(f"Comments batch size: {POSTS_PER_COMMENT_BATCH} posts")
    print("Press Ctrl+C to stop\n")
    
    cycle_count = 0
    
    try:
        while True:
            cycle_count += 1
            start_time = time.time()
            
            print(f"\n{'='*80}")
            print(f"SCRAPE CYCLE #{cycle_count} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*80}")
            
            # PHASE 1: Scrape Posts
            print(f"\n{'='*60}")
            print("POST SCRAPING PHASE")
            print(f"{'='*60}")
            
            posts = scrape_hot_posts(SUB, POSTS_LIMIT)
            new_posts = save_posts_to_db(posts)
            
            # PHASE 2: Scrape Comments for existing posts
            posts_processed, total_comments = scrape_comments_for_posts()
            
            # Calculate time taken
            elapsed_time = time.time() - start_time
            
            # Summary
            print(f"\n{'='*60}")
            print("CYCLE SUMMARY")
            print(f"{'='*60}")
            print(f"Posts scraped: {len(posts)} ({new_posts} new)")
            print(f"Comments processed: {posts_processed} posts, {total_comments} new comments")
            print(f"Cycle completed in {elapsed_time:.2f} seconds")
            
            # Wait before next cycle
            print(f"\nWaiting {SCRAPE_INTERVAL} seconds before next cycle...")
            time.sleep(SCRAPE_INTERVAL)
            
    except KeyboardInterrupt:
        print(f"\n\nScraping stopped by user after {cycle_count} cycles")
    except Exception as e:
        print(f"Unexpected error: {e}")
        print("Restarting in 60 seconds...")
        time.sleep(60)
        continuous_scrape()  # Restart on error


def get_scraping_stats():
    """
    Get current scraping statistics.
    
    Returns:
        dict: Statistics about scraped data
    """
    try:
        total_posts = posts_collection.count_documents({})
        posts_with_initial_comments = posts_collection.count_documents({"initial_comments_scraped": True})
        posts_without_initial_comments = posts_collection.count_documents({
            "$or": [
                {"initial_comments_scraped": {"$exists": False}},
                {"initial_comments_scraped": False}
            ]
        })
        posts_with_recent_updates = posts_collection.count_documents({
            "last_comment_fetch_time": {"$gte": (datetime.now(UTC) - timedelta(hours=24)).replace(tzinfo=None)}
        })
        total_comments = comments_collection.count_documents({})
        
        return {
            "total_posts": total_posts,
            "posts_with_initial_comments": posts_with_initial_comments,
            "posts_without_initial_comments": posts_without_initial_comments,
            "posts_with_recent_updates": posts_with_recent_updates,
            "total_comments": total_comments,
            "initial_completion_rate": (posts_with_initial_comments / total_posts * 100) if total_posts > 0 else 0
        }
    except Exception as e:
        print(f"Error getting stats: {e}")
        return {}


def print_stats():
    """Print current scraping statistics."""
    stats = get_scraping_stats()
    if stats:
        print(f"\n{'='*50}")
        print("CURRENT SCRAPING STATISTICS")
        print(f"{'='*50}")
        print(f"Total posts: {stats['total_posts']}")
        print(f"Posts with initial comments scraped: {stats['posts_with_initial_comments']}")
        print(f"Posts without initial comments: {stats['posts_without_initial_comments']}")
        print(f"Posts with recent updates: {stats['posts_with_recent_updates']}")
        print(f"Total comments: {stats['total_comments']}")
        print(f"Initial completion rate: {stats['initial_completion_rate']:.1f}%")
        print(f"{'='*50}")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--stats":
        # Show statistics only
        print_stats()
    elif len(sys.argv) > 1 and sys.argv[1] == "--comments-only":
        # Run comment scraping only
        print("Running comment scraping only...")
        posts_processed, total_comments = scrape_comments_for_posts()
        print(f"Completed: {posts_processed} posts, {total_comments} comments")
    else:
        # Run full continuous scraping
        print_stats()  # Show initial stats
        continuous_scrape() 