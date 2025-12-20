#!/usr/bin/env python3
"""
Reddit Scraper Management API

A FastAPI application to manage multiple Reddit scrapers.
Start, stop, and monitor scrapers for different subreddits through HTTP endpoints.
Each scraper can use unique Reddit API credentials to avoid rate limit conflicts.
Includes persistent storage and automatic restart capabilities.
"""

# CRITICAL: For OpenTelemetry/Azure Monitor, configure logging BEFORE importing FastAPI
# This ensures proper instrumentation of the FastAPI framework
import logging
from dotenv import load_dotenv
load_dotenv()

from config import LOGGING_CONFIG
from core.azure_logging import setup_azure_logging
logger = setup_azure_logging("reddit-scraper-api", level=getattr(logging, LOGGING_CONFIG["level"]))

# Now import FastAPI and other dependencies
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from typing import Dict, List, Optional
import subprocess
import threading
import time
import os
import signal
from datetime import datetime, UTC, timedelta, timezone
import pymongo
import json
import base64
import hashlib
from cryptography.fernet import Fernet
import asyncio
import re
import praw
import prawcore.exceptions

# Import centralized configuration
from config import (
    DATABASE_NAME, COLLECTIONS, DEFAULT_SCRAPER_CONFIG,
    MONITORING_CONFIG, API_CONFIG, DOCKER_CONFIG, SECURITY_CONFIG,
    EMBEDDING_WORKER_CONFIG, AZURE_OPENAI_CONFIG, MULTI_SCRAPER_CONFIG
)

# Import Prometheus metrics
from core.metrics import (
    update_metrics_from_db, get_metrics, init_metrics,
    scraper_up, database_connected, docker_available as docker_available_metric,
    CONTENT_TYPE_LATEST
)

# Import API usage tracking functions
from tracking.api_usage_tracker import get_usage_stats, get_usage_trends, API_USAGE_CONFIG

app = FastAPI(
    title=API_CONFIG["title"],
    description=API_CONFIG["description"],
    version=API_CONFIG["version"]
)

# MongoDB connection for stats and scraper storage
try:
    mongodb_uri = os.getenv("MONGODB_URI")
    if not mongodb_uri:
        logger.error("MONGODB_URI environment variable not set")
        mongo_connected = False
    else:
        client = pymongo.MongoClient(mongodb_uri)
        # Test the connection
        client.admin.command('ping')
        db = client[DATABASE_NAME]
        posts_collection = db[COLLECTIONS["POSTS"]]
        comments_collection = db[COLLECTIONS["COMMENTS"]]
        subreddit_collection = db[COLLECTIONS["SUBREDDIT_METADATA"]]
        scrapers_collection = db[COLLECTIONS["SCRAPERS"]]
        accounts_collection = db[COLLECTIONS["ACCOUNTS"]]
        mongo_connected = True
        logger.info("Successfully connected to MongoDB")

        # Create performance indexes for statistics queries
        try:
            logger.info("Creating database indexes for performance...")
            # Posts collection indexes (for stats aggregations)
            posts_collection.create_index([("subreddit", 1), ("created_datetime", -1)])
            posts_collection.create_index([("subreddit", 1), ("score", -1)])
            posts_collection.create_index([("subreddit", 1), ("num_comments", -1)])
            posts_collection.create_index([("subreddit", 1), ("scraped_at", -1)])
            posts_collection.create_index([("subreddit", 1), ("sort_method", 1)])

            # Comments collection indexes
            comments_collection.create_index([("subreddit", 1), ("created_datetime", -1)])
            comments_collection.create_index([("subreddit", 1), ("score", -1)])
            comments_collection.create_index([("subreddit", 1), ("depth", 1)])

            # Errors collection indexes
            errors_collection = db[COLLECTIONS["SCRAPE_ERRORS"]]
            errors_collection.create_index([("subreddit", 1), ("resolved", 1)])
            errors_collection.create_index([("subreddit", 1), ("timestamp", -1)])

            logger.info("Database indexes created successfully")
        except Exception as e:
            logger.warning(f"Error creating indexes (may already exist): {e}")

        # Initialize Prometheus metrics
        init_metrics(version=API_CONFIG["version"])
        logger.info("Prometheus metrics initialized")
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {e}")
    mongo_connected = False

# Global storage for active scrapers (for quick access, backed by database)
active_scrapers: Dict[str, dict] = {}

# Encryption key for credentials (generate one if not exists)
def get_encryption_key():
    key_file = SECURITY_CONFIG["encryption_key_file"]
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(key_file, "wb") as f:
            f.write(key)
        return key

ENCRYPTION_KEY = get_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)

def encrypt_credential(value: str) -> str:
    """Encrypt a credential value"""
    return base64.b64encode(cipher_suite.encrypt(value.encode())).decode()

def decrypt_credential(encrypted_value: str) -> str:
    """Decrypt a credential value"""
    return cipher_suite.decrypt(base64.b64decode(encrypted_value.encode())).decode()

class RedditCredentials(BaseModel):
    client_id: str
    client_secret: str
    username: str
    password: str
    user_agent: str

class CredentialValidationResponse(BaseModel):
    """Response model for credential validation"""
    valid: bool
    username: Optional[str] = None
    error: Optional[str] = None
    error_type: Optional[str] = None

class ScraperConfig(BaseModel):
    name: Optional[str] = None             # Custom scraper name (optional)
    subreddit: str = ""                    # Single subreddit (backwards compat)
    subreddits: List[str] = []             # Multi-subreddit mode
    scraper_type: str = "posts"  # "posts" or "comments"
    posts_limit: int = DEFAULT_SCRAPER_CONFIG["posts_limit"]
    interval: int = DEFAULT_SCRAPER_CONFIG["scrape_interval"]
    comment_batch: int = DEFAULT_SCRAPER_CONFIG["posts_per_comment_batch"]
    sorting_methods: List[str] = DEFAULT_SCRAPER_CONFIG["sorting_methods"]  # Multiple sorting methods
    credentials: RedditCredentials
    auto_restart: bool = True  # Enable automatic restart on failure

class ScraperStatus(BaseModel):
    subreddit: str
    scraper_type: str = "posts"  # "posts" or "comments"
    status: str  # "running", "stopped", "error", "failed"
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    config: Optional[ScraperConfig] = None
    last_error: Optional[str] = None

class ScraperStartRequest(BaseModel):
    name: Optional[str] = None             # Custom scraper name (optional)
    subreddit: str = ""                    # Single subreddit (backwards compat)
    subreddits: List[str] = []             # Multi-subreddit mode (up to 100)
    scraper_type: str = "posts"  # "posts" or "comments"
    posts_limit: int = DEFAULT_SCRAPER_CONFIG["posts_limit"]
    interval: int = DEFAULT_SCRAPER_CONFIG["scrape_interval"]
    comment_batch: int = DEFAULT_SCRAPER_CONFIG["posts_per_comment_batch"]
    sorting_methods: List[str] = DEFAULT_SCRAPER_CONFIG["sorting_methods"]  # Multiple sorting methods
    auto_restart: bool = True

    # Option 1: Use saved account
    saved_account_name: Optional[str] = None

    # Option 2: Manual credentials (and optionally save them)
    credentials: Optional[RedditCredentials] = None
    save_account_as: Optional[str] = None  # If provided, save manual credentials with this name

class SubredditUpdateRequest(BaseModel):
    """Request to update subreddits for a running scraper (triggers restart)."""
    subreddits: List[str]

class RateLimitInfo(BaseModel):
    """Rate limit analysis for a scraper configuration."""
    calls_per_cycle: int
    estimated_calls_per_minute: float
    reddit_limit_per_minute: int = 100  # Reddit's official limit: 100 QPM
    usage_percent: float
    warning_level: str  # "safe", "caution", "warning", "critical"
    recommendation: Optional[str] = None

# Reddit account storage in MongoDB  
def get_accounts_collection():
    """Get the accounts collection, create if needed"""
    return accounts_collection

def save_reddit_account(account_name: str, credentials: RedditCredentials):
    """Save Reddit credentials to database with encryption"""
    try:
        if not mongo_connected:
            logger.error("Database not connected, cannot save account")
            return False
        
        accounts_collection = get_accounts_collection()

        # Store credentials directly (no encryption - MongoDB already secured)
        update_data = {
            "account_name": account_name,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "username": credentials.username,
            "password": credentials.password,
            "user_agent": credentials.user_agent,
            "last_updated": datetime.now(UTC)
        }
        
        # Upsert account document
        result = accounts_collection.update_one(
            {"account_name": account_name},
            {
                "$set": update_data,
                "$setOnInsert": {"created_at": datetime.now(UTC)}
            },
            upsert=True
        )
        
        logger.info(f"Saved Reddit account: {account_name}")
        return True
        
    except Exception as e:
        logger.error(f"Error saving Reddit account: {e}")
        return False

def load_saved_accounts():
    """Load saved Reddit accounts from database"""
    try:
        if not mongo_connected:
            logger.warning("Database not connected, cannot load accounts")
            return {}
        
        accounts_collection = get_accounts_collection()
        accounts_cursor = accounts_collection.find({})
        
        accounts = {}
        for account_doc in accounts_cursor:
            account_name = account_doc["account_name"]
            accounts[account_name] = {
                "account_name": account_name,
                "username": account_doc["username"],
                "user_agent": account_doc["user_agent"],
                "created_at": account_doc["created_at"].isoformat() if isinstance(account_doc["created_at"], datetime) else account_doc["created_at"],
                "last_updated": account_doc.get("last_updated")
            }
        
        return accounts
    except Exception as e:
        logger.error(f"Error loading saved accounts: {e}")
        return {}

def get_reddit_account(account_name: str) -> Optional[RedditCredentials]:
    """Get Reddit credentials for an account"""
    try:
        if not mongo_connected:
            logger.warning("Database not connected, cannot get account")
            return None

        accounts_collection = get_accounts_collection()
        account_doc = accounts_collection.find_one({"account_name": account_name})

        if not account_doc:
            return None

        # Read credentials directly (no decryption needed)
        return RedditCredentials(
            client_id=account_doc["client_id"],
            client_secret=account_doc["client_secret"],
            username=account_doc["username"],
            password=account_doc["password"],
            user_agent=account_doc["user_agent"]
        )
        
    except Exception as e:
        logger.error(f"Error getting Reddit account {account_name}: {e}")
        return None

def delete_reddit_account(account_name: str):
    """Delete a saved Reddit account"""
    try:
        if not mongo_connected:
            logger.error("Database not connected, cannot delete account")
            return False
        
        accounts_collection = get_accounts_collection()
        result = accounts_collection.delete_one({"account_name": account_name})
        
        if result.deleted_count > 0:
            logger.info(f"Deleted Reddit account: {account_name}")
            return True
        else:
            logger.warning(f"Account '{account_name}' not found for deletion")
            return False
    except Exception as e:
        logger.error(f"Error deleting Reddit account {account_name}: {e}")
        return False

def validate_reddit_credentials(credentials: RedditCredentials, timeout: int = 10) -> dict:
    """
    Validate Reddit API credentials by attempting authentication.

    Args:
        credentials: RedditCredentials object with all credential fields
        timeout: Maximum seconds to wait for Reddit API response

    Returns:
        dict with:
            - valid (bool): Whether credentials are valid
            - username (str): Authenticated Reddit username (if valid)
            - error (str): Error message (if invalid)
            - error_type (str): Error category for programmatic handling
    """
    try:
        reddit = praw.Reddit(
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            username=credentials.username,
            password=credentials.password,
            user_agent=credentials.user_agent,
            timeout=timeout
        )

        # This call validates credentials - throws exception if invalid
        authenticated_user = reddit.user.me()

        if authenticated_user is None:
            return {
                "valid": False,
                "error": "Authentication failed - could not retrieve user info",
                "error_type": "auth_failed"
            }

        return {
            "valid": True,
            "username": str(authenticated_user),
            "error": None,
            "error_type": None
        }

    except prawcore.exceptions.OAuthException as e:
        # Invalid client_id or client_secret
        return {
            "valid": False,
            "error": f"Invalid Reddit API credentials (client_id/client_secret): {e}",
            "error_type": "oauth_error"
        }

    except prawcore.exceptions.ResponseException as e:
        # Check for specific HTTP status codes
        if hasattr(e, 'response') and e.response is not None:
            status_code = e.response.status_code
            if status_code == 401:
                return {
                    "valid": False,
                    "error": "Invalid Reddit username or password",
                    "error_type": "invalid_password"
                }
            elif status_code == 403:
                return {
                    "valid": False,
                    "error": "Reddit account is banned or suspended",
                    "error_type": "account_suspended"
                }
        return {
            "valid": False,
            "error": f"Reddit API error: {e}",
            "error_type": "api_error"
        }

    except prawcore.exceptions.Forbidden:
        return {
            "valid": False,
            "error": "Reddit account is banned, suspended, or lacks required permissions",
            "error_type": "forbidden"
        }

    except prawcore.exceptions.TooManyRequests as e:
        retry_after = getattr(e, 'retry_after', 60)
        return {
            "valid": False,
            "error": f"Reddit API rate limit exceeded. Try again in {retry_after} seconds",
            "error_type": "rate_limited"
        }

    except prawcore.exceptions.ServerError:
        return {
            "valid": False,
            "error": "Reddit API is temporarily unavailable. Please try again later",
            "error_type": "server_error"
        }

    except prawcore.exceptions.RequestException as e:
        return {
            "valid": False,
            "error": f"Network error connecting to Reddit API: {e}",
            "error_type": "network_error"
        }

    except Exception as e:
        return {
            "valid": False,
            "error": f"Unexpected error validating credentials: {e}",
            "error_type": "unknown_error"
        }

def calculate_rate_limit_info(subreddit_count: int, config: dict) -> dict:
    """
    Calculate expected Reddit API usage based on subreddit count and config.

    Reddit API limit: 100 queries per minute (QPM) for OAuth-authenticated apps.

    Args:
        subreddit_count: Number of subreddits to scrape
        config: Dict with 'sorting_methods' (list) and 'interval' (seconds)

    Returns:
        Dict with rate limit analysis including warning level
    """
    sorting_methods = len(config.get("sorting_methods", ["new", "top", "rising"]))
    interval = config.get("interval", 300)

    # API calls per subreddit per cycle:
    # - ~3 API calls per sorting method (pagination, etc.)
    # - ~1 call for metadata check
    calls_per_sub = (sorting_methods * 3) + 1
    total_calls = calls_per_sub * subreddit_count

    # Cycle time includes rotation delay (2s per subreddit) + interval
    rotation_delay = MULTI_SCRAPER_CONFIG.get("rotation_delay", 2)
    cycle_time = (subreddit_count * rotation_delay) + interval

    # Calculate calls per minute
    calls_per_min = (total_calls / cycle_time) * 60

    # Reddit limit: 100 QPM
    reddit_qpm = MULTI_SCRAPER_CONFIG.get("rate_limit", {}).get("reddit_qpm", 100)
    usage_percent = (calls_per_min / reddit_qpm) * 100

    # Determine warning level based on thresholds
    thresholds = MULTI_SCRAPER_CONFIG.get("rate_limit", {})
    critical = thresholds.get("critical_threshold", 95)
    warning = thresholds.get("warning_threshold", 85)
    caution = thresholds.get("caution_threshold", 70)

    if usage_percent >= critical:
        level = "critical"
        safe_count = int(subreddit_count * (critical / usage_percent) * 0.9)
        recommendation = f"Reduce to ~{safe_count} subreddits, increase interval to {int(interval * 1.5)}s, or use fewer sorting methods"
    elif usage_percent >= warning:
        level = "warning"
        recommendation = "Approaching rate limits. Consider reducing subreddits or sorting methods."
    elif usage_percent >= caution:
        level = "caution"
        recommendation = None
    else:
        level = "safe"
        recommendation = None

    return {
        "calls_per_cycle": total_calls,
        "estimated_calls_per_minute": round(calls_per_min, 1),
        "reddit_limit_per_minute": reddit_qpm,
        "usage_percent": round(usage_percent, 1),
        "warning_level": level,
        "recommendation": recommendation
    }

def save_scraper_to_db(subreddit: str, config: ScraperConfig, status: str = "starting",
                       container_id: str = None, container_name: str = None,
                       last_error: str = None, scraper_type: str = "posts",
                       subreddits: list = None, name: str = None):
    """Save scraper configuration to database"""
    try:
        # Store credentials directly (no encryption - MongoDB already secured)
        credentials = {
            "client_id": config.credentials.client_id,
            "client_secret": config.credentials.client_secret,
            "username": config.credentials.username,
            "password": config.credentials.password,
            "user_agent": config.credentials.user_agent
        }

        scraper_doc = {
            "name": name or config.name,  # Custom scraper name
            "subreddit": subreddit,
            "scraper_type": scraper_type,
            "status": status,
            "container_id": container_id,
            "container_name": container_name,
            "config": {
                "posts_limit": config.posts_limit,
                "interval": config.interval,
                "comment_batch": config.comment_batch,
                "sorting_methods": config.sorting_methods
            },
            "credentials": credentials,
            "auto_restart": config.auto_restart,
            "last_updated": datetime.now(UTC),
            "last_error": last_error,
            "restart_count": 0,
            "subreddits": subreddits if subreddits else [subreddit]  # Store all subreddits for multi-mode
        }

        # Initialize metrics on first insert only
        metrics_init = {
            "total_posts_collected": 0,
            "total_comments_collected": 0,
            "total_cycles": 0,
            "last_cycle_posts": 0,
            "last_cycle_comments": 0,
            "last_cycle_time": None,
            "last_cycle_duration": 0,
            "posts_per_hour": 0,
            "comments_per_hour": 0,
            "avg_cycle_duration": 0
        }

        # Upsert scraper document - unique by subreddit AND scraper_type
        scrapers_collection.update_one(
            {"subreddit": subreddit, "scraper_type": scraper_type},
            {
                "$set": scraper_doc,
                "$setOnInsert": {
                    "created_at": datetime.now(UTC),
                    "metrics": metrics_init
                }
            },
            upsert=True
        )

        logger.info(f"Saved {scraper_type} scraper configuration for r/{subreddit} to database")
        return True

    except Exception as e:
        logger.error(f"Error saving scraper to database: {e}")
        return False

def load_scraper_from_db(subreddit: str, scraper_type: str = "posts") -> Optional[dict]:
    """Load scraper configuration from database"""
    try:
        if not mongo_connected:
            logger.warning("Database not connected, cannot load scraper")
            return None

        # Try to find with scraper_type first
        scraper_doc = scrapers_collection.find_one({"subreddit": subreddit, "scraper_type": scraper_type})

        # Backwards compatibility: try without scraper_type field for old records
        if not scraper_doc:
            scraper_doc = scrapers_collection.find_one({
                "subreddit": subreddit,
                "scraper_type": {"$exists": False}
            })

        if not scraper_doc:
            return None

        # Read credentials directly (no decryption needed)
        credentials = RedditCredentials(
            client_id=scraper_doc["credentials"]["client_id"],
            client_secret=scraper_doc["credentials"]["client_secret"],
            username=scraper_doc["credentials"]["username"],
            password=scraper_doc["credentials"]["password"],
            user_agent=scraper_doc["credentials"]["user_agent"]
        )

        # Reconstruct ScraperConfig
        config = ScraperConfig(
            name=scraper_doc.get("name"),  # Preserve custom scraper name
            subreddit=subreddit,
            subreddits=scraper_doc.get("subreddits", [subreddit]),  # Preserve multi-subreddit config
            scraper_type=scraper_doc.get("scraper_type", "posts"),
            posts_limit=scraper_doc["config"]["posts_limit"],
            interval=scraper_doc["config"]["interval"],
            comment_batch=scraper_doc["config"]["comment_batch"],
            sorting_methods=scraper_doc["config"].get("sorting_methods", ["hot"]),  # Default to ["hot"] for backward compatibility
            credentials=credentials,
            auto_restart=scraper_doc.get("auto_restart", True)
        )

        return {
            "config": config,
            "status": scraper_doc["status"],
            "scraper_type": scraper_doc.get("scraper_type", "posts"),
            "container_id": scraper_doc.get("container_id"),
            "container_name": scraper_doc.get("container_name"),
            "created_at": scraper_doc["created_at"],
            "last_updated": scraper_doc["last_updated"],
            "last_error": scraper_doc.get("last_error"),
            "restart_count": scraper_doc.get("restart_count", 0)
        }

    except Exception as e:
        logger.error(f"Error loading scraper from database for r/{subreddit}: {e}")
        return None

def update_scraper_status(subreddit: str, status: str, container_id: str = None,
                         container_name: str = None, last_error: str = None,
                         increment_restart: bool = False, scraper_type: str = "posts"):
    """Update scraper status in database"""
    try:
        update_data = {
            "status": status,
            "last_updated": datetime.now(UTC)
        }

        if container_id:
            update_data["container_id"] = container_id
        if container_name:
            update_data["container_name"] = container_name
        if last_error:
            update_data["last_error"] = last_error

        # Build the update operation properly
        if increment_restart:
            # Use both $set and $inc operators at the same level
            update_operation = {
                "$set": update_data,
                "$inc": {"restart_count": 1}
            }
        else:
            # Only use $set
            update_operation = {"$set": update_data}

        # Try with scraper_type first
        result = scrapers_collection.update_one(
            {"subreddit": subreddit, "scraper_type": scraper_type},
            update_operation
        )

        # Backwards compatibility: try without scraper_type for old records
        if result.matched_count == 0:
            result = scrapers_collection.update_one(
                {"subreddit": subreddit, "scraper_type": {"$exists": False}},
                update_operation
            )

        return result.modified_count > 0

    except Exception as e:
        logger.error(f"Error updating scraper status: {e}")
        return False

def get_scraper_key(subreddit: str, scraper_type: str = "posts") -> str:
    """Generate unique key for scraper in memory cache"""
    return f"{subreddit}:{scraper_type}"

def load_all_scrapers_from_db():
    """Load all scrapers from database on startup"""
    try:
        scrapers = scrapers_collection.find({})
        for scraper_doc in scrapers:
            subreddit = scraper_doc["subreddit"]
            scraper_type = scraper_doc.get("scraper_type", "posts")
            scraper_data = load_scraper_from_db(subreddit, scraper_type)
            if scraper_data:
                # Create safe config for memory storage (masked credentials)
                safe_config = scraper_data["config"].model_copy()
                safe_config.credentials = RedditCredentials(
                    client_id=SECURITY_CONFIG["masked_credential_value"],
                    client_secret=SECURITY_CONFIG["masked_credential_value"],
                    username=scraper_data["config"].credentials.username,
                    password=SECURITY_CONFIG["masked_credential_value"],
                    user_agent=scraper_data["config"].credentials.user_agent
                )

                # Use composite key for memory cache
                scraper_key = get_scraper_key(subreddit, scraper_type)
                active_scrapers[scraper_key] = {
                    "config": safe_config,
                    "status": scraper_data["status"],
                    "scraper_type": scraper_type,
                    "container_id": scraper_data["container_id"],
                    "container_name": scraper_data["container_name"],
                    "started_at": scraper_data["created_at"],
                    "last_error": scraper_data["last_error"]
                }
        
        logger.info(f"Loaded {len(active_scrapers)} scrapers from database")
        
    except Exception as e:
        logger.error(f"Error loading scrapers from database: {e}")

