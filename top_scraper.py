#!/usr/bin/env python3
"""Scrape top posts + comments from a subreddit and save to JSON."""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import praw
from dotenv import load_dotenv
from praw.models import MoreComments

load_dotenv()


def create_reddit():
    return praw.Reddit(
        client_id=os.getenv("R_CLIENT_ID"),
        client_secret=os.getenv("R_CLIENT_SECRET"),
        username=os.getenv("R_USERNAME"),
        password=os.getenv("R_PASSWORD"),
        user_agent=os.getenv("R_USER_AGENT"),
    )


def scrape_top_posts(reddit, subreddit_name, time_filter, limit):
    subreddit = reddit.subreddit(subreddit_name)
    posts = []
    for post in subreddit.top(time_filter=time_filter, limit=limit):
        posts.append({
            "post_id": post.id,
            "title": post.title,
            "author": str(post.author) if post.author else "[deleted]",
            "score": post.score,
            "num_comments": post.num_comments,
            "created_utc": post.created_utc,
            "created_datetime": datetime.fromtimestamp(post.created_utc, tz=timezone.utc).isoformat(),
            "subreddit": subreddit_name,
            "selftext": post.selftext or "",
            "url": post.url,
            "permalink": f"https://reddit.com{post.permalink}",
            "is_self": post.is_self,
            "upvote_ratio": post.upvote_ratio,
            "over_18": post.over_18,
            "locked": post.locked,
        })
    return posts


def scrape_comments(reddit, post_id, max_depth):
    submission = reddit.submission(id=post_id)
    submission.comments.replace_more(limit=0)

    def process_comment(comment, depth=0):
        if isinstance(comment, MoreComments):
            return None
        if depth > max_depth:
            return None

        node = {
            "comment_id": comment.id,
            "author": str(comment.author) if comment.author else "[deleted]",
            "body": comment.body,
            "score": comment.score,
            "created_utc": comment.created_utc,
            "depth": depth,
            "is_submitter": comment.is_submitter,
            "parent_id": comment.parent_id,
            "replies": [],
        }

        if hasattr(comment, "replies") and depth < max_depth:
            for reply in comment.replies:
                child = process_comment(reply, depth + 1)
                if child:
                    node["replies"].append(child)

        return node

    comments = []
    for comment in submission.comments:
        node = process_comment(comment, depth=0)
        if node:
            comments.append(node)
    return comments


def count_comments(comments):
    total = len(comments)
    for c in comments:
        total += count_comments(c.get("replies", []))
    return total


def main():
    parser = argparse.ArgumentParser(description="Scrape top posts + comments to JSON")
    parser.add_argument("subreddit", help="Subreddit name")
    parser.add_argument("--time-filter", default="week", choices=["hour", "day", "week", "month", "year", "all"])
    parser.add_argument("--limit", type=int, default=30, help="Number of top posts (default: 30)")
    parser.add_argument("--max-depth", type=int, default=3, help="Max comment depth (default: 3)")
    parser.add_argument("-o", "--output", help="Output JSON file path")
    args = parser.parse_args()

    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "top_data")
    os.makedirs(output_dir, exist_ok=True)
    output_file = args.output or os.path.join(output_dir, f"{args.subreddit}_top_{args.time_filter}.json")

    print(f"Scraping top {args.limit} posts from r/{args.subreddit} ({args.time_filter})")
    reddit = create_reddit()

    posts = scrape_top_posts(reddit, args.subreddit, args.time_filter, args.limit)
    print(f"Fetched {len(posts)} posts")

    total_comments = 0
    for i, post in enumerate(posts):
        if post["num_comments"] == 0:
            post["comments"] = []
            print(f"  [{i+1}/{len(posts)}] \"{post['title'][:60]}\" — no comments")
            continue

        try:
            comments = scrape_comments(reddit, post["post_id"], args.max_depth)
            post["comments"] = comments
            n = count_comments(comments)
            total_comments += n
            print(f"  [{i+1}/{len(posts)}] \"{post['title'][:60]}\" — {n} comments")
        except Exception as e:
            post["comments"] = []
            print(f"  [{i+1}/{len(posts)}] \"{post['title'][:60]}\" — ERROR: {e}")

        time.sleep(2)

    with open(output_file, "w") as f:
        json.dump(posts, f, indent=2, default=str)

    print(f"\nDone: {len(posts)} posts, {total_comments} comments -> {output_file}")


if __name__ == "__main__":
    main()
