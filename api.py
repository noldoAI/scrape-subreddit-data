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

# Load environment variables (fallback defaults)
load_dotenv()

app = FastAPI(
    title="Reddit Scraper API",
    description="Manage multiple Reddit scrapers with unique credentials",
    version="1.0.0"
)

# MongoDB connection for stats and scraper storage
try:
    client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
    db = client["seeky_testing"]
    posts_collection = db["reddit_posts"]
    comments_collection = db["reddit_comments"]
    subreddit_collection = db["subreddit_metadata"]
    scrapers_collection = db["reddit_scrapers"]  # New collection for scraper persistence
    mongo_connected = True
except:
    mongo_connected = False

# Global storage for active scrapers (for quick access, backed by database)
active_scrapers: Dict[str, dict] = {}

# Encryption key for credentials (generate one if not exists)
def get_encryption_key():
    key_file = ".scraper_key"
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
    posts_limit: int = 1000
    interval: int = 300
    comment_batch: int = 20
    credentials: RedditCredentials
    mongodb_uri: Optional[str] = None  # Allow custom MongoDB per scraper
    auto_restart: bool = True  # Enable automatic restart on failure

class ScraperStatus(BaseModel):
    subreddit: str
    status: str  # "running", "stopped", "error", "failed"
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    config: Optional[ScraperConfig] = None
    last_error: Optional[str] = None

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
            "mongodb_uri": config.mongodb_uri,
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
        
        print(f"Saved scraper configuration for r/{subreddit} to database")
        return True
        
    except Exception as e:
        print(f"Error saving scraper to database: {e}")
        return False

def load_scraper_from_db(subreddit: str) -> Optional[dict]:
    """Load scraper configuration from database"""
    try:
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
            mongodb_uri=scraper_doc.get("mongodb_uri"),
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
        print(f"Error loading scraper from database: {e}")
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
        print(f"Error updating scraper status: {e}")
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
                    client_id="***",
                    client_secret="***",
                    username=scraper_data["config"].credentials.username,
                    password="***",
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
        
        print(f"Loaded {len(active_scrapers)} scrapers from database")
        
    except Exception as e:
        print(f"Error loading scrapers from database: {e}")

def check_for_failed_scrapers():
    """Background task to check for failed containers and restart if needed"""
    while True:
        try:
            # Get all scrapers that should be running
            running_scrapers = scrapers_collection.find({"status": "running", "auto_restart": True})
            
            for scraper_doc in running_scrapers:
                subreddit = scraper_doc["subreddit"]
                container_name = scraper_doc.get("container_name")
                
                if container_name:
                    # Check if container is actually running
                    container_status = check_container_status(container_name)
                    
                    if container_status != "running":
                        print(f"Detected failed container for r/{subreddit}, attempting restart...")
                        
                        # Load full config from database
                        scraper_data = load_scraper_from_db(subreddit)
                        if scraper_data and scraper_data["config"]:
                            # Update status to failed
                            update_scraper_status(subreddit, "failed", 
                                                last_error="Container stopped unexpectedly",
                                                increment_restart=True)
                            
                            # Attempt restart after a short delay to avoid rapid restarts
                            time.sleep(5)
                            restart_scraper(scraper_data["config"], subreddit)
            
            # Also check for scrapers that are marked as "stopped" but should be running
            stopped_scrapers = scrapers_collection.find({
                "status": {"$in": ["stopped", "failed"]}, 
                "auto_restart": True
            })
            
            for scraper_doc in stopped_scrapers:
                subreddit = scraper_doc["subreddit"]
                # Only restart if it's been stopped for more than 30 seconds to avoid rapid restarts
                last_updated = scraper_doc.get("last_updated")
                if last_updated:
                    time_since_update = (datetime.now(UTC) - last_updated).total_seconds()
                    if time_since_update > 30:
                        print(f"Auto-restarting stopped scraper for r/{subreddit}...")
                        
                        scraper_data = load_scraper_from_db(subreddit)
                        if scraper_data and scraper_data["config"]:
                            restart_scraper(scraper_data["config"], subreddit)
            
            # Sleep for 30 seconds before next check (more frequent monitoring)
            time.sleep(30)
            
        except Exception as e:
            print(f"Error in failed scraper check: {e}")
            time.sleep(60)

