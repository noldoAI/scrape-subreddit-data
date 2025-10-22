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
        
        # Encrypt sensitive credentials for update
        update_data = {
            "account_name": account_name,
            "client_id": encrypt_credential(credentials.client_id),
            "client_secret": encrypt_credential(credentials.client_secret),
            "username": credentials.username,  # Keep username unencrypted for display
            "password": encrypt_credential(credentials.password),
            "user_agent": credentials.user_agent,  # Keep user agent unencrypted
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
    """Get decrypted Reddit credentials for an account"""
    try:
        if not mongo_connected:
            logger.warning("Database not connected, cannot get account")
            return None
        
        accounts_collection = get_accounts_collection()
        account_doc = accounts_collection.find_one({"account_name": account_name})
        
        if not account_doc:
            return None
        
        # Decrypt credentials
        return RedditCredentials(
            client_id=decrypt_credential(account_doc["client_id"]),
            client_secret=decrypt_credential(account_doc["client_secret"]),
            username=account_doc["username"],
            password=decrypt_credential(account_doc["password"]),
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
    """Save scraper configuration to database with encrypted credentials"""
    try:
        # Encrypt sensitive credentials
        encrypted_credentials = {
            "client_id": encrypt_credential(config.credentials.client_id),
            "client_secret": encrypt_credential(config.credentials.client_secret),
            "username": config.credentials.username,  # Keep username unencrypted for display
            "password": encrypt_credential(config.credentials.password),
            "user_agent": config.credentials.user_agent  # Keep user agent unencrypted
        }
        
        scraper_doc = {
            "subreddit": subreddit,
            "status": status,
            "container_id": container_id,
            "container_name": container_name,
            "config": {
                "posts_limit": config.posts_limit,
                "interval": config.interval,
                "comment_batch": config.comment_batch
            },
            "credentials": encrypted_credentials,
            "auto_restart": config.auto_restart,
            "last_updated": datetime.now(UTC),
            "last_error": last_error,
            "restart_count": 0
        }
        
        # Upsert scraper document - only set created_at on insert
        scrapers_collection.update_one(
            {"subreddit": subreddit},
            {
                "$set": scraper_doc,
                "$setOnInsert": {"created_at": datetime.now(UTC)}
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
        
        # Decrypt credentials
        decrypted_credentials = RedditCredentials(
            client_id=decrypt_credential(scraper_doc["credentials"]["client_id"]),
            client_secret=decrypt_credential(scraper_doc["credentials"]["client_secret"]),
            username=scraper_doc["credentials"]["username"],
            password=decrypt_credential(scraper_doc["credentials"]["password"]),
            user_agent=scraper_doc["credentials"]["user_agent"]
        )
        
        # Reconstruct ScraperConfig
        config = ScraperConfig(
            subreddit=subreddit,
            posts_limit=scraper_doc["config"]["posts_limit"],
            interval=scraper_doc["config"]["interval"],
            comment_batch=scraper_doc["config"]["comment_batch"],
            credentials=decrypted_credentials,
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
            "--comment-batch", str(config.comment_batch)
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
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
                background-attachment: fixed;
                color: #e0e0e0;
                padding: 40px;
                min-height: 100vh;
            }
            h1 {
                font-size: 2.5em;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                margin-bottom: 10px;
                text-shadow: 0 0 30px rgba(102, 126, 234, 0.5);
            }
            h2 {
                color: #a78bfa;
                margin: 30px 0 15px 0;
                font-size: 1.8em;
            }
            h3 {
                color: #c4b5fd;
                margin: 15px 0 10px 0;
            }
            p { line-height: 1.6; margin: 10px 0; }
            a { color: #818cf8; text-decoration: none; }
            a:hover { color: #a78bfa; }

            .scraper {
                background: rgba(30, 30, 50, 0.6);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(167, 139, 250, 0.2);
                padding: 25px;
                margin: 15px 0;
                border-radius: 15px;
                box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
                transition: all 0.3s ease;
            }
            .scraper:hover {
                border-color: rgba(167, 139, 250, 0.5);
                box-shadow: 0 8px 32px 0 rgba(102, 126, 234, 0.5);
                transform: translateY(-2px);
            }
            .running {
                background: rgba(16, 185, 129, 0.1);
                border-left: 4px solid #10b981;
            }
            .stopped {
                background: rgba(107, 114, 128, 0.1);
                border-left: 4px solid #6b7280;
            }
            .error {
                background: rgba(239, 68, 68, 0.1);
                border-left: 4px solid #ef4444;
            }
            .failed {
                background: rgba(244, 63, 94, 0.1);
                border-left: 4px solid #f43f5e;
            }

            button {
                padding: 12px 24px;
                margin: 5px;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                position: relative;
                transition: all 0.3s ease;
                font-weight: 600;
                font-size: 14px;
                box-shadow: 0 4px 15px 0 rgba(0, 0, 0, 0.3);
            }
            button:hover {
                transform: translateY(-2px);
                box-shadow: 0 6px 20px 0 rgba(0, 0, 0, 0.4);
            }
            button:active { transform: translateY(0px); }
            button:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

            .start {
                background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                color: white;
            }
            .start:hover { background: linear-gradient(135deg, #059669 0%, #047857 100%); }

            .stop {
                background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
                color: white;
            }
            .stop:hover { background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%); }

            .restart {
                background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
                color: white;
            }
            .restart:hover { background: linear-gradient(135deg, #d97706 0%, #b45309 100%); }

            .delete {
                background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%);
                color: white;
            }
            .delete:hover { background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%); }

            .stats {
                background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
                color: white;
            }
            .stats:hover { background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%); }

            .loading { background: linear-gradient(135deg, #6b7280 0%, #4b5563 100%) !important; }

            .spinner {
                display: inline-block;
                width: 16px;
                height: 16px;
                border: 2px solid rgba(255, 255, 255, 0.3);
                border-radius: 50%;
                border-top-color: #ffffff;
                animation: spin 1s ease-in-out infinite;
                margin-right: 8px;
            }
            @keyframes spin { to { transform: rotate(360deg); } }

            input, select, textarea {
                padding: 10px;
                margin: 5px;
                border: 1px solid rgba(167, 139, 250, 0.3);
                border-radius: 8px;
                background: rgba(30, 30, 50, 0.5);
                color: #e0e0e0;
                transition: all 0.3s ease;
            }
            input:focus, select:focus, textarea:focus {
                outline: none;
                border-color: #818cf8;
                box-shadow: 0 0 0 3px rgba(129, 140, 248, 0.1);
            }
            input::placeholder { color: #6b7280; }

            .credentials-section {
                background: rgba(30, 30, 50, 0.4);
                backdrop-filter: blur(10px);
                padding: 20px;
                border-radius: 10px;
                margin: 15px 0;
                border: 1px solid rgba(167, 139, 250, 0.2);
            }

            .form-row { margin: 10px 0; }
            .form-row label {
                display: inline-block;
                width: 150px;
                color: #c4b5fd;
                font-weight: 500;
            }
            .form-row small { color: #9ca3af; }

            .collapsible {
                cursor: pointer;
                background: rgba(167, 139, 250, 0.2);
                padding: 12px;
                border: none;
                text-align: left;
                width: 100%;
                border-radius: 8px;
                color: #e0e0e0;
                transition: all 0.3s ease;
            }
            .collapsible:hover { background: rgba(167, 139, 250, 0.3); }

            .content {
                display: none;
                padding: 15px;
                background: rgba(30, 30, 50, 0.3);
                border: 1px solid rgba(167, 139, 250, 0.2);
                border-radius: 8px;
                margin-top: 10px;
            }

            .toggle { position: relative; display: inline-block; width: 60px; height: 34px; }
            .toggle input { opacity: 0; width: 0; height: 0; }
            .slider {
                position: absolute;
                cursor: pointer;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background-color: #4b5563;
                transition: .4s;
                border-radius: 34px;
                box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.3);
            }
            .slider:before {
                position: absolute;
                content: "";
                height: 26px;
                width: 26px;
                left: 4px;
                bottom: 4px;
                background-color: white;
                transition: .4s;
                border-radius: 50%;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
            }
            input:checked + .slider {
                background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
            }
            input:checked + .slider:before { transform: translateX(26px); }

            .status-badge {
                padding: 6px 12px;
                border-radius: 20px;
                font-size: 11px;
                font-weight: bold;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
            }
            .badge-running {
                background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                color: white;
            }
            .badge-stopped {
                background: linear-gradient(135deg, #6b7280 0%, #4b5563 100%);
                color: white;
            }
            .badge-error {
                background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
                color: white;
            }
            .badge-failed {
                background: linear-gradient(135deg, #f43f5e 0%, #e11d48 100%);
                color: white;
            }

            .loading-overlay {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background-color: rgba(0, 0, 0, 0.7);
                backdrop-filter: blur(5px);
                z-index: 1000;
            }
            .loading-message {
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                background: rgba(30, 30, 50, 0.9);
                backdrop-filter: blur(10px);
                padding: 30px;
                border-radius: 15px;
                text-align: center;
                border: 1px solid rgba(167, 139, 250, 0.3);
                box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.5);
                color: #e0e0e0;
            }

            #health-status > div {
                background: rgba(30, 30, 50, 0.6) !important;
                backdrop-filter: blur(10px);
                border: 1px solid rgba(167, 139, 250, 0.3) !important;
                box-shadow: 0 4px 20px 0 rgba(31, 38, 135, 0.3);
            }

            /* Modal styling */
            #accountManagerModal > div {
                background: rgba(15, 12, 41, 0.95) !important;
                backdrop-filter: blur(20px);
                border: 1px solid rgba(167, 139, 250, 0.3);
                box-shadow: 0 20px 60px 0 rgba(0, 0, 0, 0.5);
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
        
        <h1>🤖 Reddit Scraper Management Dashboard</h1>
        <p><strong>✨ Features:</strong> Persistent storage, unique credentials per scraper, automatic restart on failure</p>
        
        <div id="health-status"></div>
        
        <div id="scrapers"></div>
        
        <h2>🚀 Start New Scraper</h2>
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
                <input type="number" id="interval" value="300" />
                
                <label>Comment Batch:</label>
                <input type="number" id="comment_batch" value="20" />
                
                <label>Auto-restart:</label>
                <label class="toggle">
                    <input type="checkbox" id="auto_restart" checked>
                    <span class="slider"></span>
                </label>
            </div>
            
            <h3>👤 Reddit Account Selection</h3>
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
                    <button onclick="loadSavedAccounts()" class="stats">🔄 Refresh</button>
                    <button onclick="showAccountManager()" class="stats">⚙️ Manage Accounts</button>
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
                    <p><small>💡 Get credentials at <a href="https://www.reddit.com/prefs/apps" target="_blank">https://www.reddit.com/prefs/apps</a></small></p>
                </div>
            </div>
            
            <br>
            <button onclick="startScraper()" class="start" id="startScraperBtn">🚀 Start Scraper</button>
        </div>
        
        <!-- Account Manager Modal -->
        <div id="accountManagerModal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.5); z-index: 1001;">
            <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: white; padding: 30px; border-radius: 10px; width: 80%; max-width: 600px; max-height: 80%; overflow-y: auto;">
                <h2>⚙️ Account Manager</h2>
                
                <h3>📝 Add New Account</h3>
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
                    <button onclick="saveNewAccount()" class="start">💾 Save Account</button>
                </div>
                
                <h3>📋 Saved Accounts</h3>
                <div id="savedAccountsList"></div>
                
                <div style="text-align: center; margin-top: 20px;">
                    <button onclick="hideAccountManager()" class="stop">❌ Close</button>
                </div>
            </div>
        </div>
        
        <script>
            const presets = {
                high: { posts_limit: 2000, interval: 180, comment_batch: 30 },
                medium: { posts_limit: 1000, interval: 300, comment_batch: 20 },
                low: { posts_limit: 500, interval: 600, comment_batch: 10 }
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
            
            // Make credentials section collapsible
            document.querySelector('.collapsible').onclick = function() {
                const content = this.nextElementSibling;
                content.style.display = content.style.display === 'block' ? 'none' : 'block';
            };
            
            document.getElementById('preset').onchange = function() {
                const preset = presets[this.value];
                if (preset) {
                    document.getElementById('posts_limit').value = preset.posts_limit;
                    document.getElementById('interval').value = preset.interval;
                    document.getElementById('comment_batch').value = preset.comment_batch;
                }
            };
            
            async function loadHealthStatus() {
                try {
                    const response = await fetch('/health');
                    const health = await response.json();
                    const healthDiv = document.getElementById('health-status');
                    healthDiv.innerHTML = `
                        <div style="background: #e3f2fd; padding: 15px; border-radius: 5px; margin: 10px 0;">
                            <h3>📊 System Health</h3>
                            <p><strong>Total Scrapers:</strong> ${health.total_scrapers} | 
                               <strong>Running:</strong> ${health.running_containers} | 
                               <strong>Failed:</strong> ${health.failed_scrapers}</p>
                            <p><strong>Database:</strong> ${health.database_connected ? '✅ Connected' : '❌ Disconnected'} | 
                               <strong>Docker:</strong> ${health.docker_available ? '✅ Available' : '❌ Not Available'}</p>
                        </div>
                    `;
                } catch (error) {
                    console.error('Error loading health status:', error);
                }
            }
            
            async function loadScrapers() {
                try {
                    const response = await fetch('/scrapers');
                    const scrapers = await response.json();
                    const container = document.getElementById('scrapers');
                    container.innerHTML = '<h2>📋 Active Scrapers</h2>';
                    
                    if (Object.keys(scrapers).length === 0) {
                        container.innerHTML += '<p>No active scrapers. Start one above!</p>';
                        return;
                    }
                    
                    Object.entries(scrapers).forEach(([subreddit, info]) => {
                        const statusClass = info.status || 'stopped';
                        const badgeClass = `badge-${statusClass}`;
                        const restartCount = info.restart_count || 0;
                        const autoRestart = info.config?.auto_restart !== false;
                        
                        const div = document.createElement('div');
                        div.className = `scraper ${statusClass}`;
                        div.innerHTML = `
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <h3>r/${subreddit}</h3>
                                <span class="status-badge ${badgeClass}">${info.status?.toUpperCase() || 'UNKNOWN'}</span>
                            </div>
                            <p><strong>Reddit User:</strong> ${info.config?.credentials?.username || 'N/A'}</p>
                            <p><strong>Container:</strong> ${info.container_name || 'N/A'}</p>
                            <p><strong>Config:</strong> ${info.config?.posts_limit || 'N/A'} posts, ${info.config?.interval || 'N/A'}s interval, ${info.config?.comment_batch || 'N/A'} batch</p>
                            <p><strong>Restarts:</strong> ${restartCount} | <strong>Auto-restart:</strong> 
                               <label class="toggle">
                                   <input type="checkbox" ${autoRestart ? 'checked' : ''} onchange="toggleAutoRestart('${subreddit}', this.checked)">
                                   <span class="slider"></span>
                               </label>
                            </p>
                            ${info.started_at ? `<p><strong>Started:</strong> ${new Date(info.started_at).toLocaleString()}</p>` : ''}
                            ${info.last_updated ? `<p><strong>Last Updated:</strong> ${new Date(info.last_updated).toLocaleString()}</p>` : ''}
                            ${info.last_error ? `<p><strong>Error:</strong> ${info.last_error}</p>` : ''}
                            <div>
                                <button onclick="stopScraper(this, '${subreddit}')" class="stop">⏹️ Stop</button>
                                <button onclick="restartScraper(this, '${subreddit}')" class="restart">🔄 Restart</button>
                                <button onclick="getStats(this, '${subreddit}')" class="stats">📊 Stats</button>
                                <button onclick="getLogs(this, '${subreddit}')" class="stats">📋 Logs</button>
                                <button onclick="deleteScraper(this, '${subreddit}')" class="delete">🗑️ Delete</button>
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
                            <button onclick="deleteAccount('${accountName}')" class="delete" style="padding: 5px 10px;">🗑️ Delete</button>
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
                    let requestData = {
                        subreddit: document.getElementById('subreddit').value,
                        posts_limit: parseInt(document.getElementById('posts_limit').value),
                        interval: parseInt(document.getElementById('interval').value),
                        comment_batch: parseInt(document.getElementById('comment_batch').value),
                        auto_restart: document.getElementById('auto_restart').checked
                    };
                    
                    if (!requestData.subreddit) {
                        alert('Please enter a subreddit name');
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
📊 Posts: ${stats.total_posts.toLocaleString()}
💬 Comments: ${stats.total_comments.toLocaleString()}
✅ Initial Completion: ${stats.initial_completion_rate.toFixed(1)}%
🏢 Metadata: ${stats.subreddit_metadata_exists ? '✓' : '✗'}
⏰ Last Updated: ${stats.subreddit_last_updated ? new Date(stats.subreddit_last_updated).toLocaleString() : 'Never'}
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
            
            result[subreddit] = {
                "status": container_status,
                "started_at": scraper_doc.get("created_at"),
                "last_updated": scraper_doc.get("last_updated"),
                "config": {
                    "posts_limit": scraper_doc["config"]["posts_limit"],
                    "interval": scraper_doc["config"]["interval"],
                    "comment_batch": scraper_doc["config"]["comment_batch"],
                    "credentials": safe_credentials,
                    "auto_restart": scraper_doc.get("auto_restart", True)
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
async def get_scraper_stats(subreddit: str):
    """Get statistics for a specific subreddit"""
    if not mongo_connected:
        raise HTTPException(status_code=500, detail="Database not connected")
    
    try:
        # Get statistics from database
        total_posts = posts_collection.count_documents({"subreddit": subreddit})
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
        total_comments = comments_collection.count_documents({"subreddit": subreddit})
        
        # Check if subreddit metadata exists
        subreddit_metadata = subreddit_collection.find_one({"subreddit_name": subreddit})
        
        return {
            "subreddit": subreddit,
            "total_posts": total_posts,
            "posts_with_initial_comments": posts_with_initial_comments,
            "posts_without_initial_comments": posts_without_initial_comments,
            "total_comments": total_comments,
            "initial_completion_rate": (posts_with_initial_comments / total_posts * 100) if total_posts > 0 else 0,
            "subreddit_metadata_exists": subreddit_metadata is not None,
            "subreddit_last_updated": subreddit_metadata.get("last_updated") if subreddit_metadata else None
        }
    except Exception as e:
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
    """Get predefined configuration presets"""
    return {
        "high_activity": {
            "description": "For very active subreddits (wallstreetbets, stocks)",
            "posts_limit": 2000,
            "interval": 180,
            "comment_batch": 30
        },
        "medium_activity": {
            "description": "For moderately active subreddits (investing, cryptocurrency)",
            "posts_limit": 1000,
            "interval": 300,
            "comment_batch": 20
        },
        "low_activity": {
            "description": "For smaller subreddits (pennystocks, niche topics)",
            "posts_limit": 500,
            "interval": 600,
            "comment_batch": 10
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