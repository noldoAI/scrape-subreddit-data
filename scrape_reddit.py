import praw
from datetime import datetime
from dotenv import load_dotenv
import pymongo
import os
import time
from rate_limits import check_rate_limit
from praw.models import MoreComments


load_dotenv()

client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client["techbro"]
posts_collection = db["reddit_posts"]
comments_collection = db["reddit_comments"]


reddit = praw.Reddit(
    client_id=os.getenv("R_CLIENT_ID"),
    client_secret=os.getenv("R_CLIENT_SECRET"),
    username=os.getenv("R_USERNAME"),
    password=os.getenv("R_PASSWORD"),
    user_agent=os.getenv("R_USER_AGENT")
)


print(f"Authenticated as: {reddit.user.me()}")


# Configuration
SUB = "wallstreetbets"
SCRAPE_INTERVAL = 300  # 5 minutes between full cycles
POSTS_LIMIT = 1000
POSTS_PER_COMMENT_BATCH = 20  # Process comments for 20 posts at a time


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
                "score": post.score,
                "num_comments": post.num_comments,
                "created_utc": post.created_utc,
                "created_datetime": datetime.fromtimestamp(post.created_utc),
                "author": str(post.author) if post.author else "[deleted]",
                "subreddit": subreddit_name,
                "post_id": post.id,
                "scraped_at": datetime.utcnow(),
                "selftext": post.selftext[:1000] if post.selftext else "",  # Limit text length
                "is_self": post.is_self,
                "upvote_ratio": post.upvote_ratio,
                "distinguished": post.distinguished,
                "stickied": post.stickied,
                "over_18": post.over_18,
                "spoiler": post.spoiler,
                "locked": post.locked,
                "comments_scraped": False  # Initialize as not scraped
            }
            posts_list.append(post_data)
        
        print(f"Successfully scraped {len(posts_list)} posts")
        return posts_list
        
    except Exception as e:
        print(f"Error scraping posts: {e}")
        return []


def save_posts_to_db(posts_list):
    """
    Save posts to MongoDB with duplicate handling.
    
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
        
        new_posts = 0
        for post in posts_list:
            try:
                # Use upsert to avoid duplicates, but don't overwrite comments_scraped if it's True
                existing_post = posts_collection.find_one({"post_id": post["post_id"]})
                if existing_post and existing_post.get("comments_scraped"):
                    # Keep the existing comments_scraped status
                    post["comments_scraped"] = existing_post["comments_scraped"]
                    post["comments_scraped_at"] = existing_post.get("comments_scraped_at")
                
                result = posts_collection.update_one(
                    {"post_id": post["post_id"]},
                    {"$set": post},
                    upsert=True
                )
                if result.upserted_id:
                    new_posts += 1
            except pymongo.errors.DuplicateKeyError:
                # Post already exists, skip
                continue
        
        print(f"Saved {new_posts} new posts to database")
        return new_posts
        
    except Exception as e:
        print(f"Error saving posts to database: {e}")
        return 0


def get_posts_without_comments(limit=20):
    """
    Get posts from database that haven't had their comments scraped yet.
    
    Args:
        limit (int): Maximum number of posts to return
    
    Returns:
        list: List of post documents
    """
    try:
        # Find posts that don't have comments_scraped flag or it's False
        posts = list(posts_collection.find({
            "$or": [
                {"comments_scraped": {"$exists": False}},
                {"comments_scraped": False}
            ]
        }).sort("created_utc", -1).limit(limit))  # Process newest first
        
        print(f"Found {len(posts)} posts without comments scraped")
        return posts
        
    except Exception as e:
        print(f"Error fetching posts: {e}")
        return []


def scrape_post_comments(post_id):
    """
    Scrape comments for a specific post with tree structure preservation.
    
    Args:
        post_id (str): Reddit post ID
    
    Returns:
        list: List of comment dictionaries
    """
    print(f"\n--- Scraping comments for post {post_id} ---")
    
    # Check rate limits before making API calls
    check_rate_limit(reddit)
    
    try:
        # Get the submission
        submission = reddit.submission(id=post_id)
        
        # Replace "MoreComments" objects with actual comments (limited)
        submission.comments.replace_more(limit=10)  # Limit to avoid too many API calls
        
        comments_data = []
        
        def process_comment(comment, parent_id=None, depth=0):
            """Recursively process comments and their replies"""
            if isinstance(comment, MoreComments):
                return
            
            try:
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
                    "scraped_at": datetime.utcnow(),
                    "subreddit": submission.subreddit.display_name,
                    "gilded": comment.gilded if hasattr(comment, 'gilded') else 0,
                    "total_awards_received": comment.total_awards_received if hasattr(comment, 'total_awards_received') else 0
                }
                
                comments_data.append(comment_data)
                
                # Process replies recursively
                if hasattr(comment, 'replies') and comment.replies:
                    for reply in comment.replies:
                        process_comment(reply, parent_id=comment.id, depth=depth + 1)
                        
            except Exception as e:
                print(f"Error processing comment {getattr(comment, 'id', 'unknown')}: {e}")
        
        # Process all top-level comments
        for comment in submission.comments:
            process_comment(comment, parent_id=None, depth=0)
        
        print(f"Processed {len(comments_data)} comments for post {post_id}")
        return comments_data
        
    except Exception as e:
        print(f"Error scraping comments for post {post_id}: {e}")
        return []


def save_comments_to_db(comments_list):
    """
    Save comments to MongoDB with duplicate handling.
    
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
        
        new_comments = 0
        for comment in comments_list:
            try:
                # Use upsert to avoid duplicates
                result = comments_collection.update_one(
                    {"comment_id": comment["comment_id"]},
                    {"$set": comment},
                    upsert=True
                )
                if result.upserted_id:
                    new_comments += 1
            except pymongo.errors.DuplicateKeyError:
                # Comment already exists, skip
                continue
        
        print(f"Saved {new_comments} new comments to database")
        return new_comments
        
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
                "comments_scraped_at": datetime.utcnow()
            }}
        )
    except Exception as e:
        print(f"Error marking post {post_id} as scraped: {e}")


