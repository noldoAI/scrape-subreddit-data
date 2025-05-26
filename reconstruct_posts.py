import pymongo
import os
from datetime import datetime
from dotenv import load_dotenv
import json
from typing import Dict, List, Optional


load_dotenv()

client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client["techbro"]
posts_collection = db["reddit_posts"]
comments_collection = db["reddit_comments"]


def get_post_by_id(post_id: str) -> Optional[Dict]:
    """
    Get a post by its ID from the database.
    
    Args:
        post_id (str): Reddit post ID
    
    Returns:
        dict: Post data or None if not found
    """
    try:
        post = posts_collection.find_one({"post_id": post_id})
        return post
    except Exception as e:
        print(f"Error fetching post {post_id}: {e}")
        return None


def get_comment_tree(post_id: str) -> List[Dict]:
    """
    Reconstruct comment tree for a specific post.
    
    Args:
        post_id (str): Reddit post ID
    
    Returns:
        list: Nested comment tree structure
    """
    try:
        # Get all comments for the post, sorted by creation time
        comments = list(comments_collection.find(
            {"post_id": post_id}
        ).sort("created_utc", 1))
        
        if not comments:
            return []
        
        # Create a dictionary for quick lookup
        comment_dict = {comment["comment_id"]: comment for comment in comments}
        
        # Initialize replies list for each comment
        for comment in comments:
            comment["replies"] = []
        
        # Build the tree structure
        tree = []
        
        for comment in comments:
            if comment["parent_type"] == "post":
                # Top-level comment
                tree.append(comment)
            else:
                # Reply to another comment
                parent_id = comment["parent_id"]
                if parent_id in comment_dict:
                    comment_dict[parent_id]["replies"].append(comment)
        
        # Sort tree by score (highest first) and replies recursively
        def sort_comments(comment_list):
            comment_list.sort(key=lambda x: x.get("score", 0), reverse=True)
            for comment in comment_list:
                if comment["replies"]:
                    sort_comments(comment["replies"])
        
        sort_comments(tree)
        return tree
        
    except Exception as e:
        print(f"Error reconstructing comment tree for post {post_id}: {e}")
        return []


def format_post_text(post: Dict) -> str:
    """
    Format post information as readable text.
    
    Args:
        post (dict): Post data
    
    Returns:
        str: Formatted post text
    """
    if not post:
        return "Post not found"
    
    # Format creation time
    created_time = "Unknown"
    if post.get("created_datetime"):
        if isinstance(post["created_datetime"], datetime):
            created_time = post["created_datetime"].strftime("%Y-%m-%d %H:%M:%S UTC")
        else:
            created_time = str(post["created_datetime"])
    
    # Build post text
    post_text = f"""
{'='*80}
REDDIT POST
{'='*80}

Title: {post.get('title', 'No title')}
Author: u/{post.get('author', '[deleted]')}
Subreddit: r/{post.get('subreddit', 'unknown')}
Url: {post.get('url', 'No URL')}
Score: {post.get('score', 0)} points
Comments: {post.get('num_comments', 0)}
Created: {created_time}
URL: {post.get('url', 'No URL')}

"""
    
    # Add post content if it's a text post
    if post.get('selftext'):
        post_text += f"Content:\n{'-'*40}\n{post['selftext']}\n{'-'*40}\n"
    
    # Add metadata
    post_text += f"""
Metadata:
- Post ID: {post.get('post_id', 'unknown')}
- Upvote Ratio: {post.get('upvote_ratio', 'unknown')}
- NSFW: {post.get('over_18', False)}
- Spoiler: {post.get('spoiler', False)}
- Locked: {post.get('locked', False)}
- Stickied: {post.get('stickied', False)}

"""
    
    return post_text


