import praw
from datetime import datetime, UTC
from dotenv import load_dotenv
import pymongo
import os
import time
import logging
from rate_limits import check_rate_limit
from praw.models import MoreComments

# Import centralized configuration
from config import DATABASE_NAME, COLLECTIONS, LOGGING_CONFIG

load_dotenv()

# Configure logging
logging.basicConfig(
    format=LOGGING_CONFIG["format"],
    datefmt=LOGGING_CONFIG["date_format"],
    level=getattr(logging, LOGGING_CONFIG["level"]),
    force=True
)
logger = logging.getLogger("get-comments")

# MongoDB setup using centralized config
client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client[DATABASE_NAME]  # Uses "noldo" from config
posts_collection = db[COLLECTIONS["POSTS"]]
comments_collection = db[COLLECTIONS["COMMENTS"]]
errors_collection = db[COLLECTIONS["SCRAPE_ERRORS"]]


reddit = praw.Reddit(
    client_id=os.getenv("R_CLIENT_ID"),
    client_secret=os.getenv("R_CLIENT_SECRET"),
    username=os.getenv("R_USERNAME"),
    password=os.getenv("R_PASSWORD"),
    user_agent=os.getenv("R_USER_AGENT")
)


logger.info(f"Authenticated as: {reddit.user.me()}")


SCRAPE_INTERVAL = 600  # 10 minutes between comment scrapes


def log_scrape_error(post_id, error_type, error_message):
    """
    Log scraping errors to MongoDB for tracking and debugging.

    Args:
        post_id: Post ID that failed
        error_type: Type of error (e.g., 'scrape_failed', 'save_failed', 'verification_failed')
        error_message: Error message/details
    """
    try:
        error_doc = {
            "post_id": post_id,
            "error_type": error_type,
            "error_message": str(error_message),
            "timestamp": datetime.now(UTC),
            "resolved": False,
            "script": "get_comments.py"
        }
        errors_collection.insert_one(error_doc)
        logger.info(f"Logged error to database: {error_type}")
    except Exception as e:
        logger.error(f"Failed to log error to database: {e}")


