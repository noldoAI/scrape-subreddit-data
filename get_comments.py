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


SCRAPE_INTERVAL = 600  # 10 minutes between comment scrapes


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
        
        print(f"Found {len(posts)} posts without comments scraped")
        return posts
        
    except Exception as e:
        print(f"Error fetching posts: {e}")
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


def continuous_comment_scrape():
    """
    Continuously scrape comments for posts that haven't been processed yet.
    """
    print("Starting continuous comment scraping")
    print(f"Scrape interval: {SCRAPE_INTERVAL} seconds")
    print("Press Ctrl+C to stop\n")
    
    scrape_count = 0
    
    try:
        while True:
            scrape_count += 1
            start_time = time.time()
            
            print(f"\n=== Comment Scrape #{scrape_count} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
            
            # Get posts that need comment scraping
            posts = get_posts_without_comments()
            
            if not posts:
                print("No posts found that need comment scraping. Waiting...")
                time.sleep(SCRAPE_INTERVAL)
                continue
            
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
            
            # Calculate time taken
            elapsed_time = time.time() - start_time
            print(f"\nScrape completed in {elapsed_time:.2f} seconds")
            print(f"Processed {posts_processed} posts, scraped {total_comments} new comments")
            
            # Wait before next scrape
            print(f"Waiting {SCRAPE_INTERVAL} seconds before next scrape...")
            time.sleep(SCRAPE_INTERVAL)
            
    except KeyboardInterrupt:
        print(f"\n\nComment scraping stopped by user after {scrape_count} scrapes")
    except Exception as e:
        print(f"Unexpected error: {e}")
        print("Restarting in 60 seconds...")
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
        print(f"Error reconstructing comment tree: {e}")
        return []


if __name__ == "__main__":
    # Run continuous comment scraping
    continuous_comment_scrape() 