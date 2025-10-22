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
    "ACCOUNTS": "reddit_accounts"
}

# Scraper Configuration Defaults
DEFAULT_SCRAPER_CONFIG = {
    "scrape_interval": 60,         # 1 minute between cycles (optimized for max data collection)
    "posts_limit": 1000,           # Posts per scrape per sorting method
    "posts_per_comment_batch": 50, # Comments batch size (increased from 20)
    "sorting_methods": ["new", "hot", "rising"],  # Multiple sorting methods to capture more posts
    "sort_limits": {               # Limits per sorting method
        "new": 1000,               # Captures all new posts
        "hot": 1000,               # Popular/trending posts
        "rising": 500,             # Early trending detection
        "top": 500,                # Top posts (optional)
        "controversial": 500       # Controversial posts (optional)
    },
    "subreddit_update_interval": 86400,  # 24 hours for subreddit metadata
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