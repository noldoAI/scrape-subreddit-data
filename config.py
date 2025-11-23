#!/usr/bin/env python3
"""
Configuration file for Reddit Scraper system.
Centralizes all database names, collection names, and other constants.
"""

# Database Configuration
DATABASE_NAME = "noldo"

# Collection Names
COLLECTIONS = {
    "POSTS": "reddit_posts",
    "COMMENTS": "reddit_comments",
    "SUBREDDIT_METADATA": "subreddit_metadata",
    "SCRAPERS": "reddit_scrapers",
    "ACCOUNTS": "reddit_accounts",
    "SCRAPE_ERRORS": "reddit_scrape_errors"  # Track scraping failures
}

# Scraper Configuration Defaults
DEFAULT_SCRAPER_CONFIG = {
    "scrape_interval": 60,         # 1 minute between cycles
    "posts_limit": 100,            # Default posts limit (optimized for 5 scrapers per account)
    "posts_per_comment_batch": 12, # Comments batch size (increased due to faster depth-limited processing)
    "sorting_methods": ["top", "rising"],  # Focus on quality and early trending posts
    "sort_limits": {               # Limits per sorting method
        "top": 150,                # Top posts from last 24 hours (proven quality)
        "rising": 100,             # Early trending detection
        "new": 500,                # Captures all new posts (optional, not in default)
        "hot": 500,                # Popular/trending posts (optional, not in default)
        "controversial": 500       # Controversial posts (optional, not in default)
    },
    "top_time_filter": "day",      # Time filter for "top" sorting: hour, day, week, month, year, all
    "controversial_time_filter": "day",  # Time filter for "controversial" sorting
    "subreddit_update_interval": 86400,  # 24 hours for subreddit metadata
    "replace_more_limit": 0,       # 0 = skip MoreComments entirely (faster), None = expand all (slower)
    "max_comment_depth": 3,        # Maximum comment nesting level (0-3 = top 4 levels, captures 85-90% of value)
    "max_retries": 3,              # Number of retry attempts for failed operations
    "retry_backoff_factor": 2,     # Exponential backoff multiplier (2 = 2s, 4s, 8s)
    "verify_before_marking": True, # Verify comments saved to DB before marking posts as scraped
}

# Monitoring Configuration
MONITORING_CONFIG = {
    "check_interval": 30,          # Seconds between health checks
    "restart_cooldown": 30,        # Seconds to wait before auto-restart
    "restart_delay": 5,            # Seconds to wait before restarting failed container
}

# API Configuration
API_CONFIG = {
    "host": "0.0.0.0",
    "port": 8000,
    "title": "Reddit Scraper API",
    "description": "Manage multiple Reddit scrapers with unique credentials",
    "version": "1.0.0"
}

# Docker Configuration
DOCKER_CONFIG = {
    "image_name": "reddit-scraper",
    "container_prefix": "reddit-scraper-",
    "remove_on_exit": False,       # Temporarily disable --rm flag to see logs
    "detached": True,              # -d flag
}

# Security Configuration
SECURITY_CONFIG = {
    "encryption_key_file": "/tmp/.scraper_key",  # Use /tmp for writable location
    "masked_credential_value": "***"
}

# Logging Configuration
LOGGING_CONFIG = {
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "date_format": "%Y-%m-%d %H:%M:%S",
    "level": "INFO"
} 