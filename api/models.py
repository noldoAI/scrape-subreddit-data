"""
Pydantic models for Reddit Scraper API.
"""

from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

# Import config for defaults
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DEFAULT_SCRAPER_CONFIG, MULTI_SCRAPER_CONFIG


class RedditCredentials(BaseModel):
    client_id: str
    client_secret: str
    username: str
    password: str
    user_agent: str


class ScraperConfig(BaseModel):
    subreddit: str = ""                    # Single subreddit (backwards compat)
    subreddits: List[str] = []             # Multi-subreddit mode
    scraper_type: str = "posts"            # "posts" or "comments"
    posts_limit: int = DEFAULT_SCRAPER_CONFIG["posts_limit"]
    interval: int = DEFAULT_SCRAPER_CONFIG["scrape_interval"]
    comment_batch: int = DEFAULT_SCRAPER_CONFIG["posts_per_comment_batch"]
    sorting_methods: List[str] = DEFAULT_SCRAPER_CONFIG["sorting_methods"]
    credentials: RedditCredentials
    auto_restart: bool = True


class ScraperStatus(BaseModel):
    subreddit: str
    scraper_type: str = "posts"  # "posts" or "comments"
    status: str  # "running", "stopped", "error", "failed"
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    config: Optional[ScraperConfig] = None
    last_error: Optional[str] = None


class ScraperStartRequest(BaseModel):
    subreddit: str = ""                    # Single subreddit (backwards compat)
    subreddits: List[str] = []             # Multi-subreddit mode (up to 10)
    scraper_type: str = "posts"            # "posts" or "comments"
    posts_limit: int = DEFAULT_SCRAPER_CONFIG["posts_limit"]
    interval: int = DEFAULT_SCRAPER_CONFIG["scrape_interval"]
    comment_batch: int = DEFAULT_SCRAPER_CONFIG["posts_per_comment_batch"]
    sorting_methods: List[str] = DEFAULT_SCRAPER_CONFIG["sorting_methods"]
    auto_restart: bool = True

    # Option 1: Use saved account
    saved_account_name: Optional[str] = None

    # Option 2: Manual credentials (and optionally save them)
    credentials: Optional[RedditCredentials] = None
    save_account_as: Optional[str] = None  # If provided, save manual credentials with this name