def get_posts_without_comments():
    """
    Get posts from database that haven't had their comments scraped yet.
    
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
        }).limit(50))  # Process 50 posts at a time


        logger.info(f"Found {len(posts)} posts without comments scraped")
        return posts

    except Exception as e:
        logger.error(f"Error fetching posts: {e}")
        return []


def scrape_post_comments(post_id, submission_url=None):
    """
    Scrape comments for a specific post with tree structure preservation.
    
    Args:
        post_id (str): Reddit post ID
        submission_url (str): Optional URL of the submission
    
    Returns:
        int: Number of comments scraped
    """
    logger.info(f"\n--- Scraping comments for post {post_id} ---")

    # Check rate limits before making API calls
    check_rate_limit(reddit)
    
    try:
        # Get the submission
        submission = reddit.submission(id=post_id)

        # Expand ALL MoreComments to get complete thread
        try:
            removed_count = submission.comments.replace_more(limit=None)
            if removed_count:
                logger.info(f"Expanded {len(removed_count)} MoreComments objects")
        except Exception as e:
            logger.warning(f"Error expanding MoreComments: {e}, continuing with partial comment tree")

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
                    "scraped_at": datetime.now(UTC),
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
                logger.error(f"Error processing comment {getattr(comment, 'id', 'unknown')}: {e}")
        
        # Process all top-level comments
        for comment in submission.comments:
            process_comment(comment, parent_id=None, depth=0)

        logger.info(f"Processed {len(comments_data)} comments for post {post_id}")
        return comments_data

    except Exception as e:
        logger.error(f"Error scraping comments for post {post_id}: {e}")
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
        # Create index on comment_id for efficient duplicate checking
        comments_collection.create_index("comment_id", unique=True)
        comments_collection.create_index("post_id")  # For efficient post-based queries
        comments_collection.create_index("parent_id")  # For efficient tree reconstruction
        
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

        logger.info(f"Saved {new_comments} new comments to database")
        return new_comments

    except Exception as e:
        logger.error(f"Error saving comments to database: {e}")
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
        logger.error(f"Error marking post {post_id} as scraped: {e}")


def continuous_comment_scrape():
    """
    Continuously scrape comments for posts that haven't been processed yet.
    """
    logger.info("Starting continuous comment scraping")
    logger.info(f"Scrape interval: {SCRAPE_INTERVAL} seconds")
    logger.info("Press Ctrl+C to stop\n")

    scrape_count = 0
    
    try:
        while True:
            scrape_count += 1
            start_time = time.time()

            logger.info(f"\n=== Comment Scrape #{scrape_count} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

            # Get posts that need comment scraping
            posts = get_posts_without_comments()

            if not posts:
                logger.info("No posts found that need comment scraping. Waiting...")
                time.sleep(SCRAPE_INTERVAL)
                continue
            
            total_comments = 0
            posts_processed = 0
            
            for post in posts:
                post_id = post["post_id"]
                try:
                    logger.info(f"\nProcessing post: {post['title'][:50]}...")

                    # Scrape comments for this post
                    comments = scrape_post_comments(post_id)

                    if not comments:
                        logger.warning(f"No comments found for post {post_id} - NOT marking as scraped")
                        # Don't mark as scraped - will retry later
                        continue

                    # Save comments to database
                    new_comments = save_comments_to_db(comments)

                    if new_comments == 0 and comments:
                        logger.error(f"Failed to save comments for post {post_id}")
                        # Log error to database
                        log_scrape_error(post_id, "save_failed", "Comments scraped but not saved to DB")
                        continue  # Don't mark as scraped

                    # VERIFICATION: Check comments actually in database
                    actual_count = comments_collection.count_documents({"post_id": post_id})

                    if actual_count > 0:
                        # Comments verified in DB, safe to mark as scraped
                        mark_post_comments_scraped(post_id)
                        posts_processed += 1
                        total_comments += new_comments
                        logger.info(f"✓ Verified {actual_count} comments in DB for post {post_id}")
                    else:
                        # Verification failed!
                        logger.error(f"✗ VERIFICATION FAILED: 0 comments in DB for post {post_id}")
                        log_scrape_error(post_id, "verification_failed", f"Expected {len(comments)} comments but found 0 in DB")
                        continue  # Will retry later

                    # Small delay between posts to be respectful
                    time.sleep(2)

                except Exception as e:
                    logger.error(f"Error processing post {post_id}: {e}")
                    log_scrape_error(post_id, "scrape_failed", str(e))
                    continue  # Don't mark as scraped on error


            # Calculate time taken
            elapsed_time = time.time() - start_time
            logger.info(f"\nScrape completed in {elapsed_time:.2f} seconds")
            logger.info(f"Processed {posts_processed} posts, scraped {total_comments} new comments")

            # Wait before next scrape
            logger.info(f"Waiting {SCRAPE_INTERVAL} seconds before next scrape...")
            time.sleep(SCRAPE_INTERVAL)

    except KeyboardInterrupt:
        logger.info(f"\n\nComment scraping stopped by user after {scrape_count} scrapes")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        logger.info("Restarting in 60 seconds...")
        time.sleep(60)
        continuous_comment_scrape()  # Restart on error


def get_comment_tree(post_id):
    """
    Utility function to reconstruct comment tree for a specific post.
    
    Args:
        post_id (str): Reddit post ID
    
    Returns:
        dict: Nested comment tree structure
    """
    try:
        # Get all comments for the post
        comments = list(comments_collection.find({"post_id": post_id}))
        
        # Create a dictionary for quick lookup
        comment_dict = {comment["comment_id"]: comment for comment in comments}
        
        # Build the tree structure
        tree = []
        
        for comment in comments:
            if comment["parent_type"] == "post":
                # Top-level comment
                comment["replies"] = []
                tree.append(comment)
            else:
                # Reply to another comment
                parent_id = comment["parent_id"]
                if parent_id in comment_dict:
                    if "replies" not in comment_dict[parent_id]:
                        comment_dict[parent_id]["replies"] = []
                    comment_dict[parent_id]["replies"].append(comment)
        
        return tree

    except Exception as e:
        logger.error(f"Error reconstructing comment tree: {e}")
        return []


if __name__ == "__main__":
    # Run continuous comment scraping
    continuous_comment_scrape() 