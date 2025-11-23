#!/usr/bin/env python3
"""
Reddit Scraper Management API

A FastAPI application to manage multiple Reddit scrapers.
Start, stop, and monitor scrapers for different subreddits through HTTP endpoints.
Each scraper can use unique Reddit API credentials to avoid rate limit conflicts.
Includes persistent storage and automatic restart capabilities.
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Dict, List, Optional
import subprocess
import threading
import time
import os
import signal
from datetime import datetime, UTC
import pymongo
from dotenv import load_dotenv
import json
import base64
import hashlib
from cryptography.fernet import Fernet
import asyncio
import logging

# Import centralized configuration
from config import (
    DATABASE_NAME, COLLECTIONS, DEFAULT_SCRAPER_CONFIG, 
    MONITORING_CONFIG, API_CONFIG, DOCKER_CONFIG, SECURITY_CONFIG, LOGGING_CONFIG
)

# Load environment variables (fallback defaults)
load_dotenv()

# Configure logging with timestamps
logging.basicConfig(
    format=LOGGING_CONFIG["format"],
    datefmt=LOGGING_CONFIG["date_format"],
    level=getattr(logging, LOGGING_CONFIG["level"]),
    force=True  # Override any existing logging configuration
)
logger = logging.getLogger("reddit-scraper-api")

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

class ScraperConfig(BaseModel):
    subreddit: str
    posts_limit: int = DEFAULT_SCRAPER_CONFIG["posts_limit"]
    interval: int = DEFAULT_SCRAPER_CONFIG["scrape_interval"]
    comment_batch: int = DEFAULT_SCRAPER_CONFIG["posts_per_comment_batch"]
    sorting_methods: List[str] = DEFAULT_SCRAPER_CONFIG["sorting_methods"]  # Multiple sorting methods
    credentials: RedditCredentials
    auto_restart: bool = True  # Enable automatic restart on failure

class ScraperStatus(BaseModel):
    subreddit: str
    status: str  # "running", "stopped", "error", "failed"
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    config: Optional[ScraperConfig] = None
    last_error: Optional[str] = None

class ScraperStartRequest(BaseModel):
    subreddit: str
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

def save_scraper_to_db(subreddit: str, config: ScraperConfig, status: str = "starting",
                       container_id: str = None, container_name: str = None,
                       last_error: str = None):
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
            "subreddit": subreddit,
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
            "restart_count": 0
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

        # Upsert scraper document - only set created_at and metrics on insert
        scrapers_collection.update_one(
            {"subreddit": subreddit},
            {
                "$set": scraper_doc,
                "$setOnInsert": {
                    "created_at": datetime.now(UTC),
                    "metrics": metrics_init
                }
            },
            upsert=True
        )
        
        logger.info(f"Saved scraper configuration for r/{subreddit} to database")
        return True
        
    except Exception as e:
        logger.error(f"Error saving scraper to database: {e}")
        return False

def load_scraper_from_db(subreddit: str) -> Optional[dict]:
    """Load scraper configuration from database"""
    try:
        if not mongo_connected:
            logger.warning("Database not connected, cannot load scraper")
            return None
            
        scraper_doc = scrapers_collection.find_one({"subreddit": subreddit})
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
            subreddit=subreddit,
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
                         increment_restart: bool = False):
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
        
        result = scrapers_collection.update_one(
            {"subreddit": subreddit},
            update_operation
        )
        
        return result.modified_count > 0
        
    except Exception as e:
        logger.error(f"Error updating scraper status: {e}")
        return False

def load_all_scrapers_from_db():
    """Load all scrapers from database on startup"""
    try:
        scrapers = scrapers_collection.find({})
        for scraper_doc in scrapers:
            subreddit = scraper_doc["subreddit"]
            scraper_data = load_scraper_from_db(subreddit)
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
                
                active_scrapers[subreddit] = {
                    "config": safe_config,
                    "status": scraper_data["status"],
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
                container_name = scraper_doc.get("container_name")
                
                if container_name:
                    # Check if container is actually running
                    container_status = check_container_status(container_name)
                    
                    if container_status != "running":
                        logger.info(f"Detected failed container for r/{subreddit}, attempting restart...")
                        
                        # Load full config from database
                        scraper_data = load_scraper_from_db(subreddit)
                        if scraper_data and scraper_data["config"]:
                            # Update status to failed
                            update_scraper_status(subreddit, "failed", 
                                                last_error="Container stopped unexpectedly",
                                                increment_restart=True)
                            
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
                        logger.info(f"Auto-restarting stopped scraper for r/{subreddit}...")
                        
                        scraper_data = load_scraper_from_db(subreddit)
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
        logger.info(f"Restarting scraper for r/{subreddit}")
        
        # Stop and remove any existing container first using centralized naming
        container_name = f"{DOCKER_CONFIG['container_prefix']}{subreddit}"
        cleanup_container(container_name)
        
        # Update status to restarting
        update_scraper_status(subreddit, "restarting")
        
        # Start new container
        run_scraper(config)
        
    except Exception as e:
        logger.error(f"Error restarting scraper for r/{subreddit}: {e}")
        update_scraper_status(subreddit, "error", last_error=f"Restart failed: {str(e)}")

# Start background monitoring thread
if mongo_connected:
    monitoring_thread = threading.Thread(target=check_for_failed_scrapers, daemon=True)
    monitoring_thread.start()

# Load existing scrapers on startup
if mongo_connected:
    load_all_scrapers_from_db()

def run_scraper(config: ScraperConfig):
    """Run a scraper in a separate Docker container with unique credentials"""
    try:
        # Create unique container name using centralized prefix
        container_name = f"{DOCKER_CONFIG['container_prefix']}{config.subreddit}"
        
        # Save to database first
        save_scraper_to_db(config.subreddit, config, "starting", container_name=container_name)
        
        # Prepare environment variables for the container
        env_vars = [
            f"R_CLIENT_ID={config.credentials.client_id}",
            f"R_CLIENT_SECRET={config.credentials.client_secret}",
            f"R_USERNAME={config.credentials.username}",
            f"R_PASSWORD={config.credentials.password}",
            f"R_USER_AGENT={config.credentials.user_agent}",
            f"MONGODB_URI={os.getenv('MONGODB_URI', '')}"
        ]
        
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
        
        # Add the image and command
        cmd.extend([
            DOCKER_CONFIG["image_name"],
            "python", "reddit_scraper.py", config.subreddit,
            "--posts-limit", str(config.posts_limit),
            "--interval", str(config.interval),
            "--comment-batch", str(config.comment_batch),
            "--sorting-methods", ",".join(config.sorting_methods)
        ])
        
        # Stop and remove any existing container with the same name
        cleanup_container(container_name)
        
        # Start the container
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            error_msg = f"Failed to start container: {result.stderr}"
            update_scraper_status(config.subreddit, "error", last_error=error_msg)
            raise Exception(error_msg)
        
        container_id = result.stdout.strip()
        
        # Update database with container info and running status
        update_scraper_status(config.subreddit, "running", container_id=container_id, container_name=container_name)
        
        # Update memory cache with safe config (use model_copy instead of copy)
        config_safe = config.model_copy()
        config_safe.credentials = RedditCredentials(
            client_id=SECURITY_CONFIG["masked_credential_value"],
            client_secret=SECURITY_CONFIG["masked_credential_value"], 
            username=config.credentials.username,  # Keep username for identification
            password=SECURITY_CONFIG["masked_credential_value"],
            user_agent=config.credentials.user_agent
        )
        
        active_scrapers[config.subreddit] = {
            "container_id": container_id,
            "container_name": container_name,
            "config": config_safe,
            "started_at": datetime.now(UTC),
            "status": "running",
            "last_error": None
        }
        
        logger.info(f"Started container {container_name} ({container_id[:12]}) for r/{config.subreddit}")
        
    except Exception as e:
        error_msg = f"Error starting container for r/{config.subreddit}: {e}"
        logger.error(error_msg)
        update_scraper_status(config.subreddit, "error", last_error=str(e))
        if config.subreddit in active_scrapers:
            active_scrapers[config.subreddit]["status"] = "error"
            active_scrapers[config.subreddit]["last_error"] = str(e)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Enhanced web dashboard with credential input and persistent storage"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Reddit Scraper Dashboard</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                background: #000000;
                color: #e5e5e5;
                padding: 40px;
                min-height: 100vh;
            }
            h1 {
                font-size: 2.2em;
                color: #ffffff;
                margin-bottom: 8px;
                font-weight: 600;
                letter-spacing: -0.5px;
            }
            h2 {
                color: #d4d4d4;
                margin: 35px 0 18px 0;
                font-size: 1.6em;
                font-weight: 600;
            }
            h3 {
                color: #b4b4b4;
                margin: 20px 0 12px 0;
                font-weight: 500;
            }
            p { line-height: 1.6; margin: 10px 0; color: #a3a3a3; }
            a { color: #7c7c7c; text-decoration: none; transition: color 0.2s; }
            a:hover { color: #ffffff; }

            .scraper {
                background: #0a0a0a;
                border: 1px solid #1f1f1f;
                margin: 8px 0;
                border-radius: 6px;
                transition: all 0.2s ease;
                overflow: hidden;
            }
            .scraper:hover {
                border-color: #2a2a2a;
            }
            .running {
                border-left: 3px solid #22c55e;
            }
            .stopped {
                border-left: 3px solid #525252;
            }
            .error {
                border-left: 3px solid #ef4444;
            }
            .failed {
                border-left: 3px solid #dc2626;
            }

            .scraper-header {
                padding: 16px 20px;
                cursor: pointer;
                display: flex;
                justify-content: space-between;
                align-items: center;
                user-select: none;
                transition: background 0.2s;
            }
            .scraper-header:hover {
                background: #0d0d0d;
            }
            .scraper-title {
                display: flex;
                align-items: center;
                gap: 12px;
                flex: 1;
            }
            .scraper-title h3 {
                margin: 0;
                font-size: 1.1em;
                color: #e5e5e5;
            }
            .scraper-summary {
                display: flex;
                align-items: center;
                gap: 16px;
                font-size: 0.9em;
                color: #737373;
                flex-wrap: wrap;
            }
            .scraper-stat {
                display: flex;
                align-items: center;
                gap: 4px;
            }
            .expand-icon {
                font-size: 1.2em;
                transition: transform 0.2s;
                color: #737373;
            }
            .expanded .expand-icon {
                transform: rotate(180deg);
            }

            .scraper-details {
                max-height: 0;
                overflow: hidden;
                transition: max-height 0.3s ease-out;
                border-top: 1px solid #1f1f1f;
            }
            .scraper-details.show {
                max-height: 2000px;
                transition: max-height 0.5s ease-in;
            }
            .scraper-content {
                padding: 20px;
            }

            button {
                padding: 10px 18px;
                margin: 4px 4px 4px 0;
                border: 1px solid #2a2a2a;
                border-radius: 4px;
                cursor: pointer;
                transition: all 0.2s ease;
                font-weight: 500;
                font-size: 13px;
                background: #0a0a0a;
                color: #e5e5e5;
            }
            button:hover {
                background: #1a1a1a;
                border-color: #3a3a3a;
            }
            button:active { transform: scale(0.98); }
            button:disabled { opacity: 0.4; cursor: not-allowed; }

            .start {
                background: #16a34a;
                border-color: #16a34a;
                color: #ffffff;
            }
            .start:hover {
                background: #15803d;
                border-color: #15803d;
            }

            .stop {
                background: #dc2626;
                border-color: #dc2626;
                color: #ffffff;
            }
            .stop:hover {
                background: #b91c1c;
                border-color: #b91c1c;
            }

            .restart {
                background: #ea580c;
                border-color: #ea580c;
                color: #ffffff;
            }
            .restart:hover {
                background: #c2410c;
                border-color: #c2410c;
            }

            .delete {
                background: #7c2d12;
                border-color: #7c2d12;
                color: #fca5a5;
            }
            .delete:hover {
                background: #991b1b;
                border-color: #991b1b;
                color: #ffffff;
            }

            .stats {
                background: #0a0a0a;
                border-color: #2a2a2a;
                color: #e5e5e5;
            }
            .stats:hover {
                background: #1a1a1a;
                border-color: #3a3a3a;
            }

            .loading {
                background: #171717 !important;
                border-color: #2a2a2a !important;
                cursor: wait !important;
            }

            .spinner {
                display: inline-block;
                width: 14px;
                height: 14px;
                border: 2px solid #404040;
                border-radius: 50%;
                border-top-color: #e5e5e5;
                animation: spin 0.8s linear infinite;
                margin-right: 8px;
                vertical-align: middle;
            }
            @keyframes spin { to { transform: rotate(360deg); } }

            input, select, textarea {
                padding: 10px 12px;
                margin: 5px 5px 5px 0;
                border: 1px solid #2a2a2a;
                border-radius: 4px;
                background: #0a0a0a;
                color: #e5e5e5;
                transition: all 0.2s ease;
                font-size: 14px;
            }
            input:focus, select:focus, textarea:focus {
                outline: none;
                border-color: #525252;
                background: #0d0d0d;
            }
            input::placeholder { color: #525252; }

            .credentials-section {
                background: #0a0a0a;
                padding: 20px;
                border-radius: 6px;
                margin: 15px 0;
                border: 1px solid #1f1f1f;
            }

            .form-row { margin: 12px 0; display: flex; align-items: center; flex-wrap: wrap; }
            .form-row label {
                display: inline-block;
                min-width: 140px;
                color: #b4b4b4;
                font-weight: 500;
                font-size: 14px;
            }
            .form-row small { color: #737373; margin-left: 8px; }

            .collapsible {
                cursor: pointer;
                background: #0a0a0a;
                padding: 12px;
                border: 1px solid #2a2a2a;
                text-align: left;
                width: 100%;
                border-radius: 4px;
                color: #e5e5e5;
                transition: all 0.2s ease;
            }
            .collapsible:hover { background: #171717; }

            .content {
                display: none;
                padding: 15px;
                background: #0a0a0a;
                border: 1px solid #1f1f1f;
                border-radius: 4px;
                margin-top: 10px;
            }

            .toggle { position: relative; display: inline-block; width: 50px; height: 28px; }
            .toggle input { opacity: 0; width: 0; height: 0; }
            .slider {
                position: absolute;
                cursor: pointer;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background-color: #1f1f1f;
                transition: .3s;
                border-radius: 28px;
            }
            .slider:before {
                position: absolute;
                content: "";
                height: 20px;
                width: 20px;
                left: 4px;
                bottom: 4px;
                background-color: #737373;
                transition: .3s;
                border-radius: 50%;
            }
            input:checked + .slider {
                background-color: #16a34a;
            }
            input:checked + .slider:before {
                transform: translateX(22px);
                background-color: #ffffff;
            }

            .status-badge {
                padding: 4px 10px;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            .badge-running {
                background: #166534;
                color: #86efac;
            }
            .badge-stopped {
                background: #1f1f1f;
                color: #a3a3a3;
            }
            .badge-error {
                background: #7f1d1d;
                color: #fca5a5;
            }
            .badge-failed {
                background: #991b1b;
                color: #fecaca;
            }

            .loading-overlay {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background-color: rgba(0, 0, 0, 0.85);
                z-index: 1000;
            }
            .loading-message {
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                background: #0a0a0a;
                padding: 30px 40px;
                border-radius: 6px;
                text-align: center;
                border: 1px solid #2a2a2a;
                color: #e5e5e5;
            }

            #health-status > div {
                background: #0a0a0a !important;
                border: 1px solid #1f1f1f !important;
                border-radius: 6px;
            }

            #accountManagerModal > div {
                background: #0a0a0a !important;
                border: 1px solid #2a2a2a;
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
        
        <h1>Reddit Scraper Management Dashboard</h1>
        <p><strong>Features:</strong> Persistent storage, unique credentials per scraper, automatic restart on failure</p>
        
        <div id="health-status"></div>
        
        <div id="scrapers"></div>
        
        <h2>Start New Scraper</h2>
        <div>
            <div class="form-row">
                <label>Subreddit:</label>
                <input type="text" id="subreddit" placeholder="wallstreetbets" />
                
                <label>Preset:</label>
                <select id="preset">
                    <option value="custom">Custom</option>
                    <option value="high">High Activity (wallstreetbets, stocks)</option>
                    <option value="medium">Medium Activity (investing, crypto)</option>
                    <option value="low">Low Activity (pennystocks, niche)</option>
                </select>
            </div>
            
            <div class="form-row">
                <label>Posts Limit:</label>
                <input type="number" id="posts_limit" value="1000" />

                <label>Interval (sec):</label>
                <input type="number" id="interval" value="60" />

                <label>Comment Batch:</label>
                <input type="number" id="comment_batch" value="50" />

                <label>Auto-restart:</label>
                <label class="toggle">
                    <input type="checkbox" id="auto_restart" checked>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="form-row">
                <label style="vertical-align: top;">Sorting Methods:</label>
                <div style="display: inline-flex; flex-direction: column; gap: 8px;">
                    <label style="color: #e5e5e5; font-weight: normal; cursor: pointer;">
                        <input type="checkbox" name="sorting" value="new" checked style="cursor: pointer; margin-right: 6px;"> new
                        <small style="color: #737373; margin-left: 8px;">(Captures ALL new posts)</small>
                    </label>
                    <label style="color: #e5e5e5; font-weight: normal; cursor: pointer;">
                        <input type="checkbox" name="sorting" value="hot" checked style="cursor: pointer; margin-right: 6px;"> hot
                        <small style="color: #737373; margin-left: 8px;">(Popular/trending posts)</small>
                    </label>
                    <label style="color: #e5e5e5; font-weight: normal; cursor: pointer;">
                        <input type="checkbox" name="sorting" value="rising" checked style="cursor: pointer; margin-right: 6px;"> rising
                        <small style="color: #737373; margin-left: 8px;">(Early trending detection)</small>
                    </label>
                    <label style="color: #e5e5e5; font-weight: normal; cursor: pointer;">
                        <input type="checkbox" name="sorting" value="top" style="cursor: pointer; margin-right: 6px;"> top
                        <small style="color: #737373; margin-left: 8px;">(Top posts from today)</small>
                    </label>
                    <label style="color: #e5e5e5; font-weight: normal; cursor: pointer;">
                        <input type="checkbox" name="sorting" value="controversial" style="cursor: pointer; margin-right: 6px;"> controversial
                        <small style="color: #737373; margin-left: 8px;">(Divisive content)</small>
                    </label>
                </div>
            </div>
            
            <h3>Reddit Account Selection</h3>
            <div class="form-row">
                <label>Account Type:</label>
                <select id="account_type" onchange="toggleAccountType()">
                    <option value="saved">Use Saved Account</option>
                    <option value="manual">Enter Credentials Manually</option>
                </select>
            </div>
            
            <!-- Saved Account Selection -->
            <div id="saved_account_section">
                <div class="form-row">
                    <label>Saved Account:</label>
                    <select id="saved_account_name">
                        <option value="">Select an account...</option>
                    </select>
                    <button onclick="loadSavedAccounts()" class="stats">Refresh</button>
                    <button onclick="showAccountManager()" class="stats">Manage Accounts</button>
                </div>
            </div>
            
            <!-- Manual Credentials -->
            <div id="manual_credentials_section" style="display: none;">
                <div class="credentials-section">
                    <div class="form-row">
                        <label>Client ID:</label>
                        <input type="text" id="client_id" placeholder="Your Reddit app client ID" />
                    </div>
                    <div class="form-row">
                        <label>Client Secret:</label>
                        <input type="password" id="client_secret" placeholder="Your Reddit app client secret" />
                    </div>
                    <div class="form-row">
                        <label>Username:</label>
                        <input type="text" id="username" placeholder="Your Reddit username" />
                    </div>
                    <div class="form-row">
                        <label>Password:</label>
                        <input type="password" id="password" placeholder="Your Reddit password" />
                    </div>
                    <div class="form-row">
                        <label>User Agent:</label>
                        <input type="text" id="user_agent" placeholder="RedditScraper/1.0 by YourUsername" />
                    </div>
                    <div class="form-row">
                        <label>Save as:</label>
                        <input type="text" id="save_account_as" placeholder="Account name (optional)" />
                        <small>Save these credentials for future use</small>
                    </div>
                    <p><small>Get credentials at <a href="https://www.reddit.com/prefs/apps" target="_blank">https://www.reddit.com/prefs/apps</a></small></p>
                </div>
            </div>
            
            <br>
            <button onclick="startScraper()" class="start" id="startScraperBtn">Start Scraper</button>
        </div>
        
        <!-- Account Manager Modal -->
        <div id="accountManagerModal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.5); z-index: 1001;">
            <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: white; padding: 30px; border-radius: 10px; width: 80%; max-width: 600px; max-height: 80%; overflow-y: auto;">
                <h2>Account Manager</h2>

                <h3>Add New Account</h3>
                <div class="credentials-section">
                    <div class="form-row">
                        <label>Account Name:</label>
                        <input type="text" id="new_account_name" placeholder="Give this account a name" />
                    </div>
                    <div class="form-row">
                        <label>Client ID:</label>
                        <input type="text" id="new_client_id" placeholder="Your Reddit app client ID" />
                    </div>
                    <div class="form-row">
                        <label>Client Secret:</label>
                        <input type="password" id="new_client_secret" placeholder="Your Reddit app client secret" />
                    </div>
                    <div class="form-row">
                        <label>Username:</label>
                        <input type="text" id="new_username" placeholder="Your Reddit username" />
                    </div>
                    <div class="form-row">
                        <label>Password:</label>
                        <input type="password" id="new_password" placeholder="Your Reddit password" />
                    </div>
                    <div class="form-row">
                        <label>User Agent:</label>
                        <input type="text" id="new_user_agent" placeholder="RedditScraper/1.0 by YourUsername" />
                    </div>
                    <button onclick="saveNewAccount()" class="start">Save Account</button>
                </div>
                
                <h3>Saved Accounts</h3>
                <div id="savedAccountsList"></div>
                
                <div style="text-align: center; margin-top: 20px;">
                    <button onclick="hideAccountManager()" class="stop">Close</button>
                </div>
            </div>
        </div>
        
        <script>
            const presets = {
                high: {
                    posts_limit: 150,
                    interval: 60,
                    comment_batch: 8,
                    sorting_methods: ['top', 'rising']
                },
                medium: {
                    posts_limit: 100,
                    interval: 60,
                    comment_batch: 6,
                    sorting_methods: ['top', 'rising']
                },
                low: {
                    posts_limit: 80,
                    interval: 60,
                    comment_batch: 4,
                    sorting_methods: ['top', 'rising']
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
                    healthDiv.innerHTML = `
                        <div style="padding: 15px; border-radius: 6px; margin: 10px 0;">
                            <h3>System Health</h3>
                            <p><strong>Total Scrapers:</strong> ${health.total_scrapers} |
                               <strong>Running:</strong> ${health.running_containers} |
                               <strong>Failed:</strong> ${health.failed_scrapers}</p>
                            <p><strong>Database:</strong> ${health.database_connected ? 'Connected' : 'Disconnected'} |
                               <strong>Docker:</strong> ${health.docker_available ? 'Available' : 'Not Available'}</p>
                        </div>
                    `;
                } catch (error) {
                    console.error('Error loading health status:', error);
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
                try {
                    const response = await fetch('/scrapers');
                    const scrapers = await response.json();
                    const container = document.getElementById('scrapers');
                    const scraperCount = Object.keys(scrapers).length;

                    container.innerHTML = `
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                            <h2 style="margin: 0;">Active Scrapers (${scraperCount})</h2>
                            ${scraperCount > 0 ? `
                                <div>
                                    <button onclick="expandAllScrapers()" class="stats" style="padding: 8px 14px; font-size: 12px;">Expand All</button>
                                    <button onclick="collapseAllScrapers()" class="stats" style="padding: 8px 14px; font-size: 12px;">Collapse All</button>
                                </div>
                            ` : ''}
                        </div>
                    `;

                    if (scraperCount === 0) {
                        container.innerHTML += '<p>No active scrapers. Start one above!</p>';
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

                        const div = document.createElement('div');
                        div.className = `scraper ${statusClass}`;
                        div.innerHTML = `
                            <div class="scraper-header" onclick="toggleScraper(this)">
                                <div class="scraper-title">
                                    <h3>r/${subreddit}</h3>
                                    <span class="status-badge ${badgeClass}">${info.status?.toUpperCase() || 'UNKNOWN'}</span>
                                </div>
                                <div class="scraper-summary">
                                    <div class="scraper-stat">
                                        <span style="color: #22c55e;">📊 ${totalPosts}</span>
                                        <span>posts</span>
                                    </div>
                                    <div class="scraper-stat">
                                        <span style="color: #3b82f6;">${totalComments}</span>
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
                                    <p><strong>Reddit User:</strong> ${info.config?.credentials?.username || 'N/A'}</p>
                                    <p><strong>Container:</strong> ${info.container_name || 'N/A'}</p>
                                    <p><strong>Config:</strong> ${info.config?.posts_limit || 'N/A'} posts, ${info.config?.interval || 'N/A'}s interval, ${info.config?.comment_batch || 'N/A'} batch</p>

                                    <div style="margin-top: 12px; padding: 10px; background: #0d0d0d; border-radius: 4px;">
                                        <strong>📊 Database Totals:</strong><br>
                                        <span style="color: #22c55e;">▸ ${totalPosts} posts</span> |
                                        <span style="color: #3b82f6;">▸ ${totalComments} comments</span>
                                        ${info.metrics ? `
                                        <br><small style="color: #737373;">Scraper collected: ${(info.metrics.total_posts_collected || 0).toLocaleString()} posts (${(info.metrics.posts_per_hour || 0).toFixed(1)}/hr), ${(info.metrics.total_comments_collected || 0).toLocaleString()} comments (${(info.metrics.comments_per_hour || 0).toFixed(1)}/hr)</small>
                                        <br><small style="color: #737373;">
                                            Last cycle: ${info.metrics.last_cycle_posts || 0} posts, ${info.metrics.last_cycle_comments || 0} comments
                                            ${info.metrics.last_cycle_time ? `at ${new Date(info.metrics.last_cycle_time).toLocaleTimeString()}` : ''}
                                            ${info.metrics.total_cycles ? ` • ${info.metrics.total_cycles} cycles` : ''}
                                        </small>
                                        ` : ''}
                                    </div>

                                    <p><strong>Restarts:</strong> ${restartCount} | <strong>Auto-restart:</strong>
                                       <label class="toggle">
                                           <input type="checkbox" ${autoRestart ? 'checked' : ''} onchange="toggleAutoRestart('${subreddit}', this.checked)">
                                           <span class="slider"></span>
                                       </label>
                                    </p>
                                    ${info.started_at ? `<p><strong>Started:</strong> ${new Date(info.started_at).toLocaleString()}</p>` : ''}
                                    ${info.last_updated ? `<p><strong>Last Updated:</strong> ${new Date(info.last_updated).toLocaleString()}</p>` : ''}
                                    ${info.last_error ? `<p><strong>Error:</strong> ${info.last_error}</p>` : ''}

                                    <div style="margin-top: 16px;">
                                        <button onclick="event.stopPropagation(); stopScraper(this, '${subreddit}')" class="stop">Stop</button>
                                        <button onclick="event.stopPropagation(); restartScraper(this, '${subreddit}')" class="restart">Restart</button>
                                        <button onclick="event.stopPropagation(); getStats(this, '${subreddit}')" class="stats">Stats</button>
                                        <button onclick="event.stopPropagation(); getLogs(this, '${subreddit}')" class="stats">Logs</button>
                                        <button onclick="event.stopPropagation(); deleteScraper(this, '${subreddit}')" class="delete">Delete</button>
                                    </div>
                                </div>
                            </div>
                        `;
                        container.appendChild(div);
                    });
                } catch (error) {
                    console.error('Error loading scrapers:', error);
                }
            }
            
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
                
                try {
                    const response = await fetch(`/accounts?account_name=${encodeURIComponent(accountName)}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(credentials)
                    });
                    
                    if (response.ok) {
                        alert('Account saved successfully!');
                        loadAccountsInManager();
                        loadSavedAccounts();
                        // Clear form
                        ['new_account_name', 'new_client_id', 'new_client_secret', 'new_username', 'new_password', 'new_user_agent'].forEach(id => {
                            document.getElementById(id).value = '';
                        });
                    } else {
                        const error = await response.json();
                        alert('Error saving account: ' + error.detail);
                    }
                } catch (error) {
                    alert('Error saving account: ' + error.message);
                }
            }
            
            async function loadAccountsInManager() {
                try {
                    const response = await fetch('/accounts');
                    const accounts = await response.json();
                    const container = document.getElementById('savedAccountsList');
                    
                    if (Object.keys(accounts).length === 0) {
                        container.innerHTML = '<p>No saved accounts yet.</p>';
                        return;
                    }
                    
                    container.innerHTML = '';
                    Object.entries(accounts).forEach(([accountName, account]) => {
                        const div = document.createElement('div');
                        div.style.cssText = 'border: 1px solid #ddd; padding: 10px; margin: 5px 0; border-radius: 3px; display: flex; justify-content: space-between; align-items: center;';
                        div.innerHTML = `
                            <div>
                                <strong>${accountName}</strong><br>
                                <small>User: ${account.username} | Created: ${new Date(account.created_at).toLocaleDateString()}</small>
                            </div>
                            <button onclick="deleteAccount('${accountName}')" class="delete" style="padding: 5px 10px;">Delete</button>
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
                        subreddit: document.getElementById('subreddit').value,
                        posts_limit: parseInt(document.getElementById('posts_limit').value),
                        interval: parseInt(document.getElementById('interval').value),
                        comment_batch: parseInt(document.getElementById('comment_batch').value),
                        sorting_methods: sortingMethods,
                        auto_restart: document.getElementById('auto_restart').checked
                    };

                    if (!requestData.subreddit) {
                        alert('Please enter a subreddit name');
                        setButtonLoading(button, false);
                        return;
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
            

            
            // Load scrapers, health, and accounts on page load and refresh every 15 seconds
            loadScrapers();
            loadHealthStatus();
            loadSavedAccounts();
            setInterval(() => {
                loadScrapers();
                loadHealthStatus();
            }, 15000);
        </script>
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

            # Query actual database totals (persist across scraper recreations)
            db_total_posts = posts_collection.count_documents({"subreddit": subreddit})
            db_total_comments = comments_collection.count_documents({"subreddit": subreddit})

            result[subreddit] = {
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
                "last_error": scraper_doc.get("last_error"),
                "container_id": scraper_doc.get("container_id"),
                "container_name": scraper_doc.get("container_name"),
                "restart_count": scraper_doc.get("restart_count", 0)
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
    """Start a new scraper using either saved account or manual credentials"""
    
    # Determine which credentials to use
    if request.saved_account_name:
        # Use saved account
        credentials = get_reddit_account(request.saved_account_name)
        if not credentials:
            raise HTTPException(status_code=404, detail=f"Saved account '{request.saved_account_name}' not found")
        logger.info(f"Using saved account '{request.saved_account_name}' for r/{request.subreddit}")
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
    
    # Create scraper config
    config = ScraperConfig(
        subreddit=request.subreddit,
        posts_limit=request.posts_limit,
        interval=request.interval,
        comment_batch=request.comment_batch,
        sorting_methods=request.sorting_methods,
        credentials=credentials,
        auto_restart=request.auto_restart
    )
    
    # Check if scraper already exists
    existing_scraper = load_scraper_from_db(config.subreddit)
    if existing_scraper:
        if existing_scraper["container_name"]:
            container_status = check_container_status(existing_scraper["container_name"])
            if container_status == "running":
                raise HTTPException(status_code=400, detail="Scraper already running for this subreddit")
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
    
    return {
        "message": f"Scraper started for r/{config.subreddit}",
        "reddit_user": credentials.username,
        "posts_limit": config.posts_limit,
        "interval": config.interval,
        "comment_batch": config.comment_batch,
        "container_name": f"{DOCKER_CONFIG['container_prefix']}{config.subreddit}",
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
async def stop_scraper(subreddit: str):
    """Stop a running scraper container"""
    
    # Load scraper from database
    scraper_data = load_scraper_from_db(subreddit)
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")
    
    container_name = scraper_data.get("container_name")
    if container_name:
        try:
            # Stop the Docker container
            result = subprocess.run([
                "docker", "stop", container_name
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                update_scraper_status(subreddit, "stopped")
                if subreddit in active_scrapers:
                    active_scrapers[subreddit]["status"] = "stopped"
                logger.info(f"Stopped container {container_name} for r/{subreddit}")
            else:
                # Try force kill if stop didn't work
                subprocess.run([
                    "docker", "kill", container_name
                ], capture_output=True, text=True)
                update_scraper_status(subreddit, "stopped")
                if subreddit in active_scrapers:
                    active_scrapers[subreddit]["status"] = "stopped"
                logger.info(f"Force killed container {container_name} for r/{subreddit}")
                
        except subprocess.TimeoutExpired:
            # Force kill if timeout
            subprocess.run([
                "docker", "kill", container_name
            ], capture_output=True, text=True)
            update_scraper_status(subreddit, "stopped")
            if subreddit in active_scrapers:
                active_scrapers[subreddit]["status"] = "stopped"
            logger.info(f"Timeout - force killed container {container_name} for r/{subreddit}")
        except Exception as e:
            update_scraper_status(subreddit, "error", last_error=f"Error stopping container: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Error stopping container: {str(e)}")
    
    return {"message": f"Scraper stopped for r/{subreddit}"}

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
async def get_scraper_status(subreddit: str):
    """Get detailed status of a specific scraper"""
    scraper_data = load_scraper_from_db(subreddit)
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
async def get_scraper_logs(subreddit: str, lines: int = 100):
    """Get recent logs from a scraper container"""
    scraper_data = load_scraper_from_db(subreddit)
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
async def restart_scraper_endpoint(subreddit: str, background_tasks: BackgroundTasks):
    """Manually restart a scraper"""
    scraper_data = load_scraper_from_db(subreddit)
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")
    
    # Stop and remove existing container first
    container_name = scraper_data.get("container_name")
    if container_name:
        cleanup_container(container_name)
    
    # Start new container
    background_tasks.add_task(restart_scraper, scraper_data["config"], subreddit)
    
    return {"message": f"Restarting scraper for r/{subreddit}"}

@app.put("/scrapers/{subreddit}/auto-restart")
async def toggle_auto_restart(subreddit: str, auto_restart: bool):
    """Toggle auto-restart setting for a scraper"""
    scraper_data = load_scraper_from_db(subreddit)
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")
    
    # Update auto-restart setting in database
    result = scrapers_collection.update_one(
        {"subreddit": subreddit},
        {"$set": {"auto_restart": auto_restart, "last_updated": datetime.now(UTC)}}
    )
    
    if result.modified_count > 0:
        return {"message": f"Auto-restart {'enabled' if auto_restart else 'disabled'} for r/{subreddit}"}
    else:
        raise HTTPException(status_code=500, detail="Failed to update auto-restart setting")

@app.delete("/scrapers/{subreddit}")
async def remove_scraper(subreddit: str):
    """Remove a scraper completely (stop it first if running)"""
    scraper_data = load_scraper_from_db(subreddit)
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")
    
    # Stop and remove container if it exists
    container_name = scraper_data.get("container_name")
    if container_name:
        cleanup_container(container_name)
        logger.info(f"Cleaned up container {container_name} for r/{subreddit}")
    
    # Remove from database
    result = scrapers_collection.delete_one({"subreddit": subreddit})
    
    # Remove from memory cache
    if subreddit in active_scrapers:
        del active_scrapers[subreddit]
    
    if result.deleted_count > 0:
        return {"message": f"Scraper removed for r/{subreddit}"}
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

@app.get("/presets")
async def get_presets():
    """Get predefined configuration presets (optimized for 5 scrapers per account)"""
    return {
        "high_activity": {
            "description": "For very active subreddits (wallstreetbets, stocks)",
            "posts_limit": 150,
            "interval": 60,
            "comment_batch": 8,
            "sorting_methods": ["top", "rising"]
        },
        "medium_activity": {
            "description": "For moderately active subreddits (investing, cryptocurrency)",
            "posts_limit": 100,
            "interval": 60,
            "comment_batch": 6,
            "sorting_methods": ["top", "rising"]
        },
        "low_activity": {
            "description": "For smaller subreddits (pennystocks, niche topics)",
            "posts_limit": 80,
            "interval": 60,
            "comment_batch": 4,
            "sorting_methods": ["top", "rising"]
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
async def save_account(account_name: str, credentials: RedditCredentials):
    """Save Reddit credentials for reuse"""
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
    
    success = save_reddit_account(account_name.strip(), credentials)
    if success:
        return {"message": f"Account '{account_name}' saved successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to save account")

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=API_CONFIG["host"], port=API_CONFIG["port"])