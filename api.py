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
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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

# Mount static files and configure templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
async def dashboard(request: Request):
    """Web dashboard for managing Reddit scrapers"""
    return templates.TemplateResponse("dashboard.html", {"request": request})

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
            # Ensure timezone-aware for comparison
            if tracking_start.tzinfo is None:
                tracking_start = tracking_start.replace(tzinfo=timezone.utc)
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

        return {
            "status": "ok",
            "subreddit": subreddit,
            "pricing": {
                "cost_per_1000_requests": 0.24,
                "currency": "USD"
            },
            "today": {
                "requests": actual_requests_today,
                "cost_usd": round(cost_today, 4),
                "posts_scraped": posts_today,
                "comments_scraped": comments_today
            },
            "last_hour": {
                "requests": actual_requests_hour,
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