def check_container_status(container_name):
    """Check if a Docker container is running"""
    try:
        result = subprocess.run([
            "docker", "inspect", container_name, "--format", "{{.State.Status}}"
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception as e:
        logger.debug(f"Error checking container status for {container_name}: {e}")
        return None

def cleanup_container(container_name, timeout=30):
    """Stop and remove a Docker container completely"""
    try:
        # First check if container exists and get its status
        status = check_container_status(container_name)
        
        if status is None:
            logger.debug(f"Container {container_name} does not exist")
            return True
        
        # If container is running, stop it first
        if status == "running":
            try:
                result = subprocess.run([
                    "docker", "stop", container_name
                ], capture_output=True, text=True, timeout=timeout)
                
                if result.returncode == 0:
                    logger.debug(f"Stopped container {container_name}")
                else:
                    # If stop failed, try force kill
                    subprocess.run([
                        "docker", "kill", container_name
                    ], capture_output=True, text=True)
                    logger.debug(f"Force killed container {container_name}")
                    
            except subprocess.TimeoutExpired:
                # Force kill if timeout
                subprocess.run([
                    "docker", "kill", container_name
                ], capture_output=True, text=True)
                logger.debug(f"Timeout - force killed container {container_name}")
        else:
            logger.debug(f"Container {container_name} is already stopped (status: {status})")
    
    except Exception as e:
        logger.debug(f"Error during container stop phase for {container_name}: {e}")
    
    # Always try to remove the container, regardless of stop result
    try:
        result = subprocess.run([
            "docker", "rm", "-f", container_name  # Use -f to force remove
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.debug(f"Removed container {container_name}")
            return True
        else:
            logger.warning(f"Failed to remove container {container_name}: {result.stderr}")
            return False
    except Exception as e:
        logger.warning(f"Error removing container {container_name}: {e}")
        return False

def get_container_logs(container_name, lines=50):
    """Get recent logs from a Docker container"""
    try:
        result = subprocess.run([
            "docker", "logs", "--tail", str(lines), container_name
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception as e:
        logger.debug(f"Error getting logs for container {container_name}: {e}")
        return None

def check_for_failed_scrapers():
    """Background task to check for failed containers and restart if needed"""
    while True:
        try:
            # Skip if database not connected
            if not mongo_connected:
                logger.debug("Database not connected, skipping failed scraper check")
                time.sleep(60)
                continue

            # Get all scrapers that should be running
            running_scrapers = scrapers_collection.find({"status": "running", "auto_restart": True})

            for scraper_doc in running_scrapers:
                subreddit = scraper_doc["subreddit"]
                scraper_type = scraper_doc.get("scraper_type", "posts")
                container_name = scraper_doc.get("container_name")

                if container_name:
                    # Check if container is actually running
                    container_status = check_container_status(container_name)

                    if container_status != "running":
                        logger.info(f"Detected failed {scraper_type} container for r/{subreddit}, attempting restart...")

                        # Load full config from database
                        scraper_data = load_scraper_from_db(subreddit, scraper_type)
                        if scraper_data and scraper_data["config"]:
                            # Update status to failed
                            update_scraper_status(subreddit, "failed",
                                                last_error="Container stopped unexpectedly",
                                                increment_restart=True,
                                                scraper_type=scraper_type)

                            # Attempt restart after a short delay to avoid rapid restarts
                            time.sleep(MONITORING_CONFIG["restart_delay"])
                            restart_scraper(scraper_data["config"], subreddit)

            # Also check for scrapers that are marked as "stopped" but should be running
            stopped_scrapers = scrapers_collection.find({
                "status": {"$in": ["stopped", "failed"]},
                "auto_restart": True
            })

            for scraper_doc in stopped_scrapers:
                subreddit = scraper_doc["subreddit"]
                scraper_type = scraper_doc.get("scraper_type", "posts")
                # Only restart if it's been stopped for more than configured cooldown
                last_updated = scraper_doc.get("last_updated")
                if last_updated:
                    current_time = datetime.now(UTC)
                    # Ensure consistent timezone handling
                    if last_updated.tzinfo is None:
                        # Database has timezone-naive datetime, convert to UTC for comparison
                        last_updated_utc = last_updated.replace(tzinfo=UTC)
                    else:
                        last_updated_utc = last_updated
                    
                    time_since_update = (current_time - last_updated_utc).total_seconds()
                    if time_since_update > MONITORING_CONFIG["restart_cooldown"]:
                        logger.info(f"Auto-restarting stopped {scraper_type} scraper for r/{subreddit}...")

                        scraper_data = load_scraper_from_db(subreddit, scraper_type)
                        if scraper_data and scraper_data["config"]:
                            restart_scraper(scraper_data["config"], subreddit)
            
            # Sleep for configured interval before next check
            time.sleep(MONITORING_CONFIG["check_interval"])
            
        except Exception as e:
            logger.error(f"Error in failed scraper check: {e}")
            time.sleep(60)

def restart_scraper(config: ScraperConfig, subreddit: str):
    """Restart a failed scraper"""
    try:
        scraper_type = getattr(config, 'scraper_type', 'posts')
        logger.info(f"Restarting {scraper_type} scraper for r/{subreddit}")

        # Stop and remove any existing container first using centralized naming
        container_prefix = DOCKER_CONFIG['container_prefix'].get(scraper_type, DOCKER_CONFIG['container_prefix']['posts'])
        container_name = f"{container_prefix}{subreddit}"
        cleanup_container(container_name)

        # Update status to restarting
        update_scraper_status(subreddit, "restarting", scraper_type=scraper_type)

        # Start new container
        run_scraper(config)

    except Exception as e:
        logger.error(f"Error restarting scraper for r/{subreddit}: {e}")
        update_scraper_status(subreddit, "error", last_error=f"Restart failed: {str(e)}")

# Start background monitoring thread
if mongo_connected:
    monitoring_thread = threading.Thread(target=check_for_failed_scrapers, daemon=True)
    monitoring_thread.start()

# Initialize and start embedding worker
embedding_worker = None
if mongo_connected and EMBEDDING_WORKER_CONFIG.get("enabled", True):
    try:
        from embedding_worker import EmbeddingWorker
        embedding_worker = EmbeddingWorker(db)
        embedding_worker.start_background()
        logger.info("Embedding worker started")
    except ImportError:
        logger.warning("embedding_worker module not found, embedding worker disabled")
    except Exception as e:
        logger.error(f"Failed to start embedding worker: {e}")

# Load existing scrapers on startup
if mongo_connected:
    load_all_scrapers_from_db()

def run_scraper(config: ScraperConfig):
    """Run a scraper in a separate Docker container with unique credentials"""
    try:
        # Get scraper type (posts or comments)
        scraper_type = getattr(config, 'scraper_type', 'posts')

        # Determine subreddits list (multi-subreddit or single)
        subreddits = config.subreddits if config.subreddits else [config.subreddit]
        subreddit_arg = ",".join(subreddits)
        is_multi = len(subreddits) > 1

        # Create unique container name using type-specific prefix
        container_prefix = DOCKER_CONFIG['container_prefix'].get(scraper_type, DOCKER_CONFIG['container_prefix']['posts'])
        if config.name:
            # Use custom name if provided (sanitize for Docker container name)
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '-', config.name.lower())[:30]
            container_name = f"{container_prefix}{safe_name}"
            display_name = config.name
        elif is_multi:
            # Multi-subreddit container naming: reddit-posts-scraper-multi-5subs-stocks
            container_name = f"{container_prefix}multi-{len(subreddits)}subs-{subreddits[0][:10]}"
            display_name = f"multi:{len(subreddits)}subs"
        else:
            container_name = f"{container_prefix}{config.subreddit}"
            display_name = config.subreddit

        # Get the appropriate script for this scraper type
        scraper_script = DOCKER_CONFIG['scraper_scripts'].get(scraper_type, 'posts_scraper.py')

        # Save to database first (use first subreddit as primary key for multi-mode)
        primary_subreddit = subreddits[0] if is_multi else config.subreddit
        save_scraper_to_db(primary_subreddit, config, "starting", container_name=container_name, scraper_type=scraper_type, subreddits=subreddits)

        # Prepare environment variables for the container
        env_vars = [
            f"R_CLIENT_ID={config.credentials.client_id}",
            f"R_CLIENT_SECRET={config.credentials.client_secret}",
            f"R_USERNAME={config.credentials.username}",
            f"R_PASSWORD={config.credentials.password}",
            f"R_USER_AGENT={config.credentials.user_agent}",
            f"MONGODB_URI={os.getenv('MONGODB_URI', '')}"
        ]

        # Add Azure Application Insights connection string if available
        app_insights_conn = os.getenv('APPLICATIONINSIGHTS_CONNECTION_STRING', '')
        if app_insights_conn:
            env_vars.append(f"APPLICATIONINSIGHTS_CONNECTION_STRING={app_insights_conn}")

        # Build Docker command using centralized config
        cmd = ["docker", "run", "--name", container_name]

        # Add flags based on configuration
        if DOCKER_CONFIG["remove_on_exit"]:
            cmd.append("--rm")
        if DOCKER_CONFIG["detached"]:
            cmd.append("-d")

        # Add environment variables
        for env_var in env_vars:
            cmd.extend(["-e", env_var])

        # Build command based on scraper type
        if scraper_type == "posts":
            cmd.extend([
                DOCKER_CONFIG["image_name"],
                "python", scraper_script, subreddit_arg,  # Pass comma-separated subreddits
                "--posts-limit", str(config.posts_limit),
                "--interval", str(config.interval),
                "--sorting-methods", ",".join(config.sorting_methods)
            ])
        else:  # comments
            cmd.extend([
                DOCKER_CONFIG["image_name"],
                "python", scraper_script, subreddit_arg,  # Pass comma-separated subreddits
                "--interval", str(config.interval),
                "--comment-batch", str(config.comment_batch)
            ])

        # Stop and remove any existing container with the same name
        cleanup_container(container_name)
        
        # Start the container
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            error_msg = f"Failed to start container: {result.stderr}"
            update_scraper_status(primary_subreddit, "error", last_error=error_msg, scraper_type=scraper_type)
            raise Exception(error_msg)

        container_id = result.stdout.strip()

        # Update database with container info and running status
        update_scraper_status(primary_subreddit, "running", container_id=container_id,
                            container_name=container_name, scraper_type=scraper_type)

        # Update memory cache with safe config (use model_copy instead of copy)
        config_safe = config.model_copy()
        config_safe.credentials = RedditCredentials(
            client_id=SECURITY_CONFIG["masked_credential_value"],
            client_secret=SECURITY_CONFIG["masked_credential_value"],
            username=config.credentials.username,  # Keep username for identification
            password=SECURITY_CONFIG["masked_credential_value"],
            user_agent=config.credentials.user_agent
        )

        # Use composite key for memory cache
        scraper_key = get_scraper_key(primary_subreddit, scraper_type)
        active_scrapers[scraper_key] = {
            "container_id": container_id,
            "container_name": container_name,
            "config": config_safe,
            "scraper_type": scraper_type,
            "subreddits": subreddits,  # Store all subreddits for multi-mode
            "started_at": datetime.now(UTC),
            "status": "running",
            "last_error": None
        }

        if is_multi:
            logger.info(f"Started {scraper_type} container {container_name} ({container_id[:12]}) for {len(subreddits)} subreddits: {', '.join(subreddits)}")
        else:
            logger.info(f"Started {scraper_type} container {container_name} ({container_id[:12]}) for r/{primary_subreddit}")

    except Exception as e:
        error_msg = f"Error starting {scraper_type} container for {display_name}: {e}"
        logger.error(error_msg)
        update_scraper_status(primary_subreddit, "error", last_error=str(e), scraper_type=scraper_type)
        scraper_key = get_scraper_key(primary_subreddit, scraper_type)
        if scraper_key in active_scrapers:
            active_scrapers[scraper_key]["status"] = "error"
            active_scrapers[scraper_key]["last_error"] = str(e)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Enhanced web dashboard with credential input and persistent storage"""
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Reddit Scraper · Mission Control</title>
        <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect fill='%23050810' width='32' height='32' rx='6'/><circle cx='16' cy='16' r='8' fill='none' stroke='%2300e5ff' stroke-width='2'/><circle cx='16' cy='16' r='4' fill='%2300e5ff'/><line x1='16' y1='8' x2='16' y2='2' stroke='%2300e5ff' stroke-width='2' stroke-linecap='round'/></svg>">
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Fira+Code:wght@400;500;600&family=Syne:wght@600;700;800&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-deep: #050810;
                --bg-primary: #0a0f1a;
                --bg-card: #0d1424;
                --bg-elevated: #111b2e;
                --bg-hover: #162033;

                --border-subtle: rgba(255,255,255,0.06);
                --border-default: rgba(255,255,255,0.1);
                --border-hover: rgba(255,255,255,0.15);

                --text-primary: #f0f4fc;
                --text-secondary: #8892a6;
                --text-muted: #5a6478;

                --accent-cyan: #00e5ff;
                --accent-cyan-glow: rgba(0,229,255,0.15);
                --accent-green: #00ff88;
                --accent-green-glow: rgba(0,255,136,0.15);
                --accent-amber: #ffb800;
                --accent-amber-glow: rgba(255,184,0,0.15);
                --accent-red: #ff3366;
                --accent-red-glow: rgba(255,51,102,0.15);
                --accent-purple: #a855f7;
                --accent-purple-glow: rgba(168,85,247,0.15);

                --font-display: 'Syne', sans-serif;
                --font-body: 'Outfit', sans-serif;
                --font-mono: 'Fira Code', monospace;

                --radius-sm: 6px;
                --radius-md: 10px;
                --radius-lg: 16px;

                --shadow-glow: 0 0 40px rgba(0,229,255,0.1);
            }

            * { margin: 0; padding: 0; box-sizing: border-box; }

            body {
                font-family: var(--font-body);
                background: var(--bg-deep);
                color: var(--text-primary);
                min-height: 100vh;
                line-height: 1.6;
            }

            /* Background grid pattern */
            body::before {
                content: '';
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background-image:
                    linear-gradient(rgba(0,229,255,0.03) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(0,229,255,0.03) 1px, transparent 1px);
                background-size: 50px 50px;
                pointer-events: none;
                z-index: 0;
            }

            .dashboard-container {
                position: relative;
                z-index: 1;
                max-width: 1600px;
                margin: 0 auto;
                padding: 40px 48px;
            }

            /* Header Section */
            .header {
                margin-bottom: 48px;
            }

            .header-top {
                display: flex;
                align-items: flex-start;
                justify-content: space-between;
                gap: 24px;
                margin-bottom: 32px;
            }

            .header-brand {
                display: flex;
                align-items: center;
                gap: 16px;
            }

            .logo-mark {
                width: 56px;
                height: 56px;
                background: linear-gradient(135deg, var(--accent-cyan), var(--accent-purple));
                border-radius: var(--radius-md);
                display: flex;
                align-items: center;
                justify-content: center;
                font-family: var(--font-display);
                font-size: 24px;
                font-weight: 800;
                color: var(--bg-deep);
                box-shadow: 0 0 30px var(--accent-cyan-glow);
            }

            .header-title {
                font-family: var(--font-display);
                font-size: 2.4rem;
                font-weight: 800;
                letter-spacing: -1px;
                background: linear-gradient(135deg, var(--text-primary) 0%, var(--accent-cyan) 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }

            .header-subtitle {
                font-size: 0.95rem;
                color: var(--text-muted);
                margin-top: 4px;
            }

            .header-features {
                display: flex;
                gap: 20px;
                flex-wrap: wrap;
            }

            .feature-tag {
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 8px 14px;
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                border-radius: 100px;
                font-size: 0.8rem;
                color: var(--text-secondary);
            }

            .feature-tag .dot {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: var(--accent-green);
            }

            /* Health Status Cards */
            .health-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 16px;
                margin-bottom: 40px;
            }

            .health-card {
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 20px;
                position: relative;
                overflow: hidden;
            }

            .health-card::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 3px;
                background: linear-gradient(90deg, var(--accent-cyan), transparent);
            }

            .health-card.success::before { background: linear-gradient(90deg, var(--accent-green), transparent); }
            .health-card.warning::before { background: linear-gradient(90deg, var(--accent-amber), transparent); }
            .health-card.error::before { background: linear-gradient(90deg, var(--accent-red), transparent); }

            .health-label {
                font-size: 0.75rem;
                text-transform: uppercase;
                letter-spacing: 1px;
                color: var(--text-muted);
                margin-bottom: 8px;
            }

            .health-value {
                font-family: var(--font-mono);
                font-size: 1.8rem;
                font-weight: 600;
                color: var(--text-primary);
            }

            .health-value.accent-cyan { color: var(--accent-cyan); }
            .health-value.accent-green { color: var(--accent-green); }
            .health-value.accent-red { color: var(--accent-red); }

            .health-status-indicator {
                display: flex;
                align-items: center;
                gap: 8px;
                margin-top: 8px;
                font-size: 0.85rem;
            }

            .status-dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: var(--accent-green);
                box-shadow: 0 0 10px var(--accent-green-glow);
                animation: pulse 2s infinite;
            }

            .status-dot.offline { background: var(--accent-red); box-shadow: 0 0 10px var(--accent-red-glow); animation: none; }

            @keyframes pulse {
                0%, 100% { opacity: 1; transform: scale(1); }
                50% { opacity: 0.7; transform: scale(1.1); }
            }

            /* Section Titles */
            .section-header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-bottom: 20px;
                flex-wrap: wrap;
                gap: 16px;
            }

            .section-title {
                font-family: var(--font-display);
                font-size: 1.5rem;
                font-weight: 700;
                display: flex;
                align-items: center;
                gap: 12px;
            }

            .section-title .count {
                font-family: var(--font-mono);
                font-size: 0.9rem;
                padding: 4px 12px;
                background: var(--bg-elevated);
                border-radius: 100px;
                color: var(--accent-cyan);
            }

            .section-stats {
                display: flex;
                align-items: center;
                gap: 24px;
            }

            .stat-item {
                display: flex;
                align-items: center;
                gap: 8px;
            }

            .stat-value {
                font-family: var(--font-mono);
                font-size: 1.1rem;
                font-weight: 600;
            }

            .stat-value.green { color: var(--accent-green); }
            .stat-value.blue { color: var(--accent-cyan); }

            .stat-label {
                font-size: 0.85rem;
                color: var(--text-muted);
            }

            .section-actions {
                display: flex;
                gap: 8px;
            }

            /* Cost Tracker Panel */
            .cost-panel-content {
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-lg);
                padding: 24px;
                margin-top: 16px;
            }

            .cost-cards {
                display: grid;
                grid-template-columns: repeat(5, 1fr);
                gap: 16px;
            }

            .cost-card {
                background: var(--bg-elevated);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 16px;
                text-align: center;
            }

            .cost-card.projection {
                border-color: var(--accent-amber);
                background: var(--accent-amber-glow);
            }

            .cost-label {
                font-size: 0.8rem;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.5px;
                margin-bottom: 8px;
            }

            .cost-value {
                font-family: var(--font-mono);
                font-size: 1.5rem;
                font-weight: 600;
                color: var(--accent-green);
            }

            .cost-card.projection .cost-value {
                color: var(--accent-amber);
            }

            .cost-subtext {
                font-family: var(--font-mono);
                font-size: 0.85rem;
                color: var(--text-secondary);
                margin-top: 4px;
            }

            .cost-footer {
                display: flex;
                align-items: center;
                margin-top: 16px;
                padding-top: 16px;
                border-top: 1px solid var(--border-subtle);
            }

            .cost-refresh-btn {
                margin-left: auto;
                padding: 8px 16px;
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-sm);
                color: var(--text-secondary);
                cursor: pointer;
                font-size: 0.9rem;
                transition: all 0.2s;
            }

            .cost-refresh-btn:hover {
                background: var(--bg-hover);
                color: var(--text-primary);
                border-color: var(--border-hover);
            }

            @media (max-width: 1200px) {
                .cost-cards {
                    grid-template-columns: repeat(3, 1fr);
                }
            }

            @media (max-width: 768px) {
                .cost-cards {
                    grid-template-columns: repeat(2, 1fr);
                }
            }

            /* Scraper Cards */
            .scraper {
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-lg);
                margin-bottom: 12px;
                overflow: hidden;
                transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            }

            .scraper:hover {
                border-color: var(--border-hover);
                transform: translateY(-2px);
                box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            }

            .scraper.running {
                border-left: 4px solid var(--accent-green);
            }

            .scraper.stopped {
                border-left: 4px solid var(--text-muted);
            }

            .scraper.error, .scraper.failed {
                border-left: 4px solid var(--accent-red);
            }

            .scraper-header {
                padding: 20px 24px;
                cursor: pointer;
                display: flex;
                justify-content: space-between;
                align-items: center;
                user-select: none;
                transition: background 0.2s;
            }

            .scraper-header:hover {
                background: var(--bg-hover);
            }

            .scraper-title {
                display: flex;
                align-items: center;
                gap: 14px;
                flex: 1;
            }

            .scraper-title h3 {
                margin: 0;
                font-family: var(--font-body);
                font-size: 1.15rem;
                font-weight: 600;
                color: var(--text-primary);
            }

            .scraper-summary {
                display: flex;
                align-items: center;
                gap: 20px;
                font-size: 0.9rem;
                color: var(--text-secondary);
                flex-wrap: wrap;
            }

            .scraper-stat {
                display: flex;
                align-items: center;
                gap: 6px;
                font-family: var(--font-mono);
            }

            .scraper-stat .value {
                color: var(--accent-green);
                font-weight: 500;
            }

            .scraper-stat .value.blue {
                color: var(--accent-cyan);
            }

            .expand-icon {
                width: 32px;
                height: 32px;
                display: flex;
                align-items: center;
                justify-content: center;
                border-radius: 8px;
                background: var(--bg-elevated);
                color: var(--text-muted);
                transition: all 0.3s;
                font-size: 0.8rem;
            }

            .expanded .expand-icon {
                transform: rotate(180deg);
                background: var(--accent-cyan-glow);
                color: var(--accent-cyan);
            }

            .scraper-details {
                max-height: 0;
                overflow: hidden;
                transition: max-height 0.4s cubic-bezier(0.4, 0, 0.2, 1);
                border-top: 1px solid transparent;
            }

            .scraper-details.show {
                max-height: 2000px;
                border-top-color: var(--border-subtle);
            }

            .scraper-content {
                padding: 24px;
                background: rgba(0,0,0,0.2);
            }

            .scraper-meta-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
                gap: 16px;
                margin-bottom: 20px;
            }

            .meta-item {
                display: flex;
                flex-direction: column;
                gap: 4px;
            }

            .meta-label {
                font-size: 0.75rem;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                color: var(--text-muted);
            }

            .meta-value {
                font-family: var(--font-mono);
                font-size: 0.9rem;
                color: var(--text-primary);
            }

            /* Subreddit Grid in Details */
            .subreddit-grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
                gap: 8px;
                margin: 16px 0;
            }

            .subreddit-chip {
                background: var(--bg-elevated);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-sm);
                padding: 8px 12px;
                font-size: 0.8rem;
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 8px;
                overflow: hidden;
                min-width: 0;
            }

            .subreddit-chip .name {
                color: var(--accent-purple);
                font-weight: 500;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                min-width: 0;
                flex: 1;
            }

            .subreddit-chip .stats {
                font-family: var(--font-mono);
                font-size: 0.7rem;
                color: var(--text-muted);
                white-space: nowrap;
                flex-shrink: 0;
            }

            /* Database Stats Box */
            .db-stats-box {
                background: var(--bg-elevated);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 16px 20px;
                margin: 16px 0;
            }

            .db-stats-title {
                font-size: 0.8rem;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                color: var(--text-muted);
                margin-bottom: 12px;
                display: flex;
                align-items: center;
                gap: 8px;
            }

            .db-stats-row {
                display: flex;
                gap: 32px;
                flex-wrap: wrap;
            }

            .db-stat {
                display: flex;
                align-items: baseline;
                gap: 8px;
            }

            .db-stat .num {
                font-family: var(--font-mono);
                font-size: 1.2rem;
                font-weight: 600;
            }

            .db-stat .num.green { color: var(--accent-green); }
            .db-stat .num.blue { color: var(--accent-cyan); }

            .db-stat .label {
                font-size: 0.85rem;
                color: var(--text-muted);
            }

            .db-stats-meta {
                font-size: 0.8rem;
                color: var(--text-muted);
                margin-top: 12px;
                line-height: 1.6;
            }

            /* Buttons */
            button {
                font-family: var(--font-body);
                padding: 10px 18px;
                margin: 4px 4px 4px 0;
                border: 1px solid var(--border-default);
                border-radius: var(--radius-sm);
                cursor: pointer;
                transition: all 0.2s ease;
                font-weight: 500;
                font-size: 0.85rem;
                background: var(--bg-elevated);
                color: var(--text-primary);
            }

            button:hover {
                background: var(--bg-hover);
                border-color: var(--border-hover);
                transform: translateY(-1px);
            }

            button:active { transform: translateY(0) scale(0.98); }
            button:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }

            .btn-primary {
                background: linear-gradient(135deg, var(--accent-cyan), #00b8d4);
                border: none;
                color: var(--bg-deep);
                font-weight: 600;
                box-shadow: 0 4px 20px var(--accent-cyan-glow);
            }

            .btn-primary:hover {
                box-shadow: 0 6px 30px var(--accent-cyan-glow);
                transform: translateY(-2px);
            }

            .start {
                background: linear-gradient(135deg, var(--accent-green), #00cc6a);
                border: none;
                color: var(--bg-deep);
                font-weight: 600;
                box-shadow: 0 4px 20px var(--accent-green-glow);
            }

            .start:hover {
                box-shadow: 0 6px 30px var(--accent-green-glow);
            }

            .stop {
                background: var(--accent-red);
                border-color: var(--accent-red);
                color: white;
            }

            .stop:hover {
                box-shadow: 0 4px 20px var(--accent-red-glow);
            }

            .restart {
                background: var(--accent-amber);
                border-color: var(--accent-amber);
                color: var(--bg-deep);
            }

            .restart:hover {
                box-shadow: 0 4px 20px var(--accent-amber-glow);
            }

            .delete {
                background: transparent;
                border-color: var(--accent-red);
                color: var(--accent-red);
            }

            .delete:hover {
                background: var(--accent-red-glow);
            }

            .stats {
                background: var(--bg-elevated);
                border-color: var(--border-default);
            }

            .loading {
                background: var(--bg-hover) !important;
                border-color: var(--border-subtle) !important;
                cursor: wait !important;
            }

            .spinner {
                display: inline-block;
                width: 14px;
                height: 14px;
                border: 2px solid var(--border-default);
                border-radius: 50%;
                border-top-color: var(--accent-cyan);
                animation: spin 0.8s linear infinite;
                margin-right: 8px;
                vertical-align: middle;
            }

            @keyframes spin { to { transform: rotate(360deg); } }

            /* Skeleton Loading */
            .scrapers-loading {
                display: flex;
                flex-direction: column;
                gap: 1rem;
            }

            .skeleton-card {
                background: linear-gradient(90deg, var(--bg-tertiary) 25%, var(--bg-secondary) 50%, var(--bg-tertiary) 75%);
                background-size: 200% 100%;
                animation: shimmer 1.5s infinite;
                border-radius: var(--radius-md);
                height: 80px;
                border: 1px solid var(--border-subtle);
            }

            @keyframes shimmer {
                0% { background-position: 200% 0; }
                100% { background-position: -200% 0; }
            }

            .error-state {
                text-align: center;
                padding: 3rem;
                color: var(--text-secondary);
            }

            .error-state p {
                margin: 1rem 0;
                color: var(--text-secondary);
            }

            /* Form Elements */
            input, select, textarea {
                font-family: var(--font-body);
                padding: 12px 16px;
                margin: 5px 8px 5px 0;
                border: 1px solid var(--border-default);
                border-radius: var(--radius-sm);
                background: var(--bg-elevated);
                color: var(--text-primary);
                transition: all 0.2s ease;
                font-size: 0.9rem;
            }

            input:focus, select:focus, textarea:focus {
                outline: none;
                border-color: var(--accent-cyan);
                background: var(--bg-hover);
                box-shadow: 0 0 0 3px var(--accent-cyan-glow);
            }

            input::placeholder, textarea::placeholder { color: var(--text-muted); }

            input[type="text"], input[type="password"] { min-width: 220px; }
            input[type="number"] { width: 120px; font-family: var(--font-mono); }
            select { min-width: 180px; cursor: pointer; }

            /* Form Sections */
            .form-section {
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                padding: 28px;
                border-radius: var(--radius-lg);
                margin: 20px 0;
                position: relative;
            }

            .form-section h3 {
                margin: 0 0 20px 0;
                font-family: var(--font-display);
                font-size: 1.1rem;
                font-weight: 700;
                color: var(--text-primary);
                display: flex;
                align-items: center;
                gap: 10px;
            }

            .form-section h3::before {
                content: '';
                width: 4px;
                height: 20px;
                background: var(--accent-cyan);
                border-radius: 2px;
            }

            .credentials-section {
                background: var(--bg-elevated);
                padding: 24px;
                border-radius: var(--radius-md);
                margin: 16px 0;
                border: 1px solid var(--border-subtle);
            }

            .form-row {
                margin: 16px 0;
                display: flex;
                align-items: center;
                flex-wrap: wrap;
                gap: 12px;
            }

            .form-row label {
                display: inline-block;
                min-width: 130px;
                color: var(--text-secondary);
                font-weight: 500;
                font-size: 0.85rem;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            .form-row small { color: var(--text-muted); margin-left: 8px; font-size: 0.8rem; }

            .form-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
            }

            .form-grid .form-row {
                flex-direction: column;
                align-items: flex-start;
                margin: 0;
            }

            .form-grid .form-row label {
                margin-bottom: 8px;
            }

            .form-grid .form-row input,
            .form-grid .form-row select {
                width: 100%;
                margin: 0;
            }

            /* Badges */
            .mode-badge {
                display: inline-flex;
                align-items: center;
                padding: 5px 14px;
                border-radius: 100px;
                font-family: var(--font-mono);
                font-size: 0.75rem;
                font-weight: 500;
                margin-left: 10px;
            }

            .mode-badge.single {
                background: rgba(0,229,255,0.15);
                color: var(--accent-cyan);
                border: 1px solid rgba(0,229,255,0.3);
            }

            .mode-badge.multi {
                background: rgba(168,85,247,0.15);
                color: var(--accent-purple);
                border: 1px solid rgba(168,85,247,0.3);
            }

            .status-badge {
                padding: 5px 12px;
                border-radius: var(--radius-sm);
                font-family: var(--font-mono);
                font-size: 0.7rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            .badge-running {
                background: var(--accent-green-glow);
                color: var(--accent-green);
                border: 1px solid var(--accent-green);
                animation: pulse-badge 2s infinite;
            }

            @keyframes pulse-badge {
                0%, 100% { box-shadow: 0 0 0 0 var(--accent-green-glow); }
                50% { box-shadow: 0 0 0 6px transparent; }
            }

            .badge-stopped {
                background: rgba(90,100,120,0.2);
                color: var(--text-muted);
                border: 1px solid var(--text-muted);
            }

            .badge-error, .badge-failed {
                background: var(--accent-red-glow);
                color: var(--accent-red);
                border: 1px solid var(--accent-red);
            }

            /* Toggle Switch - Compact */
            .toggle { position: relative; display: inline-block; width: 36px; height: 20px; }
            .toggle input { opacity: 0; width: 0; height: 0; }

            .slider {
                position: absolute;
                cursor: pointer;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background-color: var(--bg-elevated);
                border: 1px solid var(--border-default);
                transition: .3s;
                border-radius: 20px;
            }

            .slider:before {
                position: absolute;
                content: "";
                height: 14px;
                width: 14px;
                left: 2px;
                bottom: 2px;
                background-color: var(--text-muted);
                transition: .3s;
                border-radius: 50%;
            }

            input:checked + .slider {
                background-color: var(--accent-green);
                border-color: var(--accent-green);
            }

            input:checked + .slider:before {
                transform: translateX(16px);
                background-color: white;
            }

            /* Sorting Options - Compact Chips */
            .sorting-grid {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-top: 4px;
            }

            .sorting-option {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                padding: 6px 12px;
                background: var(--bg-elevated);
                border: 1px solid var(--border-subtle);
                border-radius: 16px;
                cursor: pointer;
                transition: all 0.2s;
                font-size: 0.8rem;
            }

            .sorting-option:hover {
                border-color: var(--accent-cyan);
                background: var(--bg-hover);
            }

            .sorting-option:has(input:checked) {
                border-color: var(--accent-cyan);
                background: rgba(0, 229, 255, 0.1);
            }

            .sorting-option input {
                width: 14px;
                height: 14px;
                margin: 0;
                accent-color: var(--accent-cyan);
            }

            .sorting-option .sort-name {
                font-weight: 500;
                color: var(--text-primary);
                text-transform: uppercase;
                font-size: 0.7rem;
                letter-spacing: 0.5px;
            }

            /* Loading Overlay */
            .loading-overlay {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background-color: rgba(5,8,16,0.9);
                backdrop-filter: blur(8px);
                z-index: 1000;
            }

            .loading-message {
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                background: var(--bg-card);
                padding: 40px 50px;
                border-radius: var(--radius-lg);
                text-align: center;
                border: 1px solid var(--border-subtle);
                box-shadow: var(--shadow-glow);
            }

            .loading-message .spinner {
                width: 24px;
                height: 24px;
                border-width: 3px;
            }

            /* Modal */
            .modal-overlay {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background-color: rgba(5,8,16,0.9);
                backdrop-filter: blur(8px);
                z-index: 1001;
            }

            .modal-content {
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                background: var(--bg-card);
                padding: 32px;
                border-radius: var(--radius-lg);
                width: 90%;
                max-width: 680px;
                max-height: 85%;
                overflow-y: auto;
                border: 1px solid var(--border-subtle);
                box-shadow: var(--shadow-glow);
            }

            .modal-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 28px;
                padding-bottom: 20px;
                border-bottom: 1px solid var(--border-subtle);
            }

            .modal-header h2 {
                font-family: var(--font-display);
                font-size: 1.4rem;
                font-weight: 700;
            }

            .modal-close {
                background: none;
                border: none;
                color: var(--text-muted);
                font-size: 28px;
                cursor: pointer;
                padding: 0;
                line-height: 1;
                transition: color 0.2s;
            }

            .modal-close:hover {
                color: var(--text-primary);
            }

            /* Account Cards */
            .account-card {
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 16px 20px;
                margin-bottom: 8px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 16px;
                transition: all 0.2s;
            }

            .account-card:hover {
                border-color: var(--border-hover);
                background: var(--bg-hover);
            }

            .account-info {
                display: flex;
                align-items: center;
                gap: 12px;
            }

            .account-name {
                font-family: var(--font-display);
                font-weight: 600;
                color: var(--text-primary);
                font-size: 1rem;
            }

            .account-username {
                color: var(--text-muted);
                font-size: 0.8rem;
                font-family: var(--font-mono);
            }

            .account-stats {
                display: flex;
                gap: 24px;
            }

            .account-stat {
                text-align: center;
                min-width: 60px;
            }

            .account-stat-value {
                font-family: var(--font-mono);
                font-size: 1.25rem;
                font-weight: 600;
                color: var(--text-primary);
            }

            .account-stat-label {
                font-size: 0.65rem;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            /* Empty State */
            .empty-state {
                text-align: center;
                padding: 60px 20px;
                color: var(--text-muted);
            }

            .empty-state-icon {
                font-size: 48px;
                margin-bottom: 16px;
                opacity: 0.5;
            }

            .empty-state-text {
                font-size: 1.1rem;
                margin-bottom: 8px;
            }

            .empty-state-hint {
                font-size: 0.9rem;
                color: var(--text-muted);
            }

            /* Links */
            a { color: var(--accent-cyan); text-decoration: none; transition: all 0.2s; }
            a:hover { color: var(--text-primary); text-decoration: underline; }

            /* Utility classes */
            .text-muted { color: var(--text-muted); }

            /* Scrollbar */
            ::-webkit-scrollbar { width: 8px; height: 8px; }
            ::-webkit-scrollbar-track { background: var(--bg-primary); }
            ::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 4px; }
            ::-webkit-scrollbar-thumb:hover { background: var(--border-hover); }

            /* Responsive */
            @media (max-width: 768px) {
                .dashboard-container { padding: 20px; }
                .header-top { flex-direction: column; }
                .header-title { font-size: 1.8rem; }
                .section-header { flex-direction: column; align-items: flex-start; }
            }
        </style>
    </head>
    <body>
        <div class="loading-overlay" id="loadingOverlay">
            <div class="loading-message">
                <div class="spinner"></div>
                <span id="loadingText">Processing...</span>
            </div>
        </div>

        <div class="dashboard-container">
            <!-- Header -->
            <header class="header">
                <div class="header-top">
                    <div class="header-brand">
                        <div class="logo-mark">R</div>
                        <div>
                            <h1 class="header-title">Reddit Scraper</h1>
                            <p class="header-subtitle">Mission Control Dashboard</p>
                        </div>
                    </div>
                    <div class="header-features">
                        <div class="feature-tag"><span class="dot"></span> Persistent Storage</div>
                        <div class="feature-tag"><span class="dot"></span> Auto-Restart</div>
                        <div class="feature-tag"><span class="dot"></span> Multi-Account</div>
                    </div>
                </div>
            </header>

            <!-- Health Status -->
            <div id="health-status"></div>

            <!-- Scrapers List -->
            <div id="scrapers"></div>

            <!-- Reddit Accounts -->
            <section id="accounts" style="margin-top: 32px;"></section>

            <!-- API Cost Tracker -->
            <section id="cost-tracker" style="margin-top: 32px;">
                <div class="section-header" style="cursor: pointer;" onclick="toggleCostPanel()">
                    <h2 class="section-title">💰 API Cost Tracker</h2>
                    <span id="cost-toggle" style="color: var(--text-muted); font-size: 0.9rem;">▼</span>
                </div>
                <div id="cost-content" class="cost-panel-content">
                    <div class="cost-cards">
                        <div class="cost-card">
                            <div class="cost-label">Today</div>
                            <div class="cost-value" id="costToday">$0.00</div>
                            <div class="cost-subtext" id="reqsToday">0 requests</div>
                        </div>
                        <div class="cost-card">
                            <div class="cost-label">Last Hour</div>
                            <div class="cost-value" id="costHour">$0.00</div>
                            <div class="cost-subtext" id="reqsHour">0 requests</div>
                        </div>
                        <div class="cost-card">
                            <div class="cost-label">Avg/Hour</div>
                            <div class="cost-value" id="costAvgHour">$0.00</div>
                            <div class="cost-subtext" id="reqsAvgHour">0 requests</div>
                        </div>
                        <div class="cost-card">
                            <div class="cost-label">Avg/Day</div>
                            <div class="cost-value" id="costAvgDay">$0.00</div>
                            <div class="cost-subtext" id="reqsAvgDay">0 requests</div>
                        </div>
                        <div class="cost-card projection">
                            <div class="cost-label">Monthly</div>
                            <div class="cost-value" id="costMonthly">$0.00</div>
                            <div class="cost-subtext" id="reqsMonthly">0 requests</div>
                        </div>
                    </div>
                    <div class="cost-footer">
                        <span style="color: var(--text-muted);">Pricing: $0.24 per 1,000 requests</span>
                        <span id="cost-updated" style="color: var(--text-muted); margin-left: 20px;"></span>
                        <button onclick="fetchCostData()" class="cost-refresh-btn">↻ Refresh</button>
                    </div>
                </div>
            </section>

            <!-- Start New Scraper -->
            <section style="margin-top: 48px;">
                <div class="section-header">
                    <h2 class="section-title">Launch New Scraper</h2>
                </div>

                <!-- Target Selection -->
                <div class="form-section">
                    <h3>Target Selection</h3>
                    <div class="form-row">
                        <label>Mode:</label>
                        <select id="scraper_mode" onchange="toggleSubredditInput()" style="min-width: 260px;">
                            <option value="single">Single Subreddit</option>
                            <option value="multi">Multi-Subreddit (up to 100)</option>
                        </select>
                        <span id="mode-indicator" class="mode-badge single">1 subreddit</span>
                    </div>

                    <div class="form-row">
                        <label>Scraper Name:</label>
                        <input type="text" id="scraper_name" placeholder="e.g. Finance Bundle (optional)" style="min-width: 300px;" />
                        <small style="color: var(--text-muted);">Custom name for this scraper</small>
                    </div>

                    <div id="single-subreddit-input">
                        <div class="form-row">
                            <label>Subreddit:</label>
                            <input type="text" id="subreddit" placeholder="e.g. wallstreetbets" style="min-width: 300px;" />
                            <select id="preset" style="min-width: 280px;">
                                <option value="custom">Custom Settings</option>
                                <option value="high">High Activity (wsb, stocks)</option>
                                <option value="medium">Medium Activity (investing)</option>
                                <option value="low">Low Activity (niche subs)</option>
                            </select>
                        </div>
                    </div>

                    <div id="multi-subreddit-input" style="display: none;">
                        <div class="form-row" style="align-items: flex-start;">
                            <label style="padding-top: 12px;">Subreddits:</label>
                            <div style="flex: 1; max-width: 550px;">
                                <textarea id="subreddits" placeholder="stocks, investing, wallstreetbets, options, stockmarket, pennystocks, daytrading, thetagang, valueinvesting, dividends"
                                    style="width: 100%; height: 90px; resize: vertical;"></textarea>
                                <small style="display: block; margin-top: 8px;">Comma-separated list. Max 100 subreddits per container.</small>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Scraper Configuration -->
                <div class="form-section">
                    <h3>Configuration</h3>
                    <div class="form-row">
                        <label>Scraper Type:</label>
                        <select id="scraper_type">
                            <option value="posts">Posts Scraper</option>
                            <option value="comments">Comments Scraper</option>
                        </select>
                    </div>

                    <div class="form-grid" style="margin-top: 20px;">
                        <div class="form-row">
                            <label>Posts Limit</label>
                            <input type="number" id="posts_limit" value="1000" />
                        </div>
                        <div class="form-row">
                    <label>Interval (sec)</label>
                    <input type="number" id="interval" value="60" />
                </div>
                <div class="form-row">
                    <label>Comment Batch</label>
                    <input type="number" id="comment_batch" value="50" />
                </div>
                <div class="form-row">
                    <label>Auto-restart</label>
                    <label class="toggle" style="margin-top: 4px;">
                        <input type="checkbox" id="auto_restart" checked>
                        <span class="slider"></span>
                    </label>
                </div>
            </div>

            <div class="form-row" style="margin-top: 16px;">
                <label>Sorting:</label>
                <div class="sorting-grid">
                    <label class="sorting-option">
                        <input type="checkbox" name="sorting" value="new" checked>
                        <span class="sort-name">new</span>
                    </label>
                    <label class="sorting-option">
                        <input type="checkbox" name="sorting" value="hot" checked>
                        <span class="sort-name">hot</span>
                    </label>
                    <label class="sorting-option">
                        <input type="checkbox" name="sorting" value="rising" checked>
                        <span class="sort-name">rising</span>
                    </label>
                    <label class="sorting-option">
                        <input type="checkbox" name="sorting" value="top">
                        <span class="sort-name">top</span>
                    </label>
                    <label class="sorting-option">
                        <input type="checkbox" name="sorting" value="controversial">
                        <span class="sort-name">controversial</span>
                    </label>
                </div>
            </div>
        </div>

        <!-- Reddit Account -->
        <div class="form-section">
            <h3>Reddit Account</h3>
            <div class="form-row">
                <label>Account:</label>
                <select id="account_type" onchange="toggleAccountType()">
                    <option value="saved">Use Saved Account</option>
                    <option value="manual">Enter Manually</option>
                </select>
            </div>

            <!-- Saved Account Selection -->
            <div id="saved_account_section">
                <div class="form-row">
                    <label>Select:</label>
                    <select id="saved_account_name" style="min-width: 240px;">
                        <option value="">Choose an account...</option>
                    </select>
                    <button onclick="loadSavedAccounts()" class="stats">Refresh</button>
                    <button onclick="showAccountManager()" class="stats">Manage</button>
                </div>
            </div>

            <!-- Manual Credentials -->
            <div id="manual_credentials_section" style="display: none;">
                <div class="credentials-section">
                    <div class="form-grid">
                        <div class="form-row">
                            <label>Client ID</label>
                            <input type="text" id="client_id" placeholder="Reddit app client ID" />
                        </div>
                        <div class="form-row">
                            <label>Client Secret</label>
                            <input type="password" id="client_secret" placeholder="Reddit app secret" />
                        </div>
                        <div class="form-row">
                            <label>Username</label>
                            <input type="text" id="username" placeholder="Reddit username" />
                        </div>
                        <div class="form-row">
                            <label>Password</label>
                            <input type="password" id="password" placeholder="Reddit password" />
                        </div>
                    </div>
                    <div class="form-row" style="margin-top: 16px;">
                        <label>User Agent:</label>
                        <input type="text" id="user_agent" placeholder="RedditScraper/1.0 by YourUsername" style="flex: 1; max-width: 400px;" />
                    </div>
                    <div class="form-row">
                        <label>Save as:</label>
                        <input type="text" id="save_account_as" placeholder="Account name (optional)" />
                        <small>Save for future use</small>
                    </div>
                    <p style="margin: 12px 0 0 0;"><small style="color: var(--text-muted);">Get credentials at <a href="https://www.reddit.com/prefs/apps" target="_blank">reddit.com/prefs/apps</a></small></p>
                </div>
            </div>
        </div>

        <div style="margin-top: 24px;">
            <button onclick="startScraper()" class="start" id="startScraperBtn" style="padding: 14px 32px; font-size: 15px;">Start Scraper</button>
        </div>
        
        <!-- Account Manager Modal -->
        <div id="accountManagerModal" class="modal-overlay">
            <div class="modal-content">
                <div class="modal-header">
                    <h2>Account Manager</h2>
                    <button onclick="hideAccountManager()" class="modal-close">&times;</button>
                </div>

                <div class="form-section" style="margin-top: 0;">
                    <h3>Add New Account</h3>
                    <div class="form-grid">
                        <div class="form-row">
                            <label>Account Name</label>
                            <input type="text" id="new_account_name" placeholder="e.g. main_account" />
                        </div>
                        <div class="form-row">
                            <label>Username</label>
                            <input type="text" id="new_username" placeholder="Reddit username" />
                        </div>
                        <div class="form-row">
                            <label>Client ID</label>
                            <input type="text" id="new_client_id" placeholder="Reddit app client ID" />
                        </div>
                        <div class="form-row">
                            <label>Client Secret</label>
                            <input type="password" id="new_client_secret" placeholder="Reddit app secret" />
                        </div>
                        <div class="form-row">
                            <label>Password</label>
                            <input type="password" id="new_password" placeholder="Reddit password" />
                        </div>
                        <div class="form-row">
                            <label>User Agent</label>
                            <input type="text" id="new_user_agent" placeholder="Scraper/1.0 by user" />
                        </div>
                    </div>
                    <div style="margin-top: 16px;">
                        <button onclick="saveNewAccount()" class="start">Save Account</button>
                    </div>
                </div>

                <div class="form-section">
                    <h3>Saved Accounts</h3>
                    <div id="savedAccountsList" style="min-height: 60px;"></div>
                </div>

                <div style="text-align: center; margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border-subtle);">
                    <button onclick="hideAccountManager()" class="stats" style="padding: 10px 30px;">Close</button>
                </div>
            </div>
        </div>
        
        <script>
            const presets = {
                high: {
                    posts_limit: 150,
                    interval: 60,
                    comment_batch: 12,
                    sorting_methods: ['new', 'top', 'rising']
                },
                medium: {
                    posts_limit: 100,
                    interval: 60,
                    comment_batch: 12,
                    sorting_methods: ['new', 'top', 'rising']
                },
                low: {
                    posts_limit: 80,
                    interval: 60,
                    comment_batch: 10,
                    sorting_methods: ['new', 'top', 'rising']
                }
            };
            
            // Loading state management
            function showButtonLoading(buttonId, text = 'Loading...') {
                const button = document.getElementById(buttonId) || document.querySelector(`[onclick*="${buttonId}"]`);
                if (button) {
                    button.disabled = true;
                    button.classList.add('loading');
                    button.dataset.originalText = button.innerHTML;
                    button.innerHTML = `<span class="spinner"></span>${text}`;
                }
            }
            
            function hideButtonLoading(buttonId) {
                const button = document.getElementById(buttonId) || document.querySelector(`[onclick*="${buttonId}"]`);
                if (button && button.dataset.originalText) {
                    button.disabled = false;
                    button.classList.remove('loading');
                    button.innerHTML = button.dataset.originalText;
                    delete button.dataset.originalText;
                }
            }
            
            function showGlobalLoading(text = 'Processing...') {
                document.getElementById('loadingText').textContent = text;
                document.getElementById('loadingOverlay').style.display = 'block';
            }
            
            function hideGlobalLoading() {
                document.getElementById('loadingOverlay').style.display = 'none';
            }
            
            // Button click handlers with loading states
            function setButtonLoading(button, isLoading, loadingText = 'Loading...') {
                if (isLoading) {
                    button.disabled = true;
                    button.classList.add('loading');
                    button.dataset.originalText = button.innerHTML;
                    button.innerHTML = `<span class="spinner"></span>${loadingText}`;
                } else {
                    button.disabled = false;
                    button.classList.remove('loading');
                    if (button.dataset.originalText) {
                        button.innerHTML = button.dataset.originalText;
                        delete button.dataset.originalText;
                    }
                }
            }
            
            // Make credentials section collapsible (if exists)
            const collapsible = document.querySelector('.collapsible');
            if (collapsible) {
                collapsible.onclick = function() {
                    const content = this.nextElementSibling;
                    content.style.display = content.style.display === 'block' ? 'none' : 'block';
                };
            }
            
            document.getElementById('preset').onchange = function() {
                const preset = presets[this.value];
                if (preset) {
                    document.getElementById('posts_limit').value = preset.posts_limit;
                    document.getElementById('interval').value = preset.interval;
                    document.getElementById('comment_batch').value = preset.comment_batch;

                    // Update sorting method checkboxes
                    if (preset.sorting_methods) {
                        document.querySelectorAll('input[name="sorting"]').forEach(checkbox => {
                            checkbox.checked = preset.sorting_methods.includes(checkbox.value);
                        });
                    }
                }
            };
            
            async function loadHealthStatus() {
                try {
                    const response = await fetch('/health');
                    const health = await response.json();
                    const healthDiv = document.getElementById('health-status');

                    const dbStatus = health.database_connected;
                    const dockerStatus = health.docker_available;

                    healthDiv.innerHTML = `
                        <div class="health-grid">
                            <div class="health-card ${dbStatus ? 'success' : 'error'}">
                                <div class="health-label">Database</div>
                                <div class="health-value ${dbStatus ? 'accent-green' : 'accent-red'}">${dbStatus ? 'Online' : 'Offline'}</div>
                                <div class="health-status-indicator">
                                    <span class="status-dot ${dbStatus ? '' : 'offline'}"></span>
                                    <span>${dbStatus ? 'MongoDB Connected' : 'Connection Failed'}</span>
                                </div>
                            </div>

                            <div class="health-card ${dockerStatus ? 'success' : 'error'}">
                                <div class="health-label">Docker</div>
                                <div class="health-value ${dockerStatus ? 'accent-green' : 'accent-red'}">${dockerStatus ? 'Ready' : 'Unavailable'}</div>
                                <div class="health-status-indicator">
                                    <span class="status-dot ${dockerStatus ? '' : 'offline'}"></span>
                                    <span>${dockerStatus ? 'Engine Running' : 'Not Available'}</span>
                                </div>
                            </div>

                            <div class="health-card">
                                <div class="health-label">Total Scrapers</div>
                                <div class="health-value accent-cyan">${health.total_scrapers || 0}</div>
                                <div class="health-status-indicator">
                                    <span>Configured instances</span>
                                </div>
                            </div>

                            <div class="health-card success">
                                <div class="health-label">Running</div>
                                <div class="health-value accent-green">${health.running_containers || 0}</div>
                                <div class="health-status-indicator">
                                    <span class="status-dot"></span>
                                    <span>Active containers</span>
                                </div>
                            </div>

                            <div class="health-card ${health.failed_scrapers > 0 ? 'error' : ''}">
                                <div class="health-label">Failed</div>
                                <div class="health-value ${health.failed_scrapers > 0 ? 'accent-red' : ''}">${health.failed_scrapers || 0}</div>
                                <div class="health-status-indicator">
                                    <span>${health.failed_scrapers > 0 ? 'Needs attention' : 'All healthy'}</span>
                                </div>
                            </div>
                        </div>
                    `;
                } catch (error) {
                    console.error('Error loading health status:', error);
                    document.getElementById('health-status').innerHTML = `
                        <div class="health-grid">
                            <div class="health-card error">
                                <div class="health-label">System Status</div>
                                <div class="health-value accent-red">Error</div>
                                <div class="health-status-indicator">
                                    <span class="status-dot offline"></span>
                                    <span>Failed to load health status</span>
                                </div>
                            </div>
                        </div>
                    `;
                }
            }

            async function loadAccountStats() {
                const container = document.getElementById('accounts');
                if (!container) return;

                try {
                    const response = await fetch('/accounts/stats');
                    const stats = await response.json();
                    const accounts = Object.values(stats);

                    container.innerHTML = `
                        <div class="section-header">
                            <h2 class="section-title">Reddit Accounts <span class="count">${accounts.length}</span></h2>
                            <button onclick="showAccountManager()" class="stats">Manage</button>
                        </div>
                    `;

                    if (accounts.length === 0) {
                        container.innerHTML += `
                            <div class="empty-state" style="padding: 40px 20px;">
                                <div class="empty-state-icon">🔑</div>
                                <p class="empty-state-text">No saved accounts</p>
                                <p class="empty-state-hint">Add an account to start scraping</p>
                            </div>
                        `;
                        return;
                    }

                    accounts.forEach(account => {
                        const statusColor = account.running_count > 0 ? 'var(--accent-green)' : 'var(--text-muted)';
                        container.innerHTML += `
                            <div class="account-card">
                                <div class="account-info">
                                    <div>
                                        <div class="account-name">${account.account_name}</div>
                                        <div class="account-username">u/${account.username}</div>
                                    </div>
                                </div>
                                <div class="account-stats">
                                    <div class="account-stat">
                                        <div class="account-stat-value" style="color: ${statusColor}">${account.running_count}</div>
                                        <div class="account-stat-label">Active</div>
                                    </div>
                                    <div class="account-stat">
                                        <div class="account-stat-value">${account.scraper_count}</div>
                                        <div class="account-stat-label">Scrapers</div>
                                    </div>
                                    <div class="account-stat">
                                        <div class="account-stat-value">${account.subreddit_count}</div>
                                        <div class="account-stat-label">Subreddits</div>
                                    </div>
                                </div>
                            </div>
                        `;
                    });
                } catch (error) {
                    console.error('Error loading account stats:', error);
                }
            }

            function toggleScraper(header) {
                const scraper = header.closest('.scraper');
                const details = scraper.querySelector('.scraper-details');
                const isExpanded = details.classList.contains('show');

                if (isExpanded) {
                    details.classList.remove('show');
                    scraper.classList.remove('expanded');
                } else {
                    details.classList.add('show');
                    scraper.classList.add('expanded');
                }
            }

            function expandAllScrapers() {
                document.querySelectorAll('.scraper').forEach(scraper => {
                    const details = scraper.querySelector('.scraper-details');
                    details.classList.add('show');
                    scraper.classList.add('expanded');
                });
            }

            function collapseAllScrapers() {
                document.querySelectorAll('.scraper').forEach(scraper => {
                    const details = scraper.querySelector('.scraper-details');
                    details.classList.remove('show');
                    scraper.classList.remove('expanded');
                });
            }

            async function loadScrapers() {
                const container = document.getElementById('scrapers');

                try {
                    // Save expanded state before refresh
                    const expandedScrapers = new Set();
                    document.querySelectorAll('.scraper.expanded').forEach(el => {
                        if (el.dataset.subreddit) {
                            expandedScrapers.add(el.dataset.subreddit);
                        }
                    });

                    // Show skeleton loading on first load
                    if (!container.querySelector('.scraper')) {
                        container.innerHTML = `
                            <div class="section-header">
                                <h2 class="section-title">Active Scrapers</h2>
                            </div>
                            <div class="scrapers-loading">
                                <div class="skeleton-card"></div>
                                <div class="skeleton-card"></div>
                                <div class="skeleton-card"></div>
                            </div>
                        `;
                    }

                    const response = await fetch('/scrapers');
                    const scrapers = await response.json();
                    const scraperCount = Object.keys(scrapers).length;

                    // Calculate totals across all scrapers
                    let globalTotalPosts = 0;
                    let globalTotalComments = 0;
                    Object.values(scrapers).forEach(info => {
                        globalTotalPosts += info.database_totals?.total_posts || 0;
                        globalTotalComments += info.database_totals?.total_comments || 0;
                    });

                    container.innerHTML = `
                        <div class="section-header">
                            <h2 class="section-title">Active Scrapers <span class="count">${scraperCount}</span></h2>
                            <div class="section-stats">
                                <div class="stat-item">
                                    <span class="stat-value green">${globalTotalPosts.toLocaleString()}</span>
                                    <span class="stat-label">posts</span>
                                </div>
                                <div class="stat-item">
                                    <span class="stat-value blue">${globalTotalComments.toLocaleString()}</span>
                                    <span class="stat-label">comments</span>
                                </div>
                                ${scraperCount > 0 ? `
                                <div class="section-actions">
                                    <button onclick="expandAllScrapers()" class="stats">Expand All</button>
                                    <button onclick="collapseAllScrapers()" class="stats">Collapse All</button>
                                </div>
                                ` : ''}
                            </div>
                        </div>
                    `;

                    if (scraperCount === 0) {
                        container.innerHTML += `
                            <div class="empty-state">
                                <div class="empty-state-icon">📡</div>
                                <p class="empty-state-text">No active scrapers</p>
                                <p class="empty-state-hint">Launch a new scraper using the form below</p>
                            </div>
                        `;
                        return;
                    }
                    
                    Object.entries(scrapers).forEach(([subreddit, info]) => {
                        const statusClass = info.status || 'stopped';
                        const badgeClass = `badge-${statusClass}`;
                        const restartCount = info.restart_count || 0;
                        const autoRestart = info.config?.auto_restart !== false;

                        const totalPosts = (info.database_totals?.total_posts || 0).toLocaleString();
                        const totalComments = (info.database_totals?.total_comments || 0).toLocaleString();
                        const collectionRate = info.metrics ? `${(info.metrics.posts_per_hour || 0).toFixed(1)} posts/hr` : 'N/A';

                        // Handle multi-subreddit display
                        const allSubreddits = info.subreddits || [subreddit];
                        const isMulti = allSubreddits.length > 1;
                        const scraperName = info.name;
                        let displayTitle;
                        if (scraperName) {
                            // Use custom name
                            displayTitle = `${scraperName} <span class="text-muted" style="font-size: 0.85rem; font-weight: 400;">(${allSubreddits.length} sub${allSubreddits.length > 1 ? 's' : ''})</span>`;
                        } else if (isMulti) {
                            displayTitle = `r/${subreddit} <span class="text-muted" style="font-size: 0.85rem; font-weight: 400;">+${allSubreddits.length - 1} more</span>`;
                        } else {
                            displayTitle = `r/${subreddit}`;
                        }
                        const multiBadge = isMulti && !scraperName
                            ? `<span class="mode-badge multi">${allSubreddits.length} subs</span>`
                            : '';

                        const div = document.createElement('div');
                        div.className = `scraper ${statusClass}`;
                        div.dataset.subreddit = subreddit;
                        div.innerHTML = `
                            <div class="scraper-header" onclick="toggleScraper(this)">
                                <div class="scraper-title">
                                    <h3>${displayTitle}${multiBadge}</h3>
                                    <span class="status-badge ${badgeClass}">${info.status?.toUpperCase() || 'UNKNOWN'}</span>
                                </div>
                                <div class="scraper-summary">
                                    <div class="scraper-stat">
                                        <span class="value">📊 ${totalPosts}</span>
                                        <span>posts</span>
                                    </div>
                                    <div class="scraper-stat">
                                        <span class="value blue">${totalComments}</span>
                                        <span>comments</span>
                                    </div>
                                    <div class="scraper-stat">
                                        <span>⚡ ${collectionRate}</span>
                                    </div>
                                    <span class="expand-icon">▼</span>
                                </div>
                            </div>
                            <div class="scraper-details">
                                <div class="scraper-content">
                                    ${isMulti ? `
                                    <div class="meta-item" style="margin-bottom: 16px;">
                                        <span class="meta-label">Subreddits (${allSubreddits.length})</span>
                                        <div class="subreddit-grid">
                                            ${allSubreddits.map(s => {
                                                const stats = info.subreddit_stats?.[s] || { posts: 0, comments: 0 };
                                                return `<div class="subreddit-chip">
                                                    <span class="name">r/${s}</span>
                                                    <span class="stats">${stats.posts} / ${stats.comments}</span>
                                                </div>`;
                                            }).join('')}
                                        </div>
                                    </div>
                                    ` : ''}

                                    <div class="scraper-meta-grid">
                                        <div class="meta-item">
                                            <span class="meta-label">Reddit User</span>
                                            <span class="meta-value">${info.config?.credentials?.username || 'N/A'}</span>
                                        </div>
                                        <div class="meta-item">
                                            <span class="meta-label">Container</span>
                                            <span class="meta-value">${info.container_name || 'N/A'}</span>
                                        </div>
                                        <div class="meta-item">
                                            <span class="meta-label">Posts Limit</span>
                                            <span class="meta-value">${info.config?.posts_limit || 'N/A'}</span>
                                        </div>
                                        <div class="meta-item">
                                            <span class="meta-label">Interval</span>
                                            <span class="meta-value">${info.config?.interval || 'N/A'}s</span>
                                        </div>
                                    </div>

                                    <div class="db-stats-box">
                                        <div class="db-stats-title">📊 Database Totals</div>
                                        <div class="db-stats-row">
                                            <div class="db-stat">
                                                <span class="num green">${totalPosts}</span>
                                                <span class="label">posts</span>
                                            </div>
                                            <div class="db-stat">
                                                <span class="num blue">${totalComments}</span>
                                                <span class="label">comments</span>
                                            </div>
                                        </div>
                                        ${info.metrics ? `
                                        <div class="db-stats-meta">
                                            Scraper collected: ${(info.metrics.total_posts_collected || 0).toLocaleString()} posts (${(info.metrics.posts_per_hour || 0).toFixed(1)}/hr), ${(info.metrics.total_comments_collected || 0).toLocaleString()} comments (${(info.metrics.comments_per_hour || 0).toFixed(1)}/hr)<br>
                                            Last cycle: ${info.metrics.last_cycle_posts || 0} posts, ${info.metrics.last_cycle_comments || 0} comments
                                            ${info.metrics.last_cycle_time ? ` at ${new Date(info.metrics.last_cycle_time).toLocaleTimeString()}` : ''}
                                            ${info.metrics.total_cycles ? ` • ${info.metrics.total_cycles} cycles` : ''}
                                        </div>
                                        ` : ''}
                                    </div>

                                    <div class="scraper-meta-grid" style="margin-top: 16px;">
                                        <div class="meta-item">
                                            <span class="meta-label">Restarts</span>
                                            <span class="meta-value">${restartCount}</span>
                                        </div>
                                        <div class="meta-item" style="width: 70px;">
                                            <span class="meta-label">Auto-restart</span>
                                            <label class="toggle">
                                                <input type="checkbox" ${autoRestart ? 'checked' : ''} onchange="toggleAutoRestart('${subreddit}', this.checked)">
                                                <span class="slider"></span>
                                            </label>
                                        </div>
                                        ${info.started_at ? `
                                        <div class="meta-item">
                                            <span class="meta-label">Started</span>
                                            <span class="meta-value">${new Date(info.started_at).toLocaleString()}</span>
                                        </div>
                                        ` : ''}
                                        ${info.last_updated ? `
                                        <div class="meta-item">
                                            <span class="meta-label">Last Updated</span>
                                            <span class="meta-value">${new Date(info.last_updated).toLocaleString()}</span>
                                        </div>
                                        ` : ''}
                                    </div>

                                    ${info.last_error ? `<p style="color: var(--accent-red); margin-top: 12px;"><strong>Error:</strong> ${info.last_error}</p>` : ''}

                                    <div style="margin-top: 20px; display: flex; gap: 8px; flex-wrap: wrap;">
                                        <button onclick="event.stopPropagation(); stopScraper(this, '${subreddit}')" class="stop">Stop</button>
                                        <button onclick="event.stopPropagation(); restartScraper(this, '${subreddit}')" class="restart">Restart</button>
                                        <button onclick="event.stopPropagation(); openSubredditModal('${subreddit}', JSON.parse(this.dataset.subs))" data-subs='${JSON.stringify(allSubreddits)}' class="stats">Edit Subs</button>
                                        <button onclick="event.stopPropagation(); getStats(this, '${subreddit}')" class="stats">Stats</button>
                                        <button onclick="event.stopPropagation(); getLogs(this, '${subreddit}')" class="stats">Logs</button>
                                        <button onclick="event.stopPropagation(); deleteScraper(this, '${subreddit}')" class="delete">Delete</button>
                                    </div>
                                </div>
                            </div>
                        `;
                        container.appendChild(div);

                        // Restore expanded state
                        if (expandedScrapers.has(subreddit)) {
                            div.classList.add('expanded');
                            const details = div.querySelector('.scraper-details');
                            if (details) details.classList.add('show');
                        }
                    });
                } catch (error) {
                    console.error('Error loading scrapers:', error);
                    container.innerHTML = `
                        <div class="section-header">
                            <h2 class="section-title">Active Scrapers</h2>
                        </div>
                        <div class="error-state">
                            <span style="font-size: 2rem;">⚠️</span>
                            <p>Failed to load scrapers</p>
                            <button onclick="loadScrapers()" class="btn btn-secondary">Retry</button>
                        </div>
                    `;
                }
            }
            
            // Subreddit mode toggle
            function toggleSubredditInput() {
                const mode = document.getElementById('scraper_mode').value;
                const singleInput = document.getElementById('single-subreddit-input');
                const multiInput = document.getElementById('multi-subreddit-input');
                const modeIndicator = document.getElementById('mode-indicator');

                if (mode === 'single') {
                    singleInput.style.display = 'block';
                    multiInput.style.display = 'none';
                    modeIndicator.className = 'mode-badge single';
                    modeIndicator.textContent = '1 subreddit';
                } else {
                    singleInput.style.display = 'none';
                    multiInput.style.display = 'block';
                    modeIndicator.className = 'mode-badge multi';
                    modeIndicator.textContent = 'up to 100';
                }
                updateMultiSubredditCount();
            }

            // Update count when typing in multi-subreddit textarea
            function updateMultiSubredditCount() {
                const textarea = document.getElementById('subreddits');
                const modeIndicator = document.getElementById('mode-indicator');
                const mode = document.getElementById('scraper_mode').value;

                if (mode === 'multi' && textarea.value.trim()) {
                    const count = textarea.value.split(',').filter(s => s.trim()).length;
                    modeIndicator.textContent = count + ' subreddit' + (count !== 1 ? 's' : '');
                }
            }

            // Add event listener to textarea
            document.addEventListener('DOMContentLoaded', function() {
                const textarea = document.getElementById('subreddits');
                if (textarea) {
                    textarea.addEventListener('input', updateMultiSubredditCount);
                }
                // Fetch cost data on page load
                fetchCostData();
            });

            // Cost Tracker functions
            let costPanelCollapsed = false;

            function toggleCostPanel() {
                const content = document.getElementById('cost-content');
                const toggle = document.getElementById('cost-toggle');
                costPanelCollapsed = !costPanelCollapsed;

                if (costPanelCollapsed) {
                    content.style.display = 'none';
                    toggle.textContent = '▶';
                } else {
                    content.style.display = 'block';
                    toggle.textContent = '▼';
                }
            }

            async function fetchCostData() {
                try {
                    const response = await fetch('/api/usage/cost');
                    const data = await response.json();

                    if (data.status !== 'ok') {
                        console.error('Cost API error:', data);
                        return;
                    }

                    // Today
                    document.getElementById('costToday').textContent =
                        '$' + data.today.cost_usd.toFixed(2);
                    document.getElementById('reqsToday').textContent =
                        formatNumber(data.today.actual_http_requests) + ' HTTP / ' +
                        formatNumber(data.today.tracked_calls) + ' PRAW';

                    // Last Hour
                    document.getElementById('costHour').textContent =
                        '$' + data.last_hour.cost_usd.toFixed(4);
                    document.getElementById('reqsHour').textContent =
                        formatNumber(data.last_hour.actual_http_requests) + ' HTTP / ' +
                        formatNumber(data.last_hour.tracked_calls) + ' PRAW';

                    // Avg/Hour
                    document.getElementById('costAvgHour').textContent =
                        '$' + data.averages.hourly_cost_usd.toFixed(4);
                    document.getElementById('reqsAvgHour').textContent =
                        formatNumber(data.averages.hourly_requests) + ' reqs';

                    // Avg/Day
                    document.getElementById('costAvgDay').textContent =
                        '$' + data.averages.daily_cost_usd.toFixed(2);
                    document.getElementById('reqsAvgDay').textContent =
                        formatNumber(data.averages.daily_requests) + ' reqs';

                    // Monthly Projection
                    document.getElementById('costMonthly').textContent =
                        '$' + data.projections.monthly_cost_usd.toFixed(2);
                    document.getElementById('reqsMonthly').textContent =
                        formatNumber(data.projections.monthly_requests) + ' reqs';

                    // Update timestamp
                    document.getElementById('cost-updated').textContent =
                        'Updated: ' + new Date().toLocaleTimeString();

                } catch (error) {
                    console.error('Failed to fetch cost data:', error);
                }
            }

            function formatNumber(num) {
                if (num >= 1000000) {
                    return (num / 1000000).toFixed(1) + 'M';
                } else if (num >= 1000) {
                    return (num / 1000).toFixed(1) + 'K';
                }
                return num.toLocaleString();
            }

            // Auto-refresh cost data every 60 seconds
            setInterval(fetchCostData, 60000);

            // Account management functions
            function toggleAccountType() {
                const accountType = document.getElementById('account_type').value;
                const savedSection = document.getElementById('saved_account_section');
                const manualSection = document.getElementById('manual_credentials_section');

                if (accountType === 'saved') {
                    savedSection.style.display = 'block';
                    manualSection.style.display = 'none';
                } else {
                    savedSection.style.display = 'none';
                    manualSection.style.display = 'block';
                }
            }
            
            async function loadSavedAccounts() {
                try {
                    const response = await fetch('/accounts');
                    const accounts = await response.json();
                    const select = document.getElementById('saved_account_name');
                    
                    // Clear existing options
                    select.innerHTML = '<option value="">Select an account...</option>';
                    
                    // Add accounts
                    Object.keys(accounts).forEach(accountName => {
                        const option = document.createElement('option');
                        option.value = accountName;
                        option.textContent = `${accountName} (${accounts[accountName].username})`;
                        select.appendChild(option);
                    });
                } catch (error) {
                    console.error('Error loading saved accounts:', error);
                }
            }
            
            function showAccountManager() {
                document.getElementById('accountManagerModal').style.display = 'block';
                loadAccountsInManager();
            }
            
            function hideAccountManager() {
                document.getElementById('accountManagerModal').style.display = 'none';
                // Clear form
                ['new_account_name', 'new_client_id', 'new_client_secret', 'new_username', 'new_password', 'new_user_agent'].forEach(id => {
                    document.getElementById(id).value = '';
                });
            }
            
            async function saveNewAccount() {
                const accountName = document.getElementById('new_account_name').value;
                const credentials = {
                    client_id: document.getElementById('new_client_id').value,
                    client_secret: document.getElementById('new_client_secret').value,
                    username: document.getElementById('new_username').value,
                    password: document.getElementById('new_password').value,
                    user_agent: document.getElementById('new_user_agent').value
                };

                // Validate
                if (!accountName) {
                    alert('Please enter an account name');
                    return;
                }

                if (!Object.values(credentials).every(v => v)) {
                    alert('Please fill in all credential fields');
                    return;
                }

                // Get the save button and show loading state
                const saveBtn = event.target;
                const originalText = saveBtn.textContent;
                saveBtn.disabled = true;
                saveBtn.textContent = 'Validating...';

                try {
                    const response = await fetch(`/accounts?account_name=${encodeURIComponent(accountName)}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(credentials)
                    });

                    if (response.ok) {
                        alert('Account saved successfully! Credentials validated.');
                        loadAccountsInManager();
                        loadSavedAccounts();
                        // Clear form
                        ['new_account_name', 'new_client_id', 'new_client_secret', 'new_username', 'new_password', 'new_user_agent'].forEach(id => {
                            document.getElementById(id).value = '';
                        });
                    } else {
                        const error = await response.json();
                        alert('Validation failed: ' + error.detail);
                    }
                } catch (error) {
                    alert('Error: ' + error.message);
                } finally {
                    // Restore button state
                    saveBtn.disabled = false;
                    saveBtn.textContent = originalText;
                }
            }
            
            async function loadAccountsInManager() {
                try {
                    const response = await fetch('/accounts');
                    const accounts = await response.json();
                    const container = document.getElementById('savedAccountsList');

                    if (Object.keys(accounts).length === 0) {
                        container.innerHTML = '<p style="color: var(--text-muted);">No saved accounts yet.</p>';
                        return;
                    }

                    container.innerHTML = '';
                    Object.entries(accounts).forEach(([accountName, account]) => {
                        const div = document.createElement('div');
                        div.className = 'subreddit-chip';
                        div.style.cssText = 'display: flex; justify-content: space-between; align-items: center; padding: 14px 16px;';
                        div.innerHTML = `
                            <div>
                                <span class="name" style="color: var(--accent-cyan); font-weight: 600;">${accountName}</span><br>
                                <small style="color: var(--text-muted);">User: ${account.username} | Created: ${new Date(account.created_at).toLocaleDateString()}</small>
                            </div>
                            <button onclick="deleteAccount('${accountName}')" class="delete" style="padding: 6px 12px;">Delete</button>
                        `;
                        container.appendChild(div);
                    });
                } catch (error) {
                    console.error('Error loading accounts in manager:', error);
                }
            }
            
            async function deleteAccount(accountName) {
                if (confirm(`Are you sure you want to delete account "${accountName}"?`)) {
                    try {
                        const response = await fetch(`/accounts/${encodeURIComponent(accountName)}`, {
                            method: 'DELETE'
                        });
                        
                        if (response.ok) {
                            alert('Account deleted successfully!');
                            loadAccountsInManager();
                            loadSavedAccounts();
                        } else {
                            alert('Error deleting account');
                        }
                    } catch (error) {
                        alert('Error deleting account: ' + error.message);
                    }
                }
            }
            
            async function startScraper() {
                const button = document.getElementById('startScraperBtn');
                const accountType = document.getElementById('account_type').value;
                const scraperMode = document.getElementById('scraper_mode').value;
                const scraperType = document.getElementById('scraper_type').value;

                setButtonLoading(button, true, 'Starting...');

                try {
                    // Collect sorting methods from checkboxes
                    const sortingMethods = Array.from(document.querySelectorAll('input[name="sorting"]:checked'))
                                                .map(cb => cb.value);

                    if (sortingMethods.length === 0) {
                        alert('Please select at least one sorting method');
                        setButtonLoading(button, false);
                        return;
                    }

                    let requestData = {
                        scraper_type: scraperType,
                        posts_limit: parseInt(document.getElementById('posts_limit').value),
                        interval: parseInt(document.getElementById('interval').value),
                        comment_batch: parseInt(document.getElementById('comment_batch').value),
                        sorting_methods: sortingMethods,
                        auto_restart: document.getElementById('auto_restart').checked
                    };

                    // Add custom scraper name if provided
                    const scraperName = document.getElementById('scraper_name').value.trim();
                    if (scraperName) {
                        requestData.name = scraperName;
                    }

                    // Handle single vs multi-subreddit mode
                    if (scraperMode === 'single') {
                        const subreddit = document.getElementById('subreddit').value.trim();
                        if (!subreddit) {
                            alert('Please enter a subreddit name');
                            setButtonLoading(button, false);
                            return;
                        }
                        requestData.subreddit = subreddit;
                    } else {
                        // Multi-subreddit mode
                        const subredditsText = document.getElementById('subreddits').value;
                        const subreddits = subredditsText.split(',').map(s => s.trim()).filter(s => s);
                        if (subreddits.length === 0) {
                            alert('Please enter at least one subreddit');
                            setButtonLoading(button, false);
                            return;
                        }
                        if (subreddits.length > 100) {
                            alert('Maximum 100 subreddits per container');
                            setButtonLoading(button, false);
                            return;
                        }
                        requestData.subreddits = subreddits;
                    }
                    
                    if (accountType === 'saved') {
                        const savedAccountName = document.getElementById('saved_account_name').value;
                        if (!savedAccountName) {
                            alert('Please select a saved account');
                            return;
                        }
                        requestData.saved_account_name = savedAccountName;
                    } else {
                        // Manual credentials
                        const credentials = {
                            client_id: document.getElementById('client_id').value,
                            client_secret: document.getElementById('client_secret').value,
                            username: document.getElementById('username').value,
                            password: document.getElementById('password').value,
                            user_agent: document.getElementById('user_agent').value
                        };
                        
                        if (!Object.values(credentials).every(v => v)) {
                            alert('Please fill in all credential fields');
                            return;
                        }
                        
                        requestData.credentials = credentials;
                        
                        // Optionally save account
                        const saveAccountAs = document.getElementById('save_account_as').value;
                        if (saveAccountAs) {
                            requestData.save_account_as = saveAccountAs;
                        }
                    }
                    
                    const response = await fetch('/scrapers/start-flexible', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(requestData)
                    });
                    
                    if (response.ok) {
                        const result = await response.json();
                        let message = 'Scraper started successfully!';
                        if (result.saved_new_account) {
                            message += ` Account saved as "${requestData.save_account_as}".`;
                        }
                        alert(message);
                        
                        // Clear sensitive fields
                        if (accountType === 'manual') {
                            ['client_id', 'client_secret', 'password', 'save_account_as'].forEach(id => {
                                document.getElementById(id).value = '';
                            });
                        }
                        
                        loadScrapers();
                        loadHealthStatus();
                        loadSavedAccounts(); // Refresh in case account was saved
                    } else {
                        const error = await response.json();
                        alert('Error: ' + error.detail);
                    }
                } catch (error) {
                    alert('Error starting scraper: ' + error.message);
                } finally {
                    setButtonLoading(button, false);
                }
            }
            
            async function stopScraper(button, subreddit) {
                setButtonLoading(button, true, 'Stopping...');
                
                try {
                    const response = await fetch(`/scrapers/${subreddit}/stop`, { method: 'POST' });
                    if (response.ok) {
                        alert('Scraper stopped!');
                        loadScrapers();
                        loadHealthStatus();
                    } else {
                        alert('Error stopping scraper');
                    }
                } catch (error) {
                    alert('Error stopping scraper: ' + error.message);
                } finally {
                    setButtonLoading(button, false);
                }
            }
            
            async function restartScraper(button, subreddit) {
                setButtonLoading(button, true, 'Restarting...');
                
                try {
                    const response = await fetch(`/scrapers/${subreddit}/restart`, { method: 'POST' });
                    if (response.ok) {
                        alert('Scraper restarting!');
                        loadScrapers();
                        loadHealthStatus();
                    } else {
                        alert('Error restarting scraper');
                    }
                } catch (error) {
                    alert('Error restarting scraper: ' + error.message);
                } finally {
                    setButtonLoading(button, false);
                }
            }
            
            async function deleteScraper(button, subreddit) {
                if (confirm(`Are you sure you want to permanently delete the scraper for r/${subreddit}?`)) {
                    setButtonLoading(button, true, 'Deleting...');
                    
                    try {
                        const response = await fetch(`/scrapers/${subreddit}`, { method: 'DELETE' });
                        if (response.ok) {
                            alert('Scraper deleted!');
                            loadScrapers();
                            loadHealthStatus();
                        } else {
                            alert('Error deleting scraper');
                        }
                    } catch (error) {
                        alert('Error deleting scraper: ' + error.message);
                    } finally {
                        setButtonLoading(button, false);
                    }
                }
            }
            
            async function toggleAutoRestart(subreddit, enabled) {
                showGlobalLoading(`${enabled ? 'Enabling' : 'Disabling'} auto-restart...`);
                
                try {
                    const response = await fetch(`/scrapers/${subreddit}/auto-restart?auto_restart=${enabled}`, { method: 'PUT' });
                    if (!response.ok) {
                        alert('Error updating auto-restart setting');
                        loadScrapers(); // Reload to reset toggle
                    }
                } catch (error) {
                    alert('Error updating auto-restart: ' + error.message);
                    loadScrapers(); // Reload to reset toggle
                } finally {
                    hideGlobalLoading();
                }
            }
            
            async function getStats(button, subreddit) {
                setButtonLoading(button, true, 'Loading...');
                
                try {
                    const response = await fetch(`/scrapers/${subreddit}/stats`);
                    const stats = await response.json();
                    const statsText = `
r/${subreddit} Statistics:
━━━━━━━━━━━━━━━━━━━━━━
Posts: ${stats.total_posts.toLocaleString()}
Comments: ${stats.total_comments.toLocaleString()}
Initial Completion: ${stats.initial_completion_rate.toFixed(1)}%
Metadata: ${stats.subreddit_metadata_exists ? 'Yes' : 'No'}
Last Updated: ${stats.subreddit_last_updated ? new Date(stats.subreddit_last_updated).toLocaleString() : 'Never'}
                    `;
                    alert(statsText);
                } catch (error) {
                    alert('Error loading stats: ' + error.message);
                } finally {
                    setButtonLoading(button, false);
                }
            }
            
            async function getLogs(button, subreddit) {
                setButtonLoading(button, true, 'Loading...');
                
                try {
                    const response = await fetch(`/scrapers/${subreddit}/logs`);
                    const logs = await response.json();
                    const logsText = `
r/${subreddit} Logs:
━━━━━━━━━━━━━━━━━━━━━━
${logs.logs}
                    `;
                    alert(logsText);
                } catch (error) {
                    alert('Error loading logs: ' + error.message);
                } finally {
                    setButtonLoading(button, false);
                }
            }
            

            
            // ===== Subreddit Management Modal Functions =====
            let currentEditingScraper = null;
            let currentEditingSubreddits = [];

            // Track original subreddits and current state
            let originalSubreddits = [];
            let editedSubreddits = new Set();
            let removedSubreddits = new Set();

            function openSubredditModal(subreddit, subreddits) {
                currentEditingScraper = subreddit;
                currentEditingSubreddits = subreddits || [subreddit];

                // Initialize state
                originalSubreddits = [...currentEditingSubreddits];
                editedSubreddits = new Set(currentEditingSubreddits);
                removedSubreddits = new Set();

                document.getElementById('modalScraperName').textContent = `r/${subreddit}`;

                // Clear input
                document.getElementById('addSubredditInput').value = '';

                // Render chips and update stats
                renderSubredditChips();
                updateSubredditEditStats();
                document.getElementById('subredditModal').style.display = 'flex';

                // Focus input
                setTimeout(() => document.getElementById('addSubredditInput').focus(), 100);
            }

            function closeSubredditModal() {
                document.getElementById('subredditModal').style.display = 'none';
                currentEditingScraper = null;
                currentEditingSubreddits = [];
                originalSubreddits = [];
                editedSubreddits = new Set();
                removedSubreddits = new Set();
            }

            function renderSubredditChips() {
                const container = document.getElementById('subredditChipsContainer');

                // Combine all subreddits: existing (possibly removed) + newly added
                const allSubs = new Set([...originalSubreddits, ...editedSubreddits]);

                // Sort: active first (sorted), then removed (sorted)
                const activeSubs = [...allSubs].filter(s => editedSubreddits.has(s) && !removedSubreddits.has(s));
                const removedSubs = [...removedSubreddits];

                const sortedSubs = [...activeSubs.sort(), ...removedSubs.sort()];

                container.innerHTML = sortedSubs.map(sub => {
                    const isOriginal = originalSubreddits.includes(sub);
                    const isRemoved = removedSubreddits.has(sub);
                    const isAdded = !isOriginal && editedSubreddits.has(sub);

                    let chipClass = 'subreddit-chip';
                    if (isRemoved) chipClass += ' removed';
                    else if (isAdded) chipClass += ' added';
                    else chipClass += ' existing';

                    return `
                        <span class="${chipClass}" data-sub="${sub}">
                            r/${sub}
                            <button class="chip-remove" onclick="removeSubreddit('${sub}')" title="Remove">&times;</button>
                            <button class="chip-restore" onclick="restoreSubreddit('${sub}')" title="Restore">undo</button>
                        </span>
                    `;
                }).join('');

                updateChangeSummary();
            }

            function updateChangeSummary() {
                const added = [...editedSubreddits].filter(s => !originalSubreddits.includes(s));
                const removed = [...removedSubreddits];

                const summaryEl = document.getElementById('changeSummary');
                const addedEl = document.getElementById('addedSummary');
                const removedEl = document.getElementById('removedSummary');

                if (added.length > 0 || removed.length > 0) {
                    summaryEl.style.display = 'flex';

                    if (added.length > 0) {
                        addedEl.style.display = 'flex';
                        document.getElementById('addedCount').textContent = added.length;
                    } else {
                        addedEl.style.display = 'none';
                    }

                    if (removed.length > 0) {
                        removedEl.style.display = 'flex';
                        document.getElementById('removedCount').textContent = removed.length;
                    } else {
                        removedEl.style.display = 'none';
                    }
                } else {
                    summaryEl.style.display = 'none';
                }
            }

            function addSubredditFromInput() {
                const input = document.getElementById('addSubredditInput');
                const text = input.value.trim();

                if (!text) return;

                // Support comma-separated or single entry
                const newSubs = text.split(/[,\\s]+/).map(s => s.trim().toLowerCase().replace(/^r\\//, '')).filter(s => s);

                // Calculate effective count (excluding removed subs)
                const getEffectiveCount = () => [...editedSubreddits].filter(s => !removedSubreddits.has(s)).length;

                let addedCount = 0;
                let duplicateCount = 0;
                newSubs.forEach(sub => {
                    // Check if already in active list
                    if (editedSubreddits.has(sub) && !removedSubreddits.has(sub)) {
                        duplicateCount++;
                        return;
                    }

                    // Restoring a removed original sub
                    if (removedSubreddits.has(sub)) {
                        removedSubreddits.delete(sub);
                        addedCount++;
                        return;
                    }

                    // Adding new sub - check effective limit
                    if (getEffectiveCount() < 100) {
                        editedSubreddits.add(sub);
                        addedCount++;
                    }
                });

                if (addedCount > 0) {
                    input.value = '';
                    renderSubredditChips();
                    updateSubredditEditStats();
                } else if (duplicateCount > 0 && newSubs.length === duplicateCount) {
                    // All were duplicates - clear input silently
                    input.value = '';
                } else if (newSubs.length > 0 && getEffectiveCount() >= 100) {
                    alert('Maximum 100 subreddits per container');
                }
            }

            function removeSubreddit(sub) {
                if (originalSubreddits.includes(sub)) {
                    // Mark as removed (will show with strikethrough)
                    removedSubreddits.add(sub);
                } else {
                    // Newly added - just remove entirely
                    editedSubreddits.delete(sub);
                }
                renderSubredditChips();
                updateSubredditEditStats();
            }

            function restoreSubreddit(sub) {
                removedSubreddits.delete(sub);
                editedSubreddits.add(sub);
                renderSubredditChips();
                updateSubredditEditStats();
            }

            async function updateSubredditEditStats() {
                // Get final list (edited minus removed)
                const finalSubs = [...editedSubreddits].filter(s => !removedSubreddits.has(s));
                const count = finalSubs.length;

                document.getElementById('editSubCount').textContent = count;

                // Fetch rate limit preview
                if (count > 0) {
                    try {
                        const response = await fetch(`/scrapers/rate-limit-preview?subreddit_count=${count}`);
                        const data = await response.json();

                        const previewEl = document.getElementById('editRatePreview');
                        previewEl.textContent = `~${data.estimated_calls_per_minute} API calls/min (${data.usage_percent}%)`;
                        previewEl.className = `rate-preview ${data.warning_level}`;

                        // Show/hide warning banner
                        const warningBanner = document.getElementById('rateWarningBanner');
                        if (data.warning_level === 'warning' || data.warning_level === 'critical') {
                            warningBanner.style.display = 'flex';
                            warningBanner.className = `rate-warning-banner ${data.warning_level}`;
                            document.getElementById('rateWarningText').textContent =
                                data.recommendation || 'Approaching Reddit API rate limits';
                        } else {
                            warningBanner.style.display = 'none';
                        }
                    } catch (e) {
                        console.error('Failed to fetch rate preview:', e);
                    }
                } else {
                    document.getElementById('editRatePreview').textContent = '';
                    document.getElementById('rateWarningBanner').style.display = 'none';
                }
            }

            async function saveSubreddits() {
                if (!currentEditingScraper) return;

                const button = document.getElementById('saveSubredditsBtn');
                const finalSubs = [...editedSubreddits].filter(s => !removedSubreddits.has(s));

                if (finalSubs.length === 0) {
                    alert('Please keep at least one subreddit');
                    return;
                }

                if (finalSubs.length > 100) {
                    alert('Maximum 100 subreddits per container');
                    return;
                }

                setButtonLoading(button, true, 'Saving...');

                try {
                    const response = await fetch(`/scrapers/${currentEditingScraper}/subreddits`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ subreddits: finalSubs })
                    });

                    if (response.ok) {
                        const result = await response.json();
                        closeSubredditModal();
                        loadScrapers();
                    } else {
                        const error = await response.json();
                        alert('Error: ' + (error.detail || 'Failed to update subreddits'));
                    }
                } catch (error) {
                    alert('Error updating subreddits: ' + error.message);
                } finally {
                    setButtonLoading(button, false, 'Save & Restart');
                }
            }

            // Event listeners that need DOM to be ready
            document.addEventListener('DOMContentLoaded', function() {
                // Enter key handler for add subreddit input
                const addInput = document.getElementById('addSubredditInput');
                if (addInput) {
                    addInput.addEventListener('keydown', function(e) {
                        if (e.key === 'Enter') {
                            e.preventDefault();
                            addSubredditFromInput();
                        }
                    });
                    // Handle paste event for comma-separated values
                    addInput.addEventListener('paste', function(e) {
                        setTimeout(() => {
                            addSubredditFromInput();
                        }, 10);
                    });
                }

                // Close modal on backdrop click
                const modal = document.getElementById('subredditModal');
                if (modal) {
                    modal.addEventListener('click', function(e) {
                        if (e.target === this) {
                            closeSubredditModal();
                        }
                    });
                }
            });

            // Close modal on escape key (can attach to document immediately)
            document.addEventListener('keydown', function(e) {
                if (e.key === 'Escape') {
                    const modal = document.getElementById('subredditModal');
                    if (modal && modal.style.display === 'flex') {
                        closeSubredditModal();
                    }
                }
            });

            // Load scrapers, health, and accounts on page load and refresh every 15 seconds
            loadScrapers();
            loadHealthStatus();
            loadSavedAccounts();
            loadAccountStats();
            setInterval(() => {
                loadScrapers();
                loadHealthStatus();
                loadAccountStats();
            }, 15000);
        </script>

        <!-- Subreddit Management Modal -->
        <div id="subredditModal" class="modal-overlay subreddit-modal" style="display: none;">
            <div class="modal-container">
                <div class="modal-header">
                    <h3>Edit Subreddits</h3>
                    <button onclick="closeSubredditModal()" class="modal-close">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="modal-scraper-info">
                        <span class="meta-label">Scraper:</span>
                        <span id="modalScraperName" class="meta-value"></span>
                    </div>

                    <div id="rateWarningBanner" class="rate-warning-banner" style="display: none;">
                        <span class="warning-icon">⚠️</span>
                        <span id="rateWarningText"></span>
                    </div>

                    <!-- Add new subreddits input -->
                    <div class="form-group">
                        <label>Add Subreddits:</label>
                        <div class="add-subreddit-input-wrapper">
                            <span class="input-prefix">r/</span>
                            <input type="text" id="addSubredditInput"
                                placeholder="Type subreddit name and press Enter"
                                autocomplete="off" spellcheck="false">
                            <button onclick="addSubredditFromInput()" class="add-btn" title="Add subreddit">+</button>
                        </div>
                        <div class="input-hint">Press Enter to add, or paste comma-separated list</div>
                    </div>

                    <!-- Change summary -->
                    <div id="changeSummary" class="change-summary" style="display: none;">
                        <div id="addedSummary" class="change-item added" style="display: none;">
                            <span class="change-icon">+</span>
                            <span id="addedCount">0</span> to add
                        </div>
                        <div id="removedSummary" class="change-item removed" style="display: none;">
                            <span class="change-icon">−</span>
                            <span id="removedCount">0</span> to remove
                        </div>
                    </div>

                    <!-- Subreddit chips -->
                    <div class="form-group">
                        <label>Subreddits (<span id="editSubCount">0</span>/100):</label>
                        <div id="subredditChipsContainer" class="subreddit-chips-interactive"></div>
                    </div>

                    <div class="edit-stats">
                        <span id="editRatePreview" class="rate-preview"></span>
                    </div>

                    <div class="modal-actions">
                        <button onclick="closeSubredditModal()" class="btn-secondary">Cancel</button>
                        <button onclick="saveSubreddits()" id="saveSubredditsBtn" class="btn-primary">Save & Restart</button>
                    </div>
                </div>
            </div>
        </div>

        <style>
            /* Modal styles */
            .modal-overlay.subreddit-modal {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.7);
                justify-content: center;
                align-items: center;
                z-index: 1000;
            }
            .modal-container {
                background: var(--bg-card);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-lg);
                width: 90%;
                max-width: 550px;
                max-height: 90vh;
                overflow-y: auto;
            }
            .modal-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 16px 20px;
                border-bottom: 1px solid var(--border-default);
            }
            .modal-header h3 {
                margin: 0;
                font-size: 1.1rem;
            }
            .modal-close {
                background: none;
                border: none;
                font-size: 1.5rem;
                cursor: pointer;
                color: var(--text-muted);
                padding: 0;
                line-height: 1;
            }
            .modal-close:hover {
                color: var(--text-primary);
            }
            .modal-body {
                padding: 20px;
            }
            .modal-scraper-info {
                margin-bottom: 16px;
                padding: 12px;
                background: var(--bg-elevated);
                border-radius: var(--radius-sm);
            }
            .form-group {
                margin-bottom: 16px;
            }
            .form-group label {
                display: block;
                margin-bottom: 8px;
                font-weight: 500;
                color: var(--text-secondary);
            }
            .modal-textarea {
                width: 100%;
                padding: 12px;
                font-family: var(--font-mono);
                font-size: 0.9rem;
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-sm);
                color: var(--text-primary);
                resize: vertical;
            }
            .modal-textarea:focus {
                outline: none;
                border-color: var(--accent-cyan);
            }
            /* Add subreddit input */
            .add-subreddit-input-wrapper {
                display: flex;
                align-items: center;
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-sm);
                overflow: hidden;
            }
            .add-subreddit-input-wrapper:focus-within {
                border-color: var(--accent-cyan);
            }
            .input-prefix {
                padding: 10px 0 10px 12px;
                color: var(--text-muted);
                font-family: var(--font-mono);
                font-size: 0.9rem;
            }
            .add-subreddit-input-wrapper input {
                flex: 1;
                padding: 10px 8px;
                background: transparent;
                border: none;
                color: var(--text-primary);
                font-family: var(--font-mono);
                font-size: 0.9rem;
            }
            .add-subreddit-input-wrapper input:focus {
                outline: none;
            }
            .add-subreddit-input-wrapper input::placeholder {
                color: var(--text-muted);
            }
            .add-btn {
                padding: 10px 14px;
                background: var(--accent-cyan);
                border: none;
                color: var(--bg-primary);
                font-size: 1.1rem;
                font-weight: 600;
                cursor: pointer;
                transition: background 0.15s;
            }
            .add-btn:hover {
                background: var(--accent-green);
            }
            .input-hint {
                font-size: 0.75rem;
                color: var(--text-muted);
                margin-top: 6px;
            }

            /* Change summary */
            .change-summary {
                display: flex;
                gap: 16px;
                padding: 10px 14px;
                background: var(--bg-elevated);
                border-radius: var(--radius-sm);
                margin-bottom: 16px;
                font-size: 0.85rem;
            }
            .change-item {
                display: flex;
                align-items: center;
                gap: 6px;
            }
            .change-item.added { color: var(--accent-green); }
            .change-item.removed { color: var(--accent-red); }
            .change-icon {
                font-weight: 600;
                font-size: 1rem;
            }

            /* Interactive subreddit chips */
            .subreddit-chips-interactive {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                padding: 12px;
                background: var(--bg-elevated);
                border-radius: var(--radius-sm);
                min-height: 60px;
                max-height: 200px;
                overflow-y: auto;
            }
            .subreddit-chip {
                display: flex;
                align-items: center;
                gap: 6px;
                padding: 5px 8px 5px 12px;
                background: var(--bg-card);
                border: 1px solid var(--border-default);
                border-radius: 100px;
                font-size: 0.85rem;
                color: var(--text-secondary);
                transition: all 0.15s;
            }
            .subreddit-chip.existing {
                background: var(--bg-card);
            }
            .subreddit-chip.added {
                background: rgba(34, 197, 94, 0.15);
                border-color: var(--accent-green);
                color: var(--accent-green);
            }
            .subreddit-chip.removed {
                background: rgba(239, 68, 68, 0.15);
                border-color: var(--accent-red);
                color: var(--accent-red);
                text-decoration: line-through;
                opacity: 0.7;
            }
            .chip-remove {
                display: flex;
                align-items: center;
                justify-content: center;
                width: 18px;
                height: 18px;
                background: transparent;
                border: none;
                border-radius: 50%;
                color: var(--text-muted);
                font-size: 1rem;
                cursor: pointer;
                transition: all 0.15s;
                padding: 0;
                line-height: 1;
            }
            .chip-remove:hover {
                background: var(--accent-red);
                color: white;
            }
            .subreddit-chip.removed .chip-remove {
                display: none;
            }
            .chip-restore {
                display: none;
                padding: 2px 8px;
                background: transparent;
                border: 1px solid var(--accent-cyan);
                border-radius: 100px;
                color: var(--accent-cyan);
                font-size: 0.75rem;
                cursor: pointer;
                transition: all 0.15s;
            }
            .chip-restore:hover {
                background: var(--accent-cyan);
                color: var(--bg-primary);
            }
            .subreddit-chip.removed .chip-restore {
                display: inline-block;
            }

            .edit-stats {
                display: flex;
                justify-content: flex-end;
                align-items: center;
                margin-bottom: 20px;
                font-size: 0.85rem;
                color: var(--text-secondary);
            }
            .rate-preview {
                font-weight: 500;
            }
            .rate-preview.safe { color: var(--accent-green); }
            .rate-preview.caution { color: var(--accent-cyan); }
            .rate-preview.warning { color: var(--accent-amber); }
            .rate-preview.critical { color: var(--accent-red); }
            .rate-warning-banner {
                background: rgba(245, 158, 11, 0.1);
                border: 1px solid var(--accent-amber);
                border-radius: var(--radius-sm);
                padding: 12px 16px;
                margin-bottom: 16px;
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 0.9rem;
            }
            .rate-warning-banner.critical {
                background: rgba(239, 68, 68, 0.1);
                border-color: var(--accent-red);
            }
            .modal-actions {
                display: flex;
                justify-content: flex-end;
                gap: 12px;
            }
            .btn-secondary {
                padding: 10px 20px;
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-sm);
                color: var(--text-secondary);
                cursor: pointer;
                font-size: 0.9rem;
            }
            .btn-secondary:hover {
                background: var(--bg-card);
                color: var(--text-primary);
            }
            .btn-primary {
                padding: 10px 20px;
                background: var(--accent-cyan);
                border: none;
                border-radius: var(--radius-sm);
                color: #000;
                cursor: pointer;
                font-size: 0.9rem;
                font-weight: 500;
            }
            .btn-primary:hover {
                background: var(--accent-cyan-hover);
            }
        </style>
    </body>
    </html>
    """
    return html

@app.get("/scrapers")
async def list_scrapers():
    """List all active scrapers and their status"""
    result = {}
    
    # Get all scrapers from database
    try:
        scrapers = scrapers_collection.find({})
        for scraper_doc in scrapers:
            subreddit = scraper_doc["subreddit"]
            
            # Check current container status if running
            container_status = scraper_doc["status"]
            if scraper_doc.get("container_name") and scraper_doc["status"] == "running":
                actual_status = check_container_status(scraper_doc["container_name"])
                if actual_status != "running":
                    # Update database if container is not actually running
                    update_scraper_status(subreddit, "stopped", last_error="Container not running")
                    container_status = "stopped"
            
            # Create safe credentials for display using centralized config
            safe_credentials = {
                "client_id": SECURITY_CONFIG["masked_credential_value"],
                "client_secret": SECURITY_CONFIG["masked_credential_value"],
                "username": scraper_doc["credentials"]["username"],
                "password": SECURITY_CONFIG["masked_credential_value"],
                "user_agent": scraper_doc["credentials"]["user_agent"]
            }

            # Get all subreddits (for multi-subreddit scrapers)
            all_subreddits = scraper_doc.get("subreddits", [subreddit])
            if not all_subreddits:
                all_subreddits = [subreddit]

            # Query actual database totals (persist across scraper recreations)
            # For multi-subreddit scrapers, count totals across all subreddits
            if len(all_subreddits) > 1:
                db_total_posts = posts_collection.count_documents({"subreddit": {"$in": all_subreddits}})
                db_total_comments = comments_collection.count_documents({"subreddit": {"$in": all_subreddits}})
                # Get per-subreddit breakdown
                subreddit_stats = {}
                for sub in all_subreddits:
                    subreddit_stats[sub] = {
                        "posts": posts_collection.count_documents({"subreddit": sub}),
                        "comments": comments_collection.count_documents({"subreddit": sub})
                    }
            else:
                db_total_posts = posts_collection.count_documents({"subreddit": subreddit})
                db_total_comments = comments_collection.count_documents({"subreddit": subreddit})
                subreddit_stats = {
                    subreddit: {"posts": db_total_posts, "comments": db_total_comments}
                }

            result[subreddit] = {
                "name": scraper_doc.get("name"),  # Custom scraper name
                "status": container_status,
                "started_at": scraper_doc.get("created_at"),
                "last_updated": scraper_doc.get("last_updated"),
                "config": {
                    "posts_limit": scraper_doc["config"]["posts_limit"],
                    "interval": scraper_doc["config"]["interval"],
                    "comment_batch": scraper_doc["config"]["comment_batch"],
                    "sorting_methods": scraper_doc["config"].get("sorting_methods", ["hot"]),
                    "credentials": safe_credentials,
                    "auto_restart": scraper_doc.get("auto_restart", True)
                },
                "metrics": scraper_doc.get("metrics", {
                    "total_posts_collected": 0,
                    "total_comments_collected": 0,
                    "total_cycles": 0,
                    "posts_per_hour": 0,
                    "comments_per_hour": 0,
                    "last_cycle_posts": 0,
                    "last_cycle_comments": 0,
                    "last_cycle_time": None
                }),
                "database_totals": {
                    "total_posts": db_total_posts,
                    "total_comments": db_total_comments
                },
                "subreddit_stats": subreddit_stats,
                "last_error": scraper_doc.get("last_error"),
                "container_id": scraper_doc.get("container_id"),
                "container_name": scraper_doc.get("container_name"),
                "restart_count": scraper_doc.get("restart_count", 0),
                "subreddits": all_subreddits  # All subreddits for multi-subreddit mode
            }
    
    except Exception as e:
        logger.error(f"Error listing scrapers from database: {e}")
        # Fallback to memory cache
        for subreddit, info in active_scrapers.items():
            # Check if container is still running
            if "container_name" in info:
                container_status = check_container_status(info["container_name"])
                if container_status == "running":
                    info["status"] = "running"
                elif container_status == "exited":
                    info["status"] = "stopped"
                elif container_status is None:
                    info["status"] = "stopped"  # Container doesn't exist
                else:
                    info["status"] = container_status
            
            result[subreddit] = {
                "status": info["status"],
                "started_at": info["started_at"],
                "config": info["config"].dict() if info["config"] else None,
                "last_error": info.get("last_error"),
                "container_id": info.get("container_id"),
                "container_name": info.get("container_name")
            }
    
    return result

@app.post("/scrapers/start-flexible")
async def start_scraper_flexible(request: ScraperStartRequest, background_tasks: BackgroundTasks):
    """Start a new scraper using either saved account or manual credentials

    Supports both single and multi-subreddit modes:
    - Single: {"subreddit": "stocks", ...}
    - Multi: {"subreddits": ["stocks", "investing", "wallstreetbets"], ...}
    """

    # Determine subreddits list (multi-subreddit or single)
    if request.subreddits:
        subreddits = request.subreddits
    elif request.subreddit:
        subreddits = [request.subreddit]
    else:
        raise HTTPException(status_code=400, detail="Must provide 'subreddit' or 'subreddits'")

    # Validate max subreddits (from config)
    from config import MULTI_SCRAPER_CONFIG
    max_subreddits = MULTI_SCRAPER_CONFIG["max_subreddits_per_container"]
    if len(subreddits) > max_subreddits:
        raise HTTPException(status_code=400, detail=f"Maximum {max_subreddits} subreddits per container")

    # Clean subreddit names (lowercase for consistency)
    subreddits = [s.strip().lower() for s in subreddits if s.strip()]
    if not subreddits:
        raise HTTPException(status_code=400, detail="No valid subreddit names provided")

    is_multi = len(subreddits) > 1
    primary_subreddit = subreddits[0]
    display_name = f"multi:{len(subreddits)}subs" if is_multi else primary_subreddit

    # Determine which credentials to use
    if request.saved_account_name:
        # Use saved account
        credentials = get_reddit_account(request.saved_account_name)
        if not credentials:
            raise HTTPException(status_code=404, detail=f"Saved account '{request.saved_account_name}' not found")
        logger.info(f"Using saved account '{request.saved_account_name}' for {display_name}")
    elif request.credentials:
        # Use manual credentials
        credentials = request.credentials

        # Optionally save the account
        if request.save_account_as:
            save_success = save_reddit_account(request.save_account_as, credentials)
            if save_success:
                logger.info(f"Saved new account '{request.save_account_as}'")
            else:
                logger.warning(f"Failed to save account '{request.save_account_as}'")
    else:
        raise HTTPException(
            status_code=400,
            detail="Either 'saved_account_name' or 'credentials' must be provided"
        )

    # Create scraper config with subreddits list
    config = ScraperConfig(
        name=request.name,             # Custom scraper name (optional)
        subreddit=primary_subreddit,  # Primary key for backwards compat
        subreddits=subreddits,         # Full list for multi-mode
        scraper_type=request.scraper_type,
        posts_limit=request.posts_limit,
        interval=request.interval,
        comment_batch=request.comment_batch,
        sorting_methods=request.sorting_methods,
        credentials=credentials,
        auto_restart=request.auto_restart
    )

    # Check if scraper already exists for primary subreddit
    existing_scraper = load_scraper_from_db(primary_subreddit, request.scraper_type)
    if existing_scraper:
        if existing_scraper["container_name"]:
            container_status = check_container_status(existing_scraper["container_name"])
            if container_status == "running":
                raise HTTPException(status_code=400, detail=f"Scraper already running for r/{primary_subreddit}")
        logger.info(f"Updating existing scraper configuration for r/{config.subreddit}")
    
    # Check MongoDB URI
    mongodb_uri = os.getenv("MONGODB_URI")
    if not mongodb_uri:
        raise HTTPException(
            status_code=400,
            detail="MongoDB URI is required in environment variables (MONGODB_URI)"
        )
    
    # Check Docker image exists
    try:
        result = subprocess.run([
            "docker", "images", DOCKER_CONFIG["image_name"], "--format", "{{.Repository}}"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0 or DOCKER_CONFIG["image_name"] not in result.stdout:
            raise HTTPException(
                status_code=500,
                detail=f"Docker image '{DOCKER_CONFIG['image_name']}' not found. Please run: docker build -f Dockerfile -t {DOCKER_CONFIG['image_name']} ."
            )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Docker command timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking Docker image: {str(e)}")
    
    # Start scraper in background
    background_tasks.add_task(run_scraper, config)

    # Generate container name for response
    container_prefix = DOCKER_CONFIG['container_prefix'].get(request.scraper_type, DOCKER_CONFIG['container_prefix']['posts'])
    if is_multi:
        container_name = f"{container_prefix}multi-{len(subreddits)}subs-{subreddits[0][:10]}"
        message = f"Multi-subreddit scraper started for {len(subreddits)} subreddits"
    else:
        container_name = f"{container_prefix}{primary_subreddit}"
        message = f"Scraper started for r/{primary_subreddit}"

    return {
        "message": message,
        "subreddits": subreddits,
        "subreddit_count": len(subreddits),
        "scraper_type": request.scraper_type,
        "reddit_user": credentials.username,
        "posts_limit": config.posts_limit,
        "interval": config.interval,
        "comment_batch": config.comment_batch,
        "container_name": container_name,
        "auto_restart": config.auto_restart,
        "used_saved_account": request.saved_account_name is not None,
        "saved_new_account": request.save_account_as is not None
    }

@app.post("/scrapers/start")
async def start_scraper_legacy(config: ScraperConfig, background_tasks: BackgroundTasks):
    """Legacy endpoint - redirects to flexible endpoint (MongoDB URI must be in environment)"""

    # Convert to new format
    request = ScraperStartRequest(
        subreddit=config.subreddit,
        posts_limit=config.posts_limit,
        interval=config.interval,
        comment_batch=config.comment_batch,
        sorting_methods=config.sorting_methods,
        auto_restart=config.auto_restart,
        credentials=config.credentials
    )

    return await start_scraper_flexible(request, background_tasks)

@app.post("/scrapers/{subreddit}/stop")
async def stop_scraper(subreddit: str, scraper_type: Optional[str] = None):
    """Stop a running scraper container

    Args:
        subreddit: Subreddit name
        scraper_type: Optional - "posts" or "comments". If not provided, stops first matching scraper.
    """
    # Load scraper from database (with backwards compatibility)
    scraper_data = load_scraper_from_db(subreddit, scraper_type or "posts")
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")

    # Get actual scraper_type from loaded data
    actual_scraper_type = scraper_data.get("scraper_type", "posts")
    cache_key = get_scraper_key(subreddit, actual_scraper_type)

    container_name = scraper_data.get("container_name")
    if container_name:
        try:
            # Stop the Docker container
            result = subprocess.run([
                "docker", "stop", container_name
            ], capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                update_scraper_status(subreddit, "stopped", scraper_type=actual_scraper_type)
                if cache_key in active_scrapers:
                    active_scrapers[cache_key]["status"] = "stopped"
                logger.info(f"Stopped container {container_name} for r/{subreddit} ({actual_scraper_type})")
            else:
                # Try force kill if stop didn't work
                subprocess.run([
                    "docker", "kill", container_name
                ], capture_output=True, text=True)
                update_scraper_status(subreddit, "stopped", scraper_type=actual_scraper_type)
                if cache_key in active_scrapers:
                    active_scrapers[cache_key]["status"] = "stopped"
                logger.info(f"Force killed container {container_name} for r/{subreddit} ({actual_scraper_type})")

        except subprocess.TimeoutExpired:
            # Force kill if timeout
            subprocess.run([
                "docker", "kill", container_name
            ], capture_output=True, text=True)
            update_scraper_status(subreddit, "stopped", scraper_type=actual_scraper_type)
            if cache_key in active_scrapers:
                active_scrapers[cache_key]["status"] = "stopped"
            logger.info(f"Timeout - force killed container {container_name} for r/{subreddit} ({actual_scraper_type})")
        except Exception as e:
            update_scraper_status(subreddit, "error", last_error=f"Error stopping container: {str(e)}", scraper_type=actual_scraper_type)
            raise HTTPException(status_code=500, detail=f"Error stopping container: {str(e)}")

    return {"message": f"Scraper stopped for r/{subreddit} ({actual_scraper_type})"}

@app.get("/scrapers/{subreddit}/stats")
async def get_scraper_stats(subreddit: str, detailed: bool = False):
    """Get comprehensive statistics for a specific subreddit

    Args:
        subreddit: Subreddit name
        detailed: If True, include expensive aggregations (top posts, distributions, etc.)
    """
    if not mongo_connected:
        raise HTTPException(status_code=500, detail="Database not connected")

    try:
        # === BASIC COUNTS ===
        total_posts = posts_collection.count_documents({"subreddit": subreddit})
        total_comments = comments_collection.count_documents({"subreddit": subreddit})

        # === DATA COVERAGE ===
        posts_with_initial_comments = posts_collection.count_documents({
            "subreddit": subreddit,
            "initial_comments_scraped": True
        })
        posts_without_initial_comments = posts_collection.count_documents({
            "subreddit": subreddit,
            "$or": [
                {"initial_comments_scraped": {"$exists": False}},
                {"initial_comments_scraped": False}
            ]
        })

        # Date range
        date_pipeline = [
            {"$match": {"subreddit": subreddit}},
            {"$group": {
                "_id": None,
                "oldest": {"$min": "$created_datetime"},
                "newest": {"$max": "$created_datetime"}
            }}
        ]
        date_result = list(posts_collection.aggregate(date_pipeline))
        date_range = None
        if date_result and date_result[0].get("oldest"):
            oldest = date_result[0]["oldest"]
            newest = date_result[0]["newest"]
            span_days = (newest - oldest).days if newest and oldest else 0
            date_range = {
                "oldest_post": oldest,
                "newest_post": newest,
                "span_days": span_days
            }

        # Recent activity (24h)
        from datetime import timedelta
        now = datetime.now(UTC)
        day_ago = now - timedelta(hours=24)
        posts_scraped_24h = posts_collection.count_documents({
            "subreddit": subreddit,
            "scraped_at": {"$gte": day_ago}
        })
        comments_scraped_24h = comments_collection.count_documents({
            "subreddit": subreddit,
            "scraped_at": {"$gte": day_ago}
        })
        posts_updated_24h = posts_collection.count_documents({
            "subreddit": subreddit,
            "last_comment_fetch_time": {"$gte": day_ago}
        })

        # === CONTENT STATISTICS ===
        content_pipeline = [
            {"$match": {"subreddit": subreddit}},
            {"$group": {
                "_id": None,
                "avg_comments": {"$avg": "$num_comments"},
                "avg_score": {"$avg": "$score"},
                "avg_upvote_ratio": {"$avg": "$upvote_ratio"},
                "self_posts": {"$sum": {"$cond": ["$is_self", 1, 0]}},
                "link_posts": {"$sum": {"$cond": ["$is_self", 0, 1]}},
                "nsfw_posts": {"$sum": {"$cond": ["$over_18", 1, 0]}},
                "locked_posts": {"$sum": {"$cond": ["$locked", 1, 0]}},
                "stickied_posts": {"$sum": {"$cond": ["$stickied", 1, 0]}}
            }}
        ]
        content_result = list(posts_collection.aggregate(content_pipeline))
        content_stats = content_result[0] if content_result else {}

        # Posts by sort method
        sort_method_pipeline = [
            {"$match": {"subreddit": subreddit}},
            {"$group": {"_id": "$sort_method", "count": {"$sum": 1}}}
        ]
        sort_method_result = list(posts_collection.aggregate(sort_method_pipeline))
        posts_by_sort_method = {item["_id"]: item["count"] for item in sort_method_result if item["_id"]}

        # === COMMENT STATISTICS ===
        comment_pipeline = [
            {"$match": {"subreddit": subreddit}},
            {"$group": {
                "_id": None,
                "avg_score": {"$avg": "$score"},
                "max_depth": {"$max": "$depth"},
                "gilded_count": {"$sum": {"$cond": [{"$gt": ["$gilded", 0]}, 1, 0]}},
                "awarded_count": {"$sum": {"$cond": [{"$gt": ["$total_awards_received", 0]}, 1, 0]}},
                "top_level": {"$sum": {"$cond": [{"$eq": ["$depth", 0]}, 1, 0]}},
                "replies": {"$sum": {"$cond": [{"$gt": ["$depth", 0]}, 1, 0]}}
            }}
        ]
        comment_result = list(comments_collection.aggregate(comment_pipeline))
        comment_stats = comment_result[0] if comment_result else {}

        # === SCRAPER PERFORMANCE ===
        scraper_doc = scrapers_collection.find_one({"subreddit": subreddit})
        scraper_metrics = {}
        if scraper_doc:
            created_at = scraper_doc.get("created_at")
            uptime_hours = 0
            if created_at:
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                uptime_hours = (now - created_at).total_seconds() / 3600

            metrics = scraper_doc.get("metrics", {})
            scraper_metrics = {
                "uptime_hours": round(uptime_hours, 2),
                "total_cycles": metrics.get("total_cycles", 0),
                "total_posts_collected": metrics.get("total_posts_collected", 0),
                "total_comments_collected": metrics.get("total_comments_collected", 0),
                "posts_per_hour": metrics.get("posts_per_hour", 0),
                "comments_per_hour": metrics.get("comments_per_hour", 0),
                "last_cycle": {
                    "time": metrics.get("last_cycle_time"),
                    "posts": metrics.get("last_cycle_posts", 0),
                    "comments": metrics.get("last_cycle_comments", 0),
                    "duration_seconds": metrics.get("last_cycle_duration", 0)
                },
                "avg_cycle_duration": metrics.get("avg_cycle_duration", 0),
                "container_status": scraper_doc.get("status"),
                "restart_count": scraper_doc.get("restart_count", 0),
                "last_error": scraper_doc.get("last_error")
            }

        # === ERROR TRACKING ===
        errors_collection = db[COLLECTIONS["SCRAPE_ERRORS"]]
        total_errors = errors_collection.count_documents({"subreddit": subreddit})
        unresolved_errors = errors_collection.count_documents({"subreddit": subreddit, "resolved": False})
        recent_errors_24h = errors_collection.count_documents({
            "subreddit": subreddit,
            "timestamp": {"$gte": day_ago}
        })

        error_type_pipeline = [
            {"$match": {"subreddit": subreddit}},
            {"$group": {"_id": "$error_type", "count": {"$sum": 1}}}
        ]
        error_type_result = list(errors_collection.aggregate(error_type_pipeline))
        error_types = {item["_id"]: item["count"] for item in error_type_result if item["_id"]}

        # === SUBREDDIT METADATA ===
        subreddit_metadata = subreddit_collection.find_one({"subreddit_name": subreddit})
        metadata_info = {
            "exists": subreddit_metadata is not None,
            "last_updated": subreddit_metadata.get("last_updated") if subreddit_metadata else None,
            "subscribers": subreddit_metadata.get("subscribers") if subreddit_metadata else None,
            "active_users": subreddit_metadata.get("active_user_count") if subreddit_metadata else None,
            "created_utc": subreddit_metadata.get("created_utc") if subreddit_metadata else None,
            "age_days": None,
            "over_18": subreddit_metadata.get("over_18") if subreddit_metadata else None,
            "language": subreddit_metadata.get("lang") if subreddit_metadata else None
        }
        if subreddit_metadata and subreddit_metadata.get("created_utc"):
            created = datetime.fromtimestamp(subreddit_metadata["created_utc"], UTC)
            metadata_info["age_days"] = (now - created).days

        # Build base response
        response = {
            "subreddit": subreddit,
            "total_posts": total_posts,
            "total_comments": total_comments,
            "coverage": {
                "posts_with_initial_comments": posts_with_initial_comments,
                "posts_without_initial_comments": posts_without_initial_comments,
                "initial_completion_rate": (posts_with_initial_comments / total_posts * 100) if total_posts > 0 else 0,
                "date_range": date_range
            },
            "recent_activity_24h": {
                "posts_scraped": posts_scraped_24h,
                "comments_scraped": comments_scraped_24h,
                "posts_updated": posts_updated_24h
            },
            "content_stats": {
                "avg_comments_per_post": content_stats.get("avg_comments", 0),
                "avg_score_per_post": content_stats.get("avg_score", 0),
                "avg_upvote_ratio": content_stats.get("avg_upvote_ratio", 0),
                "self_posts": content_stats.get("self_posts", 0),
                "link_posts": content_stats.get("link_posts", 0),
                "nsfw_posts": content_stats.get("nsfw_posts", 0),
                "locked_posts": content_stats.get("locked_posts", 0),
                "stickied_posts": content_stats.get("stickied_posts", 0),
                "posts_by_sort_method": posts_by_sort_method
            },
            "comment_stats": {
                "avg_comment_score": comment_stats.get("avg_score", 0),
                "max_comment_depth": comment_stats.get("max_depth", 0),
                "gilded_comments": comment_stats.get("gilded_count", 0),
                "awarded_comments": comment_stats.get("awarded_count", 0),
                "top_level_comments": comment_stats.get("top_level", 0),
                "reply_comments": comment_stats.get("replies", 0)
            },
            "scraper_metrics": scraper_metrics,
            "errors": {
                "total_errors": total_errors,
                "unresolved_errors": unresolved_errors,
                "error_types": error_types,
                "recent_errors_24h": recent_errors_24h
            },
            "subreddit_metadata": metadata_info
        }

        # === DETAILED ANALYTICS (Optional, expensive) ===
        if detailed:
            # Top posts by score
            top_posts = list(posts_collection.find(
                {"subreddit": subreddit},
                {"post_id": 1, "title": 1, "score": 1, "num_comments": 1, "_id": 0}
            ).sort("score", -1).limit(10))

            # Most commented posts
            most_commented = list(posts_collection.find(
                {"subreddit": subreddit},
                {"post_id": 1, "title": 1, "score": 1, "num_comments": 1, "_id": 0}
            ).sort("num_comments", -1).limit(10))

            # Top authors
            author_pipeline = [
                {"$match": {"subreddit": subreddit}},
                {"$group": {
                    "_id": "$author",
                    "post_count": {"$sum": 1},
                    "total_score": {"$sum": "$score"}
                }},
                {"$sort": {"post_count": -1}},
                {"$limit": 10}
            ]
            top_authors_result = list(posts_collection.aggregate(author_pipeline))
            top_authors = [
                {
                    "author": item["_id"],
                    "post_count": item["post_count"],
                    "total_score": item["total_score"]
                }
                for item in top_authors_result if item["_id"]
            ]

            response["detailed"] = {
                "top_posts_by_score": top_posts,
                "most_commented_posts": most_commented,
                "top_authors": top_authors
            }

        return response

    except Exception as e:
        logger.error(f"Error getting stats for r/{subreddit}: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting stats: {str(e)}")

@app.get("/scrapers/{subreddit}/status")
async def get_scraper_status(subreddit: str, scraper_type: Optional[str] = None):
    """Get detailed status of a specific scraper

    Args:
        subreddit: Subreddit name
        scraper_type: Optional - "posts" or "comments". If not provided, returns first matching scraper.
    """
    scraper_data = load_scraper_from_db(subreddit, scraper_type or "posts")
    if not scraper_data:
        return {"status": "not_found", "message": "No scraper found for this subreddit"}
    
    # Check if container is still running
    container_name = scraper_data.get("container_name")
    if container_name:
        container_status = check_container_status(container_name)
        if container_status == "running":
            status = "running"
        elif container_status == "exited":
            status = "stopped"
        elif container_status is None:
            status = "stopped"  # Container doesn't exist
        else:
            status = container_status
    else:
        status = scraper_data["status"]
    
    # Create safe credentials for display using centralized config
    safe_credentials = {
        "client_id": SECURITY_CONFIG["masked_credential_value"],
        "client_secret": SECURITY_CONFIG["masked_credential_value"],
        "username": scraper_data["config"].credentials.username,
        "password": SECURITY_CONFIG["masked_credential_value"],
        "user_agent": scraper_data["config"].credentials.user_agent
    }
    
    safe_config = {
        "posts_limit": scraper_data["config"].posts_limit,
        "interval": scraper_data["config"].interval,
        "comment_batch": scraper_data["config"].comment_batch,
        "credentials": safe_credentials,
        "auto_restart": scraper_data["config"].auto_restart
    }
    
    return {
        "subreddit": subreddit,
        "scraper_type": scraper_data.get("scraper_type", "posts"),
        "status": status,
        "container_id": scraper_data.get("container_id"),
        "container_name": container_name,
        "started_at": scraper_data["created_at"],
        "last_updated": scraper_data["last_updated"],
        "config": safe_config,
        "last_error": scraper_data.get("last_error"),
        "restart_count": scraper_data.get("restart_count", 0)
    }

@app.get("/scrapers/{subreddit}/logs")
async def get_scraper_logs(subreddit: str, lines: int = 100, scraper_type: Optional[str] = None):
    """Get recent logs from a scraper container

    Args:
        subreddit: Subreddit name
        lines: Number of log lines to return (default: 100)
        scraper_type: Optional - "posts" or "comments". If not provided, returns first matching scraper.
    """
    scraper_data = load_scraper_from_db(subreddit, scraper_type or "posts")
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")
    
    container_name = scraper_data.get("container_name")
    if not container_name:
        raise HTTPException(status_code=400, detail="No container found for this scraper")
    
    logs = get_container_logs(container_name, lines)
    if logs is None:
        raise HTTPException(status_code=404, detail="Container not found or no logs available")
    
    return {
        "subreddit": subreddit,
        "container_name": container_name,
        "logs": logs,
        "lines_requested": lines
    }

@app.post("/scrapers/{subreddit}/restart")
async def restart_scraper_endpoint(subreddit: str, background_tasks: BackgroundTasks, scraper_type: Optional[str] = None):
    """Manually restart a scraper

    Args:
        subreddit: Subreddit name
        scraper_type: Optional - "posts" or "comments". If not provided, restarts first matching scraper.
    """
    scraper_data = load_scraper_from_db(subreddit, scraper_type or "posts")
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")

    # Get actual scraper_type from loaded data
    actual_scraper_type = scraper_data.get("scraper_type", "posts")

    # Stop and remove existing container first
    container_name = scraper_data.get("container_name")
    if container_name:
        cleanup_container(container_name)

    # Start new container
    background_tasks.add_task(restart_scraper, scraper_data["config"], subreddit)

    return {"message": f"Restarting scraper for r/{subreddit} ({actual_scraper_type})"}

@app.put("/scrapers/{subreddit}/auto-restart")
async def toggle_auto_restart(subreddit: str, auto_restart: bool, scraper_type: Optional[str] = None):
    """Toggle auto-restart setting for a scraper

    Args:
        subreddit: Subreddit name
        auto_restart: Enable or disable auto-restart
        scraper_type: Optional - "posts" or "comments". If not provided, updates first matching scraper.
    """
    scraper_data = load_scraper_from_db(subreddit, scraper_type or "posts")
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")

    # Get actual scraper_type from loaded data
    actual_scraper_type = scraper_data.get("scraper_type", "posts")

    # Update auto-restart setting in database - try with scraper_type first
    result = scrapers_collection.update_one(
        {"subreddit": subreddit, "scraper_type": actual_scraper_type},
        {"$set": {"auto_restart": auto_restart, "last_updated": datetime.now(UTC)}}
    )

    # Backwards compatibility for old records
    if result.matched_count == 0:
        result = scrapers_collection.update_one(
            {"subreddit": subreddit, "scraper_type": {"$exists": False}},
            {"$set": {"auto_restart": auto_restart, "last_updated": datetime.now(UTC)}}
        )

    if result.modified_count > 0:
        return {"message": f"Auto-restart {'enabled' if auto_restart else 'disabled'} for r/{subreddit} ({actual_scraper_type})"}
    else:
        raise HTTPException(status_code=500, detail="Failed to update auto-restart setting")

@app.patch("/scrapers/{subreddit}/subreddits")
async def update_scraper_subreddits(
    subreddit: str,
    request: SubredditUpdateRequest,
    background_tasks: BackgroundTasks,
    scraper_type: Optional[str] = "posts"
):
    """Update subreddits for a running scraper (triggers container restart).

    Replaces the entire subreddit list. Container will restart with ~5-10s downtime.

    Args:
        subreddit: Primary subreddit identifier (first in the list)
        request: New list of subreddits
        scraper_type: "posts" or "comments" (default: "posts")

    Returns:
        Updated configuration with rate limit analysis
    """
    # Validate max subreddits
    max_subs = MULTI_SCRAPER_CONFIG["max_subreddits_per_container"]
    if len(request.subreddits) > max_subs:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {max_subs} subreddits per container"
        )

    if len(request.subreddits) == 0:
        raise HTTPException(
            status_code=400,
            detail="At least one subreddit is required"
        )

    # Clean and deduplicate subreddit names
    new_subs = []
    seen = set()
    for s in request.subreddits:
        clean = s.strip().lower()
        if clean and clean not in seen:
            new_subs.append(clean)
            seen.add(clean)

    if not new_subs:
        raise HTTPException(status_code=400, detail="No valid subreddit names provided")

    # Load existing scraper
    scraper_data = load_scraper_from_db(subreddit, scraper_type)
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")

    # Get existing config (it's a ScraperConfig Pydantic model)
    existing_config = scraper_data["config"]

    # Get previous subreddits from config
    prev_subs = existing_config.subreddits if existing_config.subreddits else [subreddit]

    # Calculate what changed
    added = [s for s in new_subs if s not in prev_subs]
    removed = [s for s in prev_subs if s not in new_subs]

    # Calculate rate limit info
    config_dict = {
        "sorting_methods": existing_config.sorting_methods,
        "interval": existing_config.interval
    }
    rate_info = calculate_rate_limit_info(len(new_subs), config_dict)

    # Stop existing container
    container_name = scraper_data.get("container_name")
    if container_name:
        cleanup_container(container_name)
        logger.info(f"Stopped container {container_name} for subreddit update")

    # Update database with new subreddits
    scrapers_collection.update_one(
        {"subreddit": subreddit, "scraper_type": scraper_type},
        {"$set": {
            "subreddits": new_subs,
            "last_updated": datetime.now(UTC)
        }}
    )

    # Build updated config using existing config values
    updated_config = ScraperConfig(
        name=existing_config.name,
        subreddit=new_subs[0],
        subreddits=new_subs,
        scraper_type=scraper_type,
        posts_limit=existing_config.posts_limit,
        interval=existing_config.interval,
        comment_batch=existing_config.comment_batch,
        sorting_methods=existing_config.sorting_methods,
        credentials=existing_config.credentials,
        auto_restart=existing_config.auto_restart
    )

    # Restart with updated config
    background_tasks.add_task(run_scraper, updated_config)
    logger.info(f"Restarting scraper with updated subreddits: {new_subs}")

    response = {
        "message": "Subreddits updated, restarting container",
        "subreddits": new_subs,
        "previous_subreddits": prev_subs,
        "added": added,
        "removed": removed,
        "subreddit_count": len(new_subs),
        "rate_limit": rate_info,
        "estimated_restart_time": "5-10 seconds"
    }

    # Add warning if approaching limits
    if rate_info["warning_level"] in ["warning", "critical"]:
        response["rate_limit_warning"] = rate_info["recommendation"]

    return response

@app.get("/scrapers/rate-limit-preview")
async def preview_rate_limit(
    subreddit_count: int,
    sorting_methods: str = "new,top,rising",
    interval: int = 300
):
    """Preview rate limit usage for a given configuration.

    Useful for planning before creating or updating scrapers.

    Args:
        subreddit_count: Number of subreddits to scrape
        sorting_methods: Comma-separated list of sorting methods (e.g., "new,top,rising")
        interval: Scrape interval in seconds

    Returns:
        Rate limit analysis with warning level and recommendations
    """
    if subreddit_count <= 0:
        raise HTTPException(status_code=400, detail="subreddit_count must be positive")

    max_subs = MULTI_SCRAPER_CONFIG["max_subreddits_per_container"]
    if subreddit_count > max_subs:
        raise HTTPException(status_code=400, detail=f"Maximum {max_subs} subreddits per container")

    methods = [s.strip() for s in sorting_methods.split(",") if s.strip()]
    if not methods:
        methods = ["new", "top", "rising"]

    config = {
        "sorting_methods": methods,
        "interval": interval
    }

    return calculate_rate_limit_info(subreddit_count, config)

@app.delete("/scrapers/{subreddit}")
async def remove_scraper(subreddit: str, scraper_type: Optional[str] = None):
    """Remove a scraper completely (stop it first if running).

    Args:
        subreddit: Subreddit name
        scraper_type: Optional - "posts" or "comments". If not provided, removes any scraper for this subreddit.
    """
    # Build query - support old records without scraper_type field
    if scraper_type:
        query = {"subreddit": subreddit, "scraper_type": scraper_type}
    else:
        # Try to find any scraper for this subreddit (backwards compatibility)
        query = {"subreddit": subreddit}

    scraper_doc = scrapers_collection.find_one(query)
    if not scraper_doc:
        raise HTTPException(status_code=404, detail="Scraper not found")

    # Get the actual scraper_type from the document (may be None for old records)
    actual_scraper_type = scraper_doc.get("scraper_type", "posts")

    # Stop and remove container if it exists
    container_name = scraper_doc.get("container_name")
    if container_name:
        cleanup_container(container_name)
        logger.info(f"Cleaned up container {container_name} for r/{subreddit}")

    # Remove from database using the same query
    result = scrapers_collection.delete_one({"_id": scraper_doc["_id"]})

    # Remove from memory cache (try both old and new key formats)
    cache_key = f"{subreddit}:{actual_scraper_type}"
    if cache_key in active_scrapers:
        del active_scrapers[cache_key]
    if subreddit in active_scrapers:  # Old format fallback
        del active_scrapers[subreddit]

    if result.deleted_count > 0:
        return {"message": f"Scraper removed for r/{subreddit} ({actual_scraper_type})"}
    else:
        raise HTTPException(status_code=500, detail="Failed to remove scraper")

@app.get("/stats/global")
async def get_global_stats():
    """Get cross-subreddit statistics for all scrapers"""
    if not mongo_connected:
        raise HTTPException(status_code=500, detail="Database not connected")

    try:
        # Get all scrapers
        all_scrapers = list(scrapers_collection.find({}))
        total_scrapers = len(all_scrapers)
        active_scrapers = sum(1 for s in all_scrapers if s.get("status") == "running")
        failed_scrapers = sum(1 for s in all_scrapers if s.get("status") == "failed")

        # Get total posts and comments across all subreddits
        total_posts_all = posts_collection.count_documents({})
        total_comments_all = comments_collection.count_documents({})

        # Get per-subreddit breakdown
        subreddit_breakdown = []
        for scraper in all_scrapers:
            subreddit = scraper["subreddit"]
            posts_count = posts_collection.count_documents({"subreddit": subreddit})
            comments_count = comments_collection.count_documents({"subreddit": subreddit})

            subreddit_breakdown.append({
                "subreddit": subreddit,
                "status": scraper.get("status"),
                "total_posts": posts_count,
                "total_comments": comments_count,
                "container_name": scraper.get("container_name")
            })

        # Sort by total posts descending
        subreddit_breakdown.sort(key=lambda x: x["total_posts"], reverse=True)

        # Get unresolved errors across all subreddits
        errors_collection = db[COLLECTIONS["SCRAPE_ERRORS"]]
        total_errors_unresolved = errors_collection.count_documents({"resolved": False})

        # Get total unique authors
        unique_authors_pipeline = [
            {"$group": {"_id": "$author"}},
            {"$count": "total"}
        ]
        authors_result = list(posts_collection.aggregate(unique_authors_pipeline))
        total_unique_authors = authors_result[0]["total"] if authors_result else 0

        return {
            "summary": {
                "total_subreddits": total_scrapers,
                "active_scrapers": active_scrapers,
                "failed_scrapers": failed_scrapers,
                "total_posts_all": total_posts_all,
                "total_comments_all": total_comments_all,
                "total_unique_authors": total_unique_authors,
                "total_errors_unresolved": total_errors_unresolved
            },
            "subreddit_breakdown": subreddit_breakdown,
            "timestamp": datetime.now(UTC)
        }

    except Exception as e:
        logger.error(f"Error getting global stats: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting global stats: {str(e)}")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    # Check if Docker is available
    try:
        result = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=5)
        docker_available = result.returncode == 0
        docker_version = result.stdout.strip() if docker_available else None
    except:
        docker_available = False
        docker_version = None
    
    # Count running containers
    running_containers = 0
    total_scrapers = 0
    failed_scrapers = 0
    
    try:
        scrapers = scrapers_collection.find({})
        for scraper_doc in scrapers:
            total_scrapers += 1
            if scraper_doc.get("container_name") and scraper_doc["status"] == "running":
                container_status = check_container_status(scraper_doc["container_name"])
                if container_status == "running":
                    running_containers += 1
                else:
                    failed_scrapers += 1
            elif scraper_doc["status"] in ["error", "failed"]:
                failed_scrapers += 1
    except:
        # Fallback to memory cache
        for subreddit, info in active_scrapers.items():
            total_scrapers += 1
            if "container_name" in info:
                container_status = check_container_status(info["container_name"])
                if container_status == "running":
                    running_containers += 1
    
    return {
        "status": "healthy",
        "total_scrapers": total_scrapers,
        "running_containers": running_containers,
        "failed_scrapers": failed_scrapers,
        "database_connected": mongo_connected,
        "docker_available": docker_available,
        "docker_version": docker_version,
        "timestamp": datetime.now(UTC)
    }


@app.get("/metrics")
async def prometheus_metrics():
    """
    Prometheus metrics endpoint for Grafana/Prometheus monitoring.
    Returns metrics in Prometheus text format.
    """
    try:
        # Update metrics from current database state
        update_metrics_from_db(
            db,
            posts_collection,
            comments_collection,
            scrapers_collection,
            errors_collection
        )

        # Set system health metrics
        scraper_up.set(1)
        database_connected.set(1 if mongo_connected else 0)

        # Check Docker availability
        try:
            result = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=5)
            docker_available_metric.set(1 if result.returncode == 0 else 0)
        except:
            docker_available_metric.set(0)

    except Exception as e:
        logger.error(f"Error updating Prometheus metrics: {e}")
        scraper_up.set(0)

    return Response(content=get_metrics(), media_type=CONTENT_TYPE_LATEST)


@app.get("/presets")
async def get_presets():
    """Get predefined configuration presets (optimized for 5 scrapers per account)"""
    return {
        "high_activity": {
            "description": "For very active subreddits (wallstreetbets, stocks)",
            "posts_limit": 150,
            "interval": 60,
            "comment_batch": 12,
            "sorting_methods": ["new", "top", "rising"]
        },
        "medium_activity": {
            "description": "For moderately active subreddits (investing, cryptocurrency)",
            "posts_limit": 100,
            "interval": 60,
            "comment_batch": 12,
            "sorting_methods": ["new", "top", "rising"]
        },
        "low_activity": {
            "description": "For smaller subreddits (pennystocks, niche topics)",
            "posts_limit": 80,
            "interval": 60,
            "comment_batch": 10,
            "sorting_methods": ["new", "top", "rising"]
        }
    }

@app.get("/accounts")
async def list_saved_accounts():
    """List all saved Reddit accounts (without sensitive data)"""
    try:
        accounts = load_saved_accounts()
        safe_accounts = {}
        
        for account_name, account_data in accounts.items():
            safe_accounts[account_name] = {
                "account_name": account_name,
                "username": account_data["username"],
                "user_agent": account_data["user_agent"],
                "created_at": account_data.get("created_at")
            }
        
        return safe_accounts
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading accounts: {str(e)}")

@app.post("/accounts")
async def save_account(
    account_name: str,
    credentials: RedditCredentials,
    skip_validation: bool = False
):
    """
    Save Reddit credentials for reuse.

    Validates credentials against Reddit API before saving (unless skip_validation=true).
    """
    if not account_name or not account_name.strip():
        raise HTTPException(status_code=400, detail="Account name is required")

    # Validate credentials are provided
    if not all([
        credentials.client_id,
        credentials.client_secret,
        credentials.username,
        credentials.password,
        credentials.user_agent
    ]):
        raise HTTPException(
            status_code=400,
            detail="All Reddit API credentials are required"
        )

    # Validate credentials against Reddit API
    if not skip_validation:
        validation_result = validate_reddit_credentials(credentials)

        if not validation_result["valid"]:
            # Map error types to appropriate HTTP status codes
            error_type = validation_result.get("error_type", "unknown_error")
            status_map = {
                "oauth_error": 401,
                "invalid_password": 401,
                "account_suspended": 403,
                "forbidden": 403,
                "rate_limited": 429,
                "server_error": 503,
                "network_error": 503,
            }
            status_code = status_map.get(error_type, 400)

            raise HTTPException(
                status_code=status_code,
                detail=validation_result["error"]
            )

        logger.info(f"Credentials validated for Reddit user: {validation_result['username']}")

    success = save_reddit_account(account_name.strip(), credentials)
    if success:
        return {"message": f"Account '{account_name}' saved successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to save account")

@app.post("/accounts/validate", response_model=CredentialValidationResponse)
async def validate_credentials(credentials: RedditCredentials):
    """
    Validate Reddit API credentials without saving them.

    Use this to test credentials before saving or to verify existing credentials.

    Returns:
        - valid: Whether credentials are valid
        - username: Authenticated Reddit username (if valid)
        - error: Error message (if invalid)
        - error_type: Error category (oauth_error, invalid_password, etc.)
    """
    # Validate all fields are provided
    if not all([
        credentials.client_id,
        credentials.client_secret,
        credentials.username,
        credentials.password,
        credentials.user_agent
    ]):
        return CredentialValidationResponse(
            valid=False,
            error="All Reddit API credentials are required",
            error_type="missing_fields"
        )

    result = validate_reddit_credentials(credentials)

    return CredentialValidationResponse(
        valid=result["valid"],
        username=result.get("username"),
        error=result.get("error"),
        error_type=result.get("error_type")
    )

@app.get("/accounts/stats")
async def get_accounts_stats():
    """Get account usage statistics - scrapers and subreddits per account"""
    try:
        accounts = list(accounts_collection.find({}))
        scrapers = list(scrapers_collection.find({}))

        stats = {}
        for account in accounts:
            username = account["username"]
            account_name = account["account_name"]

            # Find scrapers using this account (match by username)
            using_scrapers = [s for s in scrapers if s.get("credentials", {}).get("username") == username]

            # Count subreddits (handle multi-subreddit scrapers)
            subreddits = set()
            for s in using_scrapers:
                subs = s.get("subreddits", [s.get("subreddit")])
                if subs:
                    subreddits.update([sub for sub in subs if sub])

            running_count = sum(1 for s in using_scrapers if s.get("status") == "running")

            stats[account_name] = {
                "account_name": account_name,
                "username": username,
                "scraper_count": len(using_scrapers),
                "subreddit_count": len(subreddits),
                "running_count": running_count,
                "subreddits": sorted(list(subreddits)),
                "created_at": account.get("created_at")
            }

        return stats
    except Exception as e:
        logger.error(f"Error getting account stats: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting account stats: {str(e)}")

@app.delete("/accounts/{account_name}")
async def delete_account(account_name: str):
    """Delete a saved Reddit account"""
    success = delete_reddit_account(account_name)
    if success:
        return {"message": f"Account '{account_name}' deleted successfully"}
    else:
        raise HTTPException(status_code=404, detail="Account not found")

@app.get("/accounts/{account_name}")
async def get_account_info(account_name: str):
    """Get account info (without sensitive credentials)"""
    try:
        accounts = load_saved_accounts()
        if account_name not in accounts:
            raise HTTPException(status_code=404, detail="Account not found")
        
        account = accounts[account_name]
        return {
            "account_name": account_name,
            "username": account["username"],
            "user_agent": account["user_agent"],
            "created_at": account.get("created_at")
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting account info: {str(e)}")

@app.post("/scrapers/restart-all-failed")
async def restart_all_failed_scrapers(background_tasks: BackgroundTasks):
    """Manually restart all failed or stopped scrapers with auto_restart enabled"""
    try:
        failed_scrapers = list(scrapers_collection.find({
            "status": {"$in": ["stopped", "failed", "error"]}, 
            "auto_restart": True
        }))
        
        if not failed_scrapers:
            return {"message": "No failed scrapers found that need restarting"}
        
        restarted_count = 0
        for scraper_doc in failed_scrapers:
            subreddit = scraper_doc["subreddit"]
            scraper_data = load_scraper_from_db(subreddit)
            if scraper_data and scraper_data["config"]:
                logger.info(f"Manually restarting failed scraper for r/{subreddit}")
                background_tasks.add_task(restart_scraper, scraper_data["config"], subreddit)
                restarted_count += 1
        
        return {
            "message": f"Initiated restart for {restarted_count} failed scrapers",
            "scrapers": [doc["subreddit"] for doc in failed_scrapers]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error restarting failed scrapers: {str(e)}")

@app.get("/scrapers/status-summary")
async def get_status_summary():
    """Get a summary of all scrapers by status"""
    try:
        pipeline = [
            {"$group": {
                "_id": "$status", 
                "count": {"$sum": 1},
                "scrapers": {"$push": "$subreddit"}
            }},
            {"$sort": {"_id": 1}}
        ]
        
        status_summary = list(scrapers_collection.aggregate(pipeline))
        
        total_scrapers = sum(item["count"] for item in status_summary)
        
        return {
            "total_scrapers": total_scrapers,
            "status_breakdown": {
                item["_id"]: {
                    "count": item["count"],
                    "scrapers": item["scrapers"]
                } for item in status_summary
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting status summary: {str(e)}")

# ======================= SEMANTIC SEARCH ENDPOINTS =======================

# Lazy loading of Azure OpenAI embedding client (only when needed)
_embedding_client = None

def get_embedding_client():
    """Lazy load the Azure OpenAI embedding client to avoid startup delay."""
    global _embedding_client
    if _embedding_client is None:
        try:
            endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
            api_key = os.getenv("AZURE_OPENAI_API_KEY")

            if not endpoint or not api_key:
                raise HTTPException(
                    status_code=500,
                    detail="Azure OpenAI not configured. Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY."
                )

            from openai import AzureOpenAI
            _embedding_client = AzureOpenAI(
                azure_endpoint=endpoint,
                api_key=api_key,
                api_version=AZURE_OPENAI_CONFIG.get("api_version", "2024-02-01")
            )
        except ImportError:
            raise HTTPException(status_code=500, detail="openai package not installed")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to initialize Azure OpenAI client: {str(e)}")
    return _embedding_client


def generate_query_embedding(query: str) -> list:
    """Generate embedding for a search query using Azure OpenAI."""
    client = get_embedding_client()
    deployment = os.getenv("AZURE_EMBEDDING_DEPLOYMENT", AZURE_OPENAI_CONFIG.get("embedding_deployment", "text-embedding-3-small"))

    response = client.embeddings.create(
        input=query,
        model=deployment
    )
    return response.data[0].embedding

@app.post("/search/subreddits")
async def semantic_search_subreddits(
    query: str,
    limit: int = 10,
    min_subscribers: int = 1000,
    exclude_nsfw: bool = True,
    language: str = None,
    subreddit_type: str = "public"
):
    """
    Semantic search for subreddits using natural language queries.

    Args:
        query: Natural language search query (e.g., "building b2b saas")
        limit: Number of results to return (default: 10)
        min_subscribers: Minimum subscriber count (default: 1000, use 0 for no filter)
        exclude_nsfw: Filter out NSFW subreddits (default: True)
        language: Language filter (e.g., "en", optional)
        subreddit_type: Filter by type (public/private/restricted, default: public)

    Returns:
        JSON with query and ranked results

    Example:
        POST /search/subreddits?query=building%20b2b%20saas&limit=10
    """
    try:
        # Generate query embedding using Azure OpenAI
        query_embedding = generate_query_embedding(query)

        # Build MongoDB filters
        filters = {}
        if subreddit_type and subreddit_type != "all":
            filters["subreddit_type"] = subreddit_type
        if exclude_nsfw:
            filters["over_18"] = False
        if language:
            filters["lang"] = language

        # Subscriber filter
        subscriber_filter = {}
        if min_subscribers > 0:
            subscriber_filter["$gte"] = min_subscribers

        # Build aggregation pipeline
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "subreddit_vector_index",
                    "path": "embeddings.combined_embedding",
                    "queryVector": query_embedding,
                    "numCandidates": 100,
                    "limit": limit,
                    "filter": filters
                }
            },
            {
                "$project": {
                    "subreddit_name": 1,
                    "title": 1,
                    "public_description": 1,
                    "subscribers": 1,
                    "active_user_count": 1,
                    "advertiser_category": 1,
                    "over_18": 1,
                    "subreddit_type": 1,
                    "lang": 1,
                    "url": 1,
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]

        # Add subscriber filter if needed
        if subscriber_filter:
            pipeline.insert(1, {"$match": {"subscribers": subscriber_filter}})

        # Execute search
        subreddit_discovery = db.subreddit_discovery
        results = list(subreddit_discovery.aggregate(pipeline))

        return {
            "query": query,
            "filters": {
                "min_subscribers": min_subscribers,
                "exclude_nsfw": exclude_nsfw,
                "language": language,
                "subreddit_type": subreddit_type
            },
            "count": len(results),
            "results": results
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Semantic search failed: {str(e)}")

@app.post("/discover/subreddits")
async def discover_subreddits_endpoint(query: str, limit: int = 50):
    """
    Discover new subreddits by topic and scrape comprehensive metadata.

    Args:
        query: Search query (e.g., "saas", "startup")
        limit: Maximum number of results per query (default: 50)

    Returns:
        JSON with discovered subreddits count and status

    Example:
        POST /discover/subreddits?query=saas&limit=50
    """
    try:
        import praw

        # Reddit API authentication
        reddit = praw.Reddit(
            client_id=os.getenv('R_CLIENT_ID'),
            client_secret=os.getenv('R_CLIENT_SECRET'),
            username=os.getenv('R_USERNAME'),
            password=os.getenv('R_PASSWORD'),
            user_agent=os.getenv('R_USER_AGENT')
        )

        # Search subreddits
        search_results = list(reddit.subreddits.search(query, limit=limit))

        discovered_count = 0
        errors = []

        for subreddit in search_results:
            try:
                # Import scraping function
                from reddit_scraper import UnifiedRedditScraper
                scraper = UnifiedRedditScraper(subreddit.display_name, {})
                metadata = scraper.scrape_subreddit_metadata()

                if metadata:
                    # Store in subreddit_discovery collection
                    db.subreddit_discovery.update_one(
                        {"subreddit_name": metadata["subreddit_name"]},
                        {"$set": metadata},
                        upsert=True
                    )
                    discovered_count += 1
            except Exception as e:
                errors.append({"subreddit": subreddit.display_name, "error": str(e)})

        return {
            "query": query,
            "found": len(search_results),
            "discovered": discovered_count,
            "errors": errors[:10]  # Limit error list
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {str(e)}")

@app.get("/embeddings/stats")
async def get_embedding_stats():
    """Get statistics about embeddings in all collections."""
    try:
        # Discovery collection stats
        subreddit_discovery = db.subreddit_discovery
        discovery_total = subreddit_discovery.count_documents({})
        discovery_with_embeddings = subreddit_discovery.count_documents(
            {"embeddings.combined_embedding": {"$exists": True}}
        )

        # Metadata collection stats (active scrapers)
        metadata_total = subreddit_collection.count_documents({})
        metadata_with_embeddings = subreddit_collection.count_documents(
            {"embeddings.combined_embedding": {"$exists": True}}
        )
        metadata_pending = subreddit_collection.count_documents(
            {"embedding_status": "pending"}
        )
        metadata_failed = subreddit_collection.count_documents(
            {"embedding_status": "failed"}
        )

        # Sample document to check dimensions
        sample = subreddit_discovery.find_one(
            {"embeddings.combined_embedding": {"$exists": True}}
        ) or subreddit_collection.find_one(
            {"embeddings.combined_embedding": {"$exists": True}}
        )

        model_info = {}
        if sample and 'embeddings' in sample:
            model_info = {
                "dimensions": len(sample['embeddings']['combined_embedding']),
                "model": sample['embeddings'].get('model', 'unknown'),
                "context_window": sample['embeddings'].get('context_window', 'unknown')
            }

        return {
            "discovery_collection": {
                "total": discovery_total,
                "with_embeddings": discovery_with_embeddings,
                "without_embeddings": discovery_total - discovery_with_embeddings,
                "completion_rate": round(discovery_with_embeddings / discovery_total * 100, 1) if discovery_total > 0 else 0
            },
            "metadata_collection": {
                "total": metadata_total,
                "with_embeddings": metadata_with_embeddings,
                "pending": metadata_pending,
                "failed": metadata_failed,
                "completion_rate": round(metadata_with_embeddings / metadata_total * 100, 1) if metadata_total > 0 else 0
            },
            "combined": {
                "total_with_embeddings": discovery_with_embeddings + metadata_with_embeddings
            },
            "model_info": model_info
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting stats: {str(e)}")


# ======================= EMBEDDING WORKER ENDPOINTS =======================

@app.get("/embeddings/worker/status")
async def get_embedding_worker_status():
    """Get the status of the background embedding worker."""
    try:
        if embedding_worker is None:
            return {
                "enabled": False,
                "reason": "Worker not initialized (check EMBEDDING_WORKER_CONFIG or module import)"
            }

        stats = embedding_worker.get_stats()
        return {
            "enabled": True,
            **stats
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting worker status: {str(e)}")


@app.post("/embeddings/worker/process")
async def trigger_embedding_processing(
    subreddit: str = None,
    force: bool = False
):
    """
    Manually trigger embedding processing.

    Args:
        subreddit: Process specific subreddit only (optional)
        force: Force reprocessing even if already complete

    Returns:
        Processing results
    """
    try:
        if embedding_worker is None:
            raise HTTPException(
                status_code=503,
                detail="Embedding worker not available"
            )

        if subreddit:
            # Process specific subreddit
            doc = subreddit_collection.find_one({"subreddit_name": subreddit})
            if not doc:
                raise HTTPException(
                    status_code=404,
                    detail=f"Subreddit r/{subreddit} not found in metadata"
                )

            if force or doc.get("embedding_status") != "complete":
                # Set to pending for processing
                subreddit_collection.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"embedding_status": "pending"}}
                )
                doc["embedding_status"] = "pending"

                success = embedding_worker.process_one(doc)
                return {
                    "subreddit": subreddit,
                    "success": success,
                    "status": "complete" if success else "failed"
                }
            else:
                return {
                    "subreddit": subreddit,
                    "success": True,
                    "status": "already_complete",
                    "message": "Use force=true to reprocess"
                }
        else:
            # Process batch
            result = embedding_worker.process_batch()
            return {
                "batch_processed": True,
                **result
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")


# =============================================================================
# API USAGE TRACKING ENDPOINTS
# =============================================================================

@app.get("/api/usage")
async def get_api_usage():
    """
    Get overall Reddit API usage statistics.

    Returns aggregated API call counts, breakdowns by type and subreddit,
    average response times, and error rates.
    """
    if not mongo_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    try:
        stats = get_usage_stats(db)
        return {
            "status": "ok",
            **stats
        }
    except Exception as e:
        logger.error(f"Error getting API usage stats: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting API usage: {str(e)}")


@app.get("/api/usage/trends")
async def get_api_usage_trends(
    period: str = "day",
    granularity: str = "hour",
    subreddit: Optional[str] = None
):
    """
    Get time-series API usage data for charting.

    Args:
        period: Time period - "hour", "day", or "week" (default: "day")
        granularity: Data granularity - "minute", "hour", or "day" (default: "hour")
        subreddit: Optional subreddit filter

    Returns time-series data with timestamps, call counts, and error counts.
    """
    if not mongo_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    # Validate parameters
    if period not in ["hour", "day", "week"]:
        raise HTTPException(status_code=400, detail="period must be 'hour', 'day', or 'week'")
    if granularity not in ["minute", "hour", "day"]:
        raise HTTPException(status_code=400, detail="granularity must be 'minute', 'hour', or 'day'")

    try:
        trends = get_usage_trends(db, period=period, granularity=granularity, subreddit=subreddit)
        return {
            "status": "ok",
            **trends
        }
    except Exception as e:
        logger.error(f"Error getting API usage trends: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting API usage trends: {str(e)}")


@app.get("/api/usage/cost")
async def get_api_cost(subreddit: Optional[str] = None):
    """
    Get Reddit API cost analysis with projections and averages.

    Args:
        subreddit: Optional subreddit filter

    Returns actual HTTP request counts and estimated costs at $0.24 per 1,000 requests.
    Includes hourly/daily averages and monthly projections.
    """
    if not mongo_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    try:
        stats = get_usage_stats(db, subreddit=subreddit)

        # Extract cost data
        actual_requests_today = stats.get("actual_http_requests_today", 0)
        actual_requests_hour = stats.get("actual_http_requests_hour", 0)
        cost_today = stats.get("cost_usd_today", 0)
        cost_hour = stats.get("cost_usd_hour", 0)

        # Calculate hours elapsed since tracking started (not since midnight)
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hour_start = now - timedelta(hours=1)

        # Get actual tracking start time (first record today)
        usage_collection = db[COLLECTIONS["API_USAGE"]]
        first_record = usage_collection.find_one(
            {"timestamp": {"$gte": today_start}},
            sort=[("timestamp", 1)]
        )

        if first_record:
            tracking_start = first_record["timestamp"]
            hours_elapsed = max((now - tracking_start).total_seconds() / 3600, 0.1)
        else:
            hours_elapsed = 1  # Fallback if no records

        # Count actual posts/comments scraped (output context)
        scrape_filter_today = {"scraped_at": {"$gte": today_start}}
        scrape_filter_hour = {"scraped_at": {"$gte": hour_start}}
        if subreddit:
            scrape_filter_today["subreddit"] = subreddit
            scrape_filter_hour["subreddit"] = subreddit

        posts_today = posts_collection.count_documents(scrape_filter_today)
        comments_today = comments_collection.count_documents(scrape_filter_today)
        posts_hour = posts_collection.count_documents(scrape_filter_hour)
        comments_hour = comments_collection.count_documents(scrape_filter_hour)

        # Avg per hour (today's total ÷ hours elapsed)
        avg_hourly_requests = actual_requests_today / hours_elapsed
        avg_hourly_cost = cost_today / hours_elapsed

        # Get historical average (last 7 days) for avg/day
        week_ago = now - timedelta(days=7)

        # Aggregate daily totals for last 7 days
        daily_pipeline = [
            {"$match": {"timestamp": {"$gte": week_ago, "$lt": today_start}}},
            {
                "$group": {
                    "_id": "$day_bucket",
                    "daily_requests": {"$sum": {"$ifNull": ["$actual_http_requests", 0]}},
                    "daily_cost": {"$sum": {"$ifNull": ["$estimated_cost_usd", 0]}}
                }
            }
        ]
        daily_results = list(usage_collection.aggregate(daily_pipeline))

        if daily_results:
            total_historical_requests = sum(d["daily_requests"] for d in daily_results)
            total_historical_cost = sum(d["daily_cost"] for d in daily_results)
            num_days = len(daily_results)
            avg_daily_requests = total_historical_requests / num_days
            avg_daily_cost = total_historical_cost / num_days
        else:
            # No historical data, use today's extrapolated values
            avg_daily_requests = avg_hourly_requests * 24
            avg_daily_cost = avg_hourly_cost * 24

        # Project monthly from avg/day
        projected_monthly_requests = avg_daily_requests * 30
        projected_monthly_cost = avg_daily_cost * 30

        # Calculate accuracy ratio (tracked vs actual)
        tracked_calls_today = stats.get("total_calls_today", 0)
        tracked_calls_hour = stats.get("total_calls_hour", 0)
        accuracy_ratio = tracked_calls_today / actual_requests_today if actual_requests_today > 0 else 1.0

        return {
            "status": "ok",
            "subreddit": subreddit,
            "pricing": {
                "cost_per_1000_requests": 0.24,
                "currency": "USD"
            },
            "today": {
                "actual_http_requests": actual_requests_today,
                "tracked_calls": tracked_calls_today,
                "cost_usd": round(cost_today, 4),
                "accuracy_ratio": round(accuracy_ratio, 4),
                "posts_scraped": posts_today,
                "comments_scraped": comments_today
            },
            "last_hour": {
                "actual_http_requests": actual_requests_hour,
                "tracked_calls": tracked_calls_hour,
                "cost_usd": round(cost_hour, 4),
                "posts_scraped": posts_hour,
                "comments_scraped": comments_hour
            },
            "averages": {
                "hourly_requests": round(avg_hourly_requests),
                "hourly_cost_usd": round(avg_hourly_cost, 4),
                "daily_requests": round(avg_daily_requests),
                "daily_cost_usd": round(avg_daily_cost, 2),
                "days_of_data": len(daily_results) if daily_results else 0
            },
            "projections": {
                "monthly_requests": round(projected_monthly_requests),
                "monthly_cost_usd": round(projected_monthly_cost, 2)
            },
            "by_subreddit": stats.get("calls_by_subreddit", {}) if not subreddit else None
        }
    except Exception as e:
        logger.error(f"Error getting API cost analysis: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting API cost: {str(e)}")


@app.get("/api/usage/{subreddit}")
async def get_api_usage_by_subreddit(subreddit: str):
    """
    Get Reddit API usage statistics for a specific subreddit.

    Args:
        subreddit: The subreddit name to get stats for

    Returns per-subreddit API call counts, breakdowns by type,
    average response times, and current rate limit status.
    """
    if not mongo_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    try:
        stats = get_usage_stats(db, subreddit=subreddit)
        return {
            "status": "ok",
            "subreddit": subreddit,
            **stats
        }
    except Exception as e:
        logger.error(f"Error getting API usage stats for r/{subreddit}: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting API usage: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=API_CONFIG["host"], port=API_CONFIG["port"])