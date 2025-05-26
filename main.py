import praw
from datetime import datetime
from dotenv import load_dotenv
import pymongo
import os
import time
from rate_limits import check_rate_limit


load_dotenv()

client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client["techbro"]
collection = db["reddit_posts"]


reddit = praw.Reddit(
    client_id=os.getenv("R_CLIENT_ID"),
    client_secret=os.getenv("R_CLIENT_SECRET"),
    username=os.getenv("R_USERNAME"),
    password=os.getenv("R_PASSWORD"),
    user_agent=os.getenv("R_USER_AGENT")
)


print(f"Authenticated as: {reddit.user.me()}")


SUB = "wallstreetbets"
SCRAPE_INTERVAL = 300  # 5 minutes between scrapes
POSTS_LIMIT = 1000


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
                "locked": post.locked
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
        collection.create_index("post_id", unique=True)
        
        new_posts = 0
        for post in posts_list:
            try:
                # Use upsert to avoid duplicates
                result = collection.update_one(
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
        print(f"Error saving to database: {e}")
        return 0


def continuous_scrape():
    """
    Continuously scrape hot posts with rate limiting and error handling.
    """
    print(f"Starting continuous scraping of r/{SUB}")
    print(f"Scrape interval: {SCRAPE_INTERVAL} seconds")
    print(f"Posts per scrape: {POSTS_LIMIT}")
    print("Press Ctrl+C to stop\n")
    
    scrape_count = 0
    
    try:
        while True:
            scrape_count += 1
            start_time = time.time()
            
            print(f"\n=== Scrape #{scrape_count} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
            
            # Scrape posts
            posts = scrape_hot_posts(SUB, POSTS_LIMIT)
            
            # Save to database
            new_posts = save_posts_to_db(posts)
            
            # Calculate time taken
            elapsed_time = time.time() - start_time
            print(f"Scrape completed in {elapsed_time:.2f} seconds")
            
            # Wait before next scrape
            print(f"Waiting {SCRAPE_INTERVAL} seconds before next scrape...")
            time.sleep(SCRAPE_INTERVAL)
            
    except KeyboardInterrupt:
        print(f"\n\nScraping stopped by user after {scrape_count} scrapes")
    except Exception as e:
        print(f"Unexpected error: {e}")
        print("Restarting in 60 seconds...")
        time.sleep(60)
        continuous_scrape()  # Restart on error


if __name__ == "__main__":
    # Run continuous scraping
    continuous_scrape()