def format_comment_tree(comments: List[Dict], indent_level: int = 0) -> str:
    """
    Format comment tree as readable text with proper indentation.
    
    Args:
        comments (list): List of comment dictionaries
        indent_level (int): Current indentation level
    
    Returns:
        str: Formatted comment tree text
    """
    if not comments:
        return ""
    
    comment_text = ""
    indent = "  " * indent_level  # 2 spaces per level
    
    for i, comment in enumerate(comments):
        # Format creation time
        created_time = "Unknown"
        if comment.get("created_datetime"):
            if isinstance(comment["created_datetime"], datetime):
                created_time = comment["created_datetime"].strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                created_time = str(comment["created_datetime"])
        
        # Comment header
        author = comment.get('author', '[deleted]')
        score = comment.get('score', 0)
        depth_indicator = f"[Depth {comment.get('depth', 0)}]"
        
        comment_text += f"\n{indent}┌─ Comment by u/{author} │ {score} points │ {created_time} {depth_indicator}\n"
        
        # Comment body with proper line wrapping and indentation
        body = comment.get('body', '[deleted]')
        if body:
            # Split long lines and add indentation
            lines = body.split('\n')
            for line in lines:
                # Wrap long lines at 80 characters minus indent
                max_width = 80 - len(indent) - 3  # 3 for "│  "
                if len(line) > max_width:
                    words = line.split(' ')
                    current_line = ""
                    for word in words:
                        if len(current_line + word) > max_width:
                            if current_line:
                                comment_text += f"{indent}│  {current_line}\n"
                                current_line = word
                            else:
                                comment_text += f"{indent}│  {word}\n"
                                current_line = ""
                        else:
                            current_line += (" " if current_line else "") + word
                    if current_line:
                        comment_text += f"{indent}│  {current_line}\n"
                else:
                    comment_text += f"{indent}│  {line}\n"
        
        # Add metadata for special comments
        metadata = []
        if comment.get('distinguished'):
            metadata.append(f"Distinguished: {comment['distinguished']}")
        if comment.get('stickied'):
            metadata.append("Stickied")
        if comment.get('edited'):
            metadata.append("Edited")
        if comment.get('gilded', 0) > 0:
            metadata.append(f"Gilded: {comment['gilded']}")
        if comment.get('controversiality', 0) > 0:
            metadata.append(f"Controversial: {comment['controversiality']}")
        
        if metadata:
            comment_text += f"{indent}│  [Metadata: {', '.join(metadata)}]\n"
        
        comment_text += f"{indent}└─ Comment ID: {comment.get('comment_id', 'unknown')}\n"
        
        # Process replies recursively
        if comment.get('replies'):
            comment_text += format_comment_tree(comment['replies'], indent_level + 1)
    
    return comment_text


def reconstruct_full_post(post_id: str) -> str:
    """
    Reconstruct a complete post with all comments.
    
    Args:
        post_id (str): Reddit post ID
    
    Returns:
        str: Complete formatted post with comments
    """
    print(f"Reconstructing post: {post_id}")
    
    # Get post data
    post = get_post_by_id(post_id)
    if not post:
        return f"Post {post_id} not found in database"
    
    # Get comment tree
    comments = get_comment_tree(post_id)
    
    # Format post
    full_text = format_post_text(post)
    
    # Add comments section
    if comments:
        full_text += f"\n{'='*80}\nCOMMENTS ({len(comments)} top-level comments)\n{'='*80}\n"
        full_text += format_comment_tree(comments)
    else:
        full_text += f"\n{'='*80}\nCOMMENTS\n{'='*80}\n\nNo comments found for this post.\n"
    
    return full_text


def save_post_to_file(post_id: str, output_dir: str = "reconstructed_posts") -> str:
    """
    Save reconstructed post to a text file.
    
    Args:
        post_id (str): Reddit post ID
        output_dir (str): Directory to save files
    
    Returns:
        str: Path to saved file
    """
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Reconstruct post
    post_content = reconstruct_full_post(post_id)
    
    # Create filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{post_id}_{timestamp}.txt"
    filepath = os.path.join(output_dir, filename)
    
    # Save to file
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(post_content)
        print(f"Post saved to: {filepath}")
        return filepath
    except Exception as e:
        print(f"Error saving post to file: {e}")
        return ""