def scrape_comments_for_posts():
    """
    Scrape comments for posts that haven't been processed yet.
    
    Returns:
        tuple: (posts_processed, total_comments)
    """
    print(f"\n{'='*60}")
    print("COMMENT SCRAPING PHASE")
    print(f"{'='*60}")
    
    # Get posts that need comment scraping
    posts = get_posts_without_comments(POSTS_PER_COMMENT_BATCH)
    
    if not posts:
        print("No posts found that need comment scraping.")
        return 0, 0
    
    total_comments = 0
    posts_processed = 0
    
    for post in posts:
        try:
            post_id = post["post_id"]
            print(f"\nProcessing post: {post['title'][:50]}...")
            
            # Scrape comments for this post
            comments = scrape_post_comments(post_id)
            
            # Save comments to database
            new_comments = save_comments_to_db(comments)
            total_comments += new_comments
            
            # Mark post as processed
            mark_post_comments_scraped(post_id)
            posts_processed += 1
            
            # Small delay between posts to be respectful
            time.sleep(2)
            
        except Exception as e:
            print(f"Error processing post {post.get('post_id', 'unknown')}: {e}")
            continue
    
    print(f"\nComment scraping completed: {posts_processed} posts, {total_comments} new comments")
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
        posts_with_comments = posts_collection.count_documents({"comments_scraped": True})
        posts_without_comments = posts_collection.count_documents({
            "$or": [
                {"comments_scraped": {"$exists": False}},
                {"comments_scraped": False}
            ]
        })
        total_comments = comments_collection.count_documents({})
        
        return {
            "total_posts": total_posts,
            "posts_with_comments": posts_with_comments,
            "posts_without_comments": posts_without_comments,
            "total_comments": total_comments,
            "completion_rate": (posts_with_comments / total_posts * 100) if total_posts > 0 else 0
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
        print(f"Posts with comments scraped: {stats['posts_with_comments']}")
        print(f"Posts without comments: {stats['posts_without_comments']}")
        print(f"Total comments: {stats['total_comments']}")
        print(f"Completion rate: {stats['completion_rate']:.1f}%")
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