def restart_scraper(config: ScraperConfig, subreddit: str):
    """Restart a failed scraper"""
    try:
        print(f"Restarting scraper for r/{subreddit}")
        
        # Stop any existing container first
        container_name = f"reddit-scraper-{subreddit}"
        subprocess.run(["docker", "stop", container_name], capture_output=True, text=True)
        
        # Update status to restarting
        update_scraper_status(subreddit, "restarting")
        
        # Start new container
        run_scraper(config)
        
    except Exception as e:
        print(f"Error restarting scraper for r/{subreddit}: {e}")
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
        # Create unique container name
        container_name = f"reddit-scraper-{config.subreddit}"
        
        # Save to database first
        save_scraper_to_db(config.subreddit, config, "starting", container_name=container_name)
        
        # Prepare environment variables for the container
        env_vars = [
            f"R_CLIENT_ID={config.credentials.client_id}",
            f"R_CLIENT_SECRET={config.credentials.client_secret}",
            f"R_USERNAME={config.credentials.username}",
            f"R_PASSWORD={config.credentials.password}",
            f"R_USER_AGENT={config.credentials.user_agent}",
            f"MONGODB_URI={config.mongodb_uri or os.getenv('MONGODB_URI', '')}"
        ]
        
        # Build Docker command
        cmd = [
            "docker", "run",
            "--name", container_name,
            "--rm",  # Remove container when it stops
            "-d",    # Run in detached mode
        ]
        
        # Add environment variables
        for env_var in env_vars:
            cmd.extend(["-e", env_var])
        
        # Add the image and command
        cmd.extend([
            "reddit-scraper",  # Docker image name
            "python", "reddit_scraper.py", config.subreddit,
            "--posts-limit", str(config.posts_limit),
            "--interval", str(config.interval),
            "--comment-batch", str(config.comment_batch)
        ])
        
        # Remove any existing container with the same name
        subprocess.run([
            "docker", "stop", container_name
        ], capture_output=True, text=True)
        
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
            client_id="***",
            client_secret="***", 
            username=config.credentials.username,  # Keep username for identification
            password="***",
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
        
        print(f"Started container {container_name} ({container_id[:12]}) for r/{config.subreddit}")
        
    except Exception as e:
        error_msg = f"Error starting container for r/{config.subreddit}: {e}"
        print(error_msg)
        update_scraper_status(config.subreddit, "error", last_error=str(e))
        if config.subreddit in active_scrapers:
            active_scrapers[config.subreddit]["status"] = "error"
            active_scrapers[config.subreddit]["last_error"] = str(e)