def export_post_as_json(post_id: str, output_dir: str = "reconstructed_posts") -> str:
    """
    Export post and comments as structured JSON.
    
    Args:
        post_id (str): Reddit post ID
        output_dir (str): Directory to save files
    
    Returns:
        str: Path to saved JSON file
    """
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Get post and comments
    post = get_post_by_id(post_id)
    comments = get_comment_tree(post_id)
    
    if not post:
        print(f"Post {post_id} not found")
        return ""
    
    # Create structured data
    structured_data = {
        "post": post,
        "comments": comments,
        "metadata": {
            "exported_at": datetime.utcnow().isoformat(),
            "total_comments": len(comments),
            "post_id": post_id
        }
    }
    
    # Create filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{post_id}_{timestamp}.json"
    filepath = os.path.join(output_dir, filename)
    
    # Save to JSON file
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(structured_data, f, indent=2, default=str, ensure_ascii=False)
        print(f"Post exported as JSON to: {filepath}")
        return filepath
    except Exception as e:
        print(f"Error exporting post as JSON: {e}")
        return ""


def list_available_posts(limit: int = 20) -> List[Dict]:
    """
    List available posts in the database.
    
    Args:
        limit (int): Maximum number of posts to return
    
    Returns:
        list: List of post summaries
    """
    try:
        posts = list(posts_collection.find(
            {},
            {
                "post_id": 1,
                "title": 1,
                "author": 1,
                "score": 1,
                "num_comments": 1,
                "created_datetime": 1,
                "comments_scraped": 1
            }
        ).sort("created_utc", -1).limit(limit))
        
        return posts
    except Exception as e:
        print(f"Error listing posts: {e}")
        return []


def interactive_mode():
    """
    Interactive mode for reconstructing posts.
    """
    print("Reddit Post Reconstructor - Interactive Mode")
    print("=" * 50)
    
    while True:
        print("\nOptions:")
        print("1. List recent posts")
        print("2. Reconstruct post by ID")
        print("3. Save post to text file")
        print("4. Export post as JSON")
        print("5. Exit")
        
        choice = input("\nEnter your choice (1-5): ").strip()
        
        if choice == "1":
            print("\nRecent posts:")
            posts = list_available_posts()
            for i, post in enumerate(posts, 1):
                comments_status = "✓" if post.get("comments_scraped") else "✗"
                print(f"{i:2d}. [{post['post_id']}] {post.get('title', 'No title')[:60]}...")
                print(f"     Author: u/{post.get('author', '[deleted]')} | Score: {post.get('score', 0)} | Comments: {post.get('num_comments', 0)} | Scraped: {comments_status}")
        
        elif choice == "2":
            post_id = input("Enter post ID: ").strip()
            if post_id:
                content = reconstruct_full_post(post_id)
                print("\n" + content)
        
        elif choice == "3":
            post_id = input("Enter post ID: ").strip()
            if post_id:
                filepath = save_post_to_file(post_id)
                if filepath:
                    print(f"Post saved successfully!")
        
        elif choice == "4":
            post_id = input("Enter post ID: ").strip()
            if post_id:
                filepath = export_post_as_json(post_id)
                if filepath:
                    print(f"Post exported successfully!")
        
        elif choice == "5":
            print("Goodbye!")
            break
        
        else:
            print("Invalid choice. Please try again.")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        # Command line mode
        post_id = sys.argv[1]
        
        if len(sys.argv) > 2 and sys.argv[2] == "--json":
            # Export as JSON
            export_post_as_json(post_id)
        elif len(sys.argv) > 2 and sys.argv[2] == "--save":
            # Save to file
            save_post_to_file(post_id)
        else:
            # Print to console
            content = reconstruct_full_post(post_id)
            print(content)
    else:
        # Interactive mode
        interactive_mode() 