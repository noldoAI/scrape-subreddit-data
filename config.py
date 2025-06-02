#!/usr/bin/env python3
"""
Configuration file for Reddit Scraper system.
Centralizes all database names, collection names, and other constants.
"""

# Database Configuration
DATABASE_NAME = "seeky_testing"

# Collection Names
COLLECTIONS = {
    "POSTS": "reddit_posts",
    "COMMENTS": "reddit_comments", 
    "SUBREDDIT_METADATA": "subreddit_metadata",
    "SCRAPERS": "reddit_scrapers"
}

# Scraper Configuration Defaults
DEFAULT_SCRAPER_CONFIG = {
    "scrape_interval": 300,        # 5 minutes between cycles
    "posts_limit": 1000,           # Posts per scrape
    "posts_per_comment_batch": 20, # Comments batch size
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
    "remove_on_exit": True,        # --rm flag
    "detached": True,              # -d flag
}

# Security Configuration
SECURITY_CONFIG = {
    "encryption_key_file": ".scraper_key",
    "masked_credential_value": "***"
} 