def check_container_status(container_name):
    """Check if a Docker container is running"""
    try:
        result = subprocess.run([
            "docker", "inspect", container_name, "--format", "{{.State.Status}}"
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except:
        return None

def get_container_logs(container_name, lines=50):
    """Get recent logs from a Docker container"""
    try:
        result = subprocess.run([
            "docker", "logs", "--tail", str(lines), container_name
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            return result.stdout
        return None
    except:
        return None

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Enhanced web dashboard with credential input and persistent storage"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Reddit Scraper Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            .scraper { border: 1px solid #ddd; padding: 20px; margin: 10px 0; border-radius: 5px; }
            .running { background-color: #e8f5e8; }
            .stopped { background-color: #f5f5f5; }
            .error { background-color: #ffe8e8; }
            .failed { background-color: #ffcccc; }
            button { padding: 10px 20px; margin: 5px; border: none; border-radius: 3px; cursor: pointer; position: relative; transition: all 0.3s ease; }
            button:disabled { opacity: 0.6; cursor: not-allowed; }
            .start { background-color: #4CAF50; color: white; }
            .stop { background-color: #f44336; color: white; }
            .restart { background-color: #ff9800; color: white; }
            .delete { background-color: #9c27b0; color: white; }
            .stats { background-color: #2196F3; color: white; }
            .loading { background-color: #9E9E9E !important; }
            .spinner { 
                display: inline-block; 
                width: 16px; 
                height: 16px; 
                border: 2px solid #ffffff; 
                border-radius: 50%; 
                border-top-color: transparent; 
                animation: spin 1s ease-in-out infinite; 
                margin-right: 8px;
            }
            @keyframes spin { to { transform: rotate(360deg); } }
            input, select, textarea { padding: 8px; margin: 5px; border: 1px solid #ddd; border-radius: 3px; }
            .credentials-section { background: #f9f9f9; padding: 15px; border-radius: 5px; margin: 10px 0; }
            .form-row { margin: 10px 0; }
            .form-row label { display: inline-block; width: 150px; }
            .collapsible { cursor: pointer; background: #eee; padding: 10px; border: none; text-align: left; width: 100%; }
            .content { display: none; padding: 10px; background: white; border: 1px solid #ddd; }
            .toggle { position: relative; display: inline-block; width: 60px; height: 34px; }
            .toggle input { opacity: 0; width: 0; height: 0; }
            .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #ccc; transition: .4s; border-radius: 34px; }
            .slider:before { position: absolute; content: ""; height: 26px; width: 26px; left: 4px; bottom: 4px; background-color: white; transition: .4s; border-radius: 50%; }
            input:checked + .slider { background-color: #2196F3; }
            input:checked + .slider:before { transform: translateX(26px); }
            .status-badge { padding: 4px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; color: white; }
            .badge-running { background-color: #4CAF50; }
            .badge-stopped { background-color: #9E9E9E; }
            .badge-error { background-color: #f44336; }
            .badge-failed { background-color: #E91E63; }
            .loading-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.3); z-index: 1000; }
            .loading-message { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: white; padding: 20px; border-radius: 5px; text-align: center; }
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
            
            <button class="collapsible">📋 Reddit API Credentials (Required)</button>
            <div class="content credentials-section">
                <div class="form-row">
                    <label>Client ID:</label>
                    <input type="text" id="client_id" placeholder="Your Reddit app client ID" required />
                </div>
                <div class="form-row">
                    <label>Client Secret:</label>
                    <input type="password" id="client_secret" placeholder="Your Reddit app client secret" required />
                </div>
                <div class="form-row">
                    <label>Username:</label>
                    <input type="text" id="username" placeholder="Your Reddit username" required />
                </div>
                <div class="form-row">
                    <label>Password:</label>
                    <input type="password" id="password" placeholder="Your Reddit password" required />
                </div>
                <div class="form-row">
                    <label>User Agent:</label>
                    <input type="text" id="user_agent" placeholder="RedditScraper/1.0 by YourUsername" required />
                </div>
                <div class="form-row">
                    <label>MongoDB URI:</label>
                    <input type="text" id="mongodb_uri" placeholder="mongodb+srv://... (optional, uses default)" />
                </div>
                <p><small>💡 Get credentials at <a href="https://www.reddit.com/prefs/apps" target="_blank">https://www.reddit.com/prefs/apps</a></small></p>
            </div>
            
            <br>
            <button onclick="startScraper()" class="start" id="startScraperBtn">🚀 Start Scraper</button>
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
            
            async function startScraper() {
                const button = document.getElementById('startScraperBtn');
                
                // Validate required fields
                const requiredFields = ['subreddit', 'client_id', 'client_secret', 'username', 'password', 'user_agent'];
                for (const field of requiredFields) {
                    if (!document.getElementById(field).value) {
                        alert(`Please fill in ${field.replace('_', ' ')}`);
                        return;
                    }
                }
                
                setButtonLoading(button, true, 'Starting...');
                
                try {
                    const config = {
                        subreddit: document.getElementById('subreddit').value,
                        posts_limit: parseInt(document.getElementById('posts_limit').value),
                        interval: parseInt(document.getElementById('interval').value),
                        comment_batch: parseInt(document.getElementById('comment_batch').value),
                        auto_restart: document.getElementById('auto_restart').checked,
                        credentials: {
                            client_id: document.getElementById('client_id').value,
                            client_secret: document.getElementById('client_secret').value,
                            username: document.getElementById('username').value,
                            password: document.getElementById('password').value,
                            user_agent: document.getElementById('user_agent').value
                        },
                        mongodb_uri: document.getElementById('mongodb_uri').value || null
                    };
                    
                    const response = await fetch('/scrapers/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(config)
                    });
                    
                    if (response.ok) {
                        alert('Scraper started successfully!');
                        // Clear credentials for security
                        ['client_id', 'client_secret', 'password'].forEach(id => {
                            document.getElementById(id).value = '';
                        });
                        loadScrapers();
                        loadHealthStatus();
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
            
            // Load scrapers and health on page load and refresh every 15 seconds
            loadScrapers();
            loadHealthStatus();
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
            
            # Create safe credentials for display
            safe_credentials = {
                "client_id": "***",
                "client_secret": "***",
                "username": scraper_doc["credentials"]["username"],
                "password": "***",
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
        print(f"Error listing scrapers from database: {e}")
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

@app.post("/scrapers/start")
async def start_scraper(config: ScraperConfig, background_tasks: BackgroundTasks):
    """Start a new scraper for a subreddit with unique credentials"""
    
    # Check if scraper already exists in database
    existing_scraper = load_scraper_from_db(config.subreddit)
    if existing_scraper:
        # Check if it's actually running
        if existing_scraper["container_name"]:
            container_status = check_container_status(existing_scraper["container_name"])
            if container_status == "running":
                raise HTTPException(status_code=400, detail="Scraper already running for this subreddit")
        
        # If not running, we can restart with new config
        print(f"Updating existing scraper configuration for r/{config.subreddit}")
    
    # Validate required credentials are provided
    if not all([
        config.credentials.client_id,
        config.credentials.client_secret,
        config.credentials.username,
        config.credentials.password,
        config.credentials.user_agent
    ]):
        raise HTTPException(
            status_code=400, 
            detail="All Reddit API credentials are required (client_id, client_secret, username, password, user_agent)"
        )
    
    # Check if MongoDB URI is available (either in config or environment)
    mongodb_uri = config.mongodb_uri or os.getenv("MONGODB_URI")
    if not mongodb_uri:
        raise HTTPException(
            status_code=400,
            detail="MongoDB URI is required (either in config or MONGODB_URI environment variable)"
        )
    
    # Check if reddit-scraper Docker image exists
    try:
        result = subprocess.run([
            "docker", "images", "reddit-scraper", "--format", "{{.Repository}}"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0 or "reddit-scraper" not in result.stdout:
            raise HTTPException(
                status_code=500,
                detail="Docker image 'reddit-scraper' not found. Please run: docker build -f Dockerfile -t reddit-scraper ."
            )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Docker command timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking Docker image: {str(e)}")
    
    # Start scraper in background
    background_tasks.add_task(run_scraper, config)
    
    return {
        "message": f"Scraper started for r/{config.subreddit}",
        "reddit_user": config.credentials.username,
        "posts_limit": config.posts_limit,
        "interval": config.interval,
        "comment_batch": config.comment_batch,
        "container_name": f"reddit-scraper-{config.subreddit}",
        "auto_restart": config.auto_restart
    }

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
                print(f"Stopped container {container_name} for r/{subreddit}")
            else:
                # Try force kill if stop didn't work
                subprocess.run([
                    "docker", "kill", container_name
                ], capture_output=True, text=True)
                update_scraper_status(subreddit, "stopped")
                if subreddit in active_scrapers:
                    active_scrapers[subreddit]["status"] = "stopped"
                print(f"Force killed container {container_name} for r/{subreddit}")
                
        except subprocess.TimeoutExpired:
            # Force kill if timeout
            subprocess.run([
                "docker", "kill", container_name
            ], capture_output=True, text=True)
            update_scraper_status(subreddit, "stopped")
            if subreddit in active_scrapers:
                active_scrapers[subreddit]["status"] = "stopped"
            print(f"Timeout - force killed container {container_name} for r/{subreddit}")
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
    
    # Create safe credentials for display
    safe_credentials = {
        "client_id": "***",
        "client_secret": "***",
        "username": scraper_data["config"].credentials.username,
        "password": "***",
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
    if subreddit not in active_scrapers:
        raise HTTPException(status_code=404, detail="Scraper not found")
    
    scraper_info = active_scrapers[subreddit]
    if "container_name" not in scraper_info:
        raise HTTPException(status_code=400, detail="No container found for this scraper")
    
    logs = get_container_logs(scraper_info["container_name"], lines)
    if logs is None:
        raise HTTPException(status_code=404, detail="Container not found or no logs available")
    
    return {
        "subreddit": subreddit,
        "container_name": scraper_info["container_name"],
        "logs": logs,
        "lines_requested": lines
    }

@app.post("/scrapers/{subreddit}/restart")
async def restart_scraper_endpoint(subreddit: str, background_tasks: BackgroundTasks):
    """Manually restart a scraper"""
    scraper_data = load_scraper_from_db(subreddit)
    if not scraper_data:
        raise HTTPException(status_code=404, detail="Scraper not found")
    
    # Stop existing container first
    container_name = scraper_data.get("container_name")
    if container_name:
        subprocess.run(["docker", "stop", container_name], capture_output=True, text=True)
    
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
    
    # Stop container if running
    container_name = scraper_data.get("container_name")
    if container_name:
        subprocess.run(["docker", "stop", container_name], capture_output=True, text=True)
    
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
                print(f"Manually restarting failed scraper for r/{subreddit}")
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
    uvicorn.run(app, host="0.0.0.0", port=8000) 