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
    "SCRAPE_ERRORS": "reddit_scrape_errors",  # Track scraping failures
    "API_USAGE": "reddit_api_usage"  # Track Reddit API call usage
}

# Posts Scraper Configuration Defaults
DEFAULT_POSTS_SCRAPER_CONFIG = {
    "scrape_interval": 60,         # 1 minute between cycles
    "posts_limit": 100,            # Default posts limit (optimized for 5 scrapers per account)
    "sorting_methods": ["new", "top", "rising"],  # Complete coverage + quality indicators
    "sort_limits": {               # Limits per sorting method
        "top": 150,                # Top posts from last 24 hours (proven quality)
        "rising": 100,             # Early trending detection
        "new": 500,                # Captures all new posts chronologically
        "hot": 500,                # Popular/trending posts (optional, not in default)
        "controversial": 500       # Controversial posts (optional, not in default)
    },
    "top_time_filter": "day",      # Time filter for "top" sorting: hour, day, week, month, year, all
    "initial_top_time_filter": "month",  # Time filter for first run to get historical data
    "controversial_time_filter": "day",  # Time filter for "controversial" sorting
    "subreddit_update_interval": 86400,  # 24 hours for subreddit metadata
    "max_retries": 3,              # Number of retry attempts for failed operations
    "retry_backoff_factor": 2,     # Exponential backoff multiplier (2 = 2s, 4s, 8s)
}

# Comments Scraper Configuration Defaults
DEFAULT_COMMENTS_SCRAPER_CONFIG = {
    "scrape_interval": 60,         # 1 minute between cycles
    "posts_per_comment_batch": 12, # Comments batch size (increased due to faster depth-limited processing)
    "replace_more_limit": 0,       # 0 = skip MoreComments entirely (faster), None = expand all (slower)
    "max_comment_depth": 3,        # Maximum comment nesting level (0-3 = top 4 levels, captures 85-90% of value)
    "max_retries": 3,              # Number of retry attempts for failed operations
    "retry_backoff_factor": 2,     # Exponential backoff multiplier (2 = 2s, 4s, 8s)
    "verify_before_marking": True, # Verify comments saved to DB before marking posts as scraped
}

# Combined config for backwards compatibility (used by both scrapers as defaults)
DEFAULT_SCRAPER_CONFIG = {
    **DEFAULT_POSTS_SCRAPER_CONFIG,
    **DEFAULT_COMMENTS_SCRAPER_CONFIG,
}

# Multi-Subreddit Scraper Configuration
MULTI_SCRAPER_CONFIG = {
    "max_subreddits_per_container": 100,   # Maximum subreddits per container (safe with 10-min interval)
    "rotation_delay": 2,                    # Seconds between subreddit switches
    "recommended_posts_limit": 50,          # Recommended posts limit per subreddit in multi-mode
    "recommended_interval": 600,            # Recommended interval (10 min) for 100 subreddits
    # Reddit API rate limit configuration (100 QPM for OAuth-authenticated apps)
    "rate_limit": {
        "reddit_qpm": 100,                  # Reddit's official limit: 100 queries per minute
        "safe_threshold": 50,               # < 50% usage = safe (green)
        "caution_threshold": 70,            # 50-70% usage = caution (yellow)
        "warning_threshold": 85,            # 70-85% usage = warning (orange)
        "critical_threshold": 95            # > 85% usage = critical (red)
    }
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
    "container_prefix": {
        "posts": "reddit-posts-scraper-",
        "comments": "reddit-comments-scraper-",
    },
    "scraper_scripts": {
        "posts": "posts_scraper.py",
        "comments": "comments_scraper.py",
    },
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

# Semantic Search & Embedding Configuration (Azure OpenAI)
EMBEDDING_CONFIG = {
    "model_name": "text-embedding-3-small",         # Azure OpenAI embedding model
    "dimensions": 1536,                             # text-embedding-3-small output dimensions
    "context_window": 8191,                         # Maximum tokens per text
    "batch_size": 32,                               # Batch size for embedding generation
    "similarity_metric": "cosine",                  # Distance metric for vector search
}

# Subreddit Discovery Configuration
DISCOVERY_CONFIG = {
    "collection_name": "subreddit_discovery",       # MongoDB collection for discovered subreddits
    "vector_index_name": "subreddit_vector_index",  # Vector search index name
    "default_search_limit": 10,                     # Default number of search results
    "default_min_subscribers": 1000,                # Default minimum subscriber filter
    "num_candidates": 100,                          # Number of candidates for vector search
    "sample_posts_limit": 20,                       # Number of sample posts to collect
    "sample_posts_time_filter": "month"             # Time filter for sample posts (month/week/year)
}

# Background Embedding Worker Configuration
EMBEDDING_WORKER_CONFIG = {
    "enabled": True,                                # Enable/disable background embedding worker
    "check_interval": 60,                           # Seconds between checks for pending embeddings
    "batch_size": 10,                               # Max subreddits to process per cycle
    "metadata_vector_index_name": "metadata_vector_index",  # Vector index for subreddit_metadata
    "max_retries": 3,                               # Max retry attempts for failed embeddings
    "retry_delay": 300                              # Seconds to wait before retrying failed embeddings
}

# Azure OpenAI Configuration (for LLM enrichment and embeddings)
AZURE_OPENAI_CONFIG = {
    "api_version": "2024-02-01",                    # Azure OpenAI API version
    "deployment_name": "gpt-4o-mini",               # Default deployment name for chat
    "embedding_deployment": "text-embedding-3-small",  # Deployment name for embeddings
    "max_tokens": 500,                              # Max tokens for enrichment response
    "temperature": 0.3,                             # Lower temperature for consistent output
    "enrichment_delay": 0.5,                        # Delay between API calls (seconds)
    "max_retries": 3                                # Max retry attempts for failed enrichment
}

# Persona Search Configuration
PERSONA_SEARCH_CONFIG = {
    "persona_vector_index_name": "metadata_persona_vector_index",  # Vector index for persona embeddings on subreddit_metadata
    "default_min_subscribers": 1000,                # Default minimum subscriber filter
    "num_candidates": 100,                          # Number of candidates for vector search
    "enrichment_fields": [                          # Fields included in persona embedding
        "audience_profile",
        "audience_types",
        "user_intents",
        "pain_points",
        "content_themes"
    ]
}

# Reddit API Usage Tracking Configuration
API_USAGE_CONFIG = {
    "collection_name": "reddit_api_usage",          # MongoDB collection for API usage data
    "flush_interval": 60,                           # Seconds between DB writes
    "retention_days": 30,                           # Days to keep historical data (TTL auto-cleanup)
    "batch_size": 100,                              # Max records per flush
} 