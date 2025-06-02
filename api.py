#!/usr/bin/env python3
"""
Reddit Scraper Management API

A FastAPI application to manage multiple Reddit scrapers.
Start, stop, and monitor scrapers for different subreddits through HTTP endpoints.
Each scraper can use unique Reddit API credentials to avoid rate limit conflicts.
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

# Load environment variables (fallback defaults)
load_dotenv()

app = FastAPI(
    title="Reddit Scraper API",
    description="Manage multiple Reddit scrapers with unique credentials",
    version="1.0.0"
)

# MongoDB connection for stats
try:
    client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
    db = client["seeky_testing"]
    posts_collection = db["reddit_posts"]
    comments_collection = db["reddit_comments"]
    subreddit_collection = db["subreddit_metadata"]
    mongo_connected = True
except:
    mongo_connected = False

# Global storage for active scrapers
active_scrapers: Dict[str, dict] = {}

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

class ScraperStatus(BaseModel):
    subreddit: str
    status: str  # "running", "stopped", "error"
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    config: Optional[ScraperConfig] = None
    last_error: Optional[str] = None

def run_scraper(config: ScraperConfig):
    """Run a scraper in a separate Docker container with unique credentials"""
    try:
        # Create unique container name
        container_name = f"reddit-scraper-{config.subreddit}"
        
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
            raise Exception(f"Failed to start container: {result.stderr}")
        
        container_id = result.stdout.strip()
        
        # Update scraper info (don't store actual credentials in memory)
        config_safe = config.copy()
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
        print(f"Error starting container for r/{config.subreddit}: {e}")
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
    """Enhanced web dashboard with credential input"""
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
            button { padding: 10px 20px; margin: 5px; border: none; border-radius: 3px; cursor: pointer; }
            .start { background-color: #4CAF50; color: white; }
            .stop { background-color: #f44336; color: white; }
            .stats { background-color: #2196F3; color: white; }
            input, select, textarea { padding: 8px; margin: 5px; border: 1px solid #ddd; border-radius: 3px; }
            .credentials-section { background: #f9f9f9; padding: 15px; border-radius: 5px; margin: 10px 0; }
            .form-row { margin: 10px 0; }
            .form-row label { display: inline-block; width: 150px; }
            .collapsible { cursor: pointer; background: #eee; padding: 10px; border: none; text-align: left; width: 100%; }
            .content { display: none; padding: 10px; background: white; border: 1px solid #ddd; }
        </style>
    </head>
    <body>
        <h1>Reddit Scraper Dashboard</h1>
        <p><strong>Note:</strong> Each scraper uses unique Reddit API credentials to avoid rate limit conflicts.</p>
        
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
                <input type="number" id="interval" value="300" />
                
                <label>Comment Batch:</label>
                <input type="number" id="comment_batch" value="20" />
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
            <button onclick="startScraper()" class="start">🚀 Start Scraper</button>
        </div>
        
        <script>
            const presets = {
                high: { posts_limit: 2000, interval: 180, comment_batch: 30 },
                medium: { posts_limit: 1000, interval: 300, comment_batch: 20 },
                low: { posts_limit: 500, interval: 600, comment_batch: 10 }
            };
            
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
            
            async function loadScrapers() {
                const response = await fetch('/scrapers');
                const scrapers = await response.json();
                const container = document.getElementById('scrapers');
                container.innerHTML = '';
                
                Object.entries(scrapers).forEach(([subreddit, info]) => {
                    const div = document.createElement('div');
                    div.className = `scraper ${info.status}`;
                    div.innerHTML = `
                        <h3>r/${subreddit}</h3>
                        <p><strong>Status:</strong> ${info.status}</p>
                        <p><strong>Reddit User:</strong> ${info.config?.credentials?.username || 'N/A'}</p>
                        <p><strong>Container:</strong> ${info.container_name || 'N/A'}</p>
                        <p><strong>Posts Limit:</strong> ${info.config?.posts_limit || 'N/A'}</p>
                        <p><strong>Interval:</strong> ${info.config?.interval || 'N/A'}s</p>
                        <p><strong>Comment Batch:</strong> ${info.config?.comment_batch || 'N/A'}</p>
                        ${info.started_at ? `<p><strong>Started:</strong> ${new Date(info.started_at).toLocaleString()}</p>` : ''}
                        ${info.last_error ? `<p><strong>Error:</strong> ${info.last_error}</p>` : ''}
                        <button onclick="stopScraper('${subreddit}')" class="stop">⏹️ Stop</button>
                        <button onclick="getStats('${subreddit}')" class="stats">📊 Stats</button>
                        <button onclick="getLogs('${subreddit}')" class="stats">📋 Logs</button>
                    `;
                    container.appendChild(div);
                });
            }
            
            async function startScraper() {
                // Validate required fields
                const requiredFields = ['subreddit', 'client_id', 'client_secret', 'username', 'password', 'user_agent'];
                for (const field of requiredFields) {
                    if (!document.getElementById(field).value) {
                        alert(`Please fill in ${field.replace('_', ' ')}`);
                        return;
                    }
                }
                
                const config = {
                    subreddit: document.getElementById('subreddit').value,
                    posts_limit: parseInt(document.getElementById('posts_limit').value),
                    interval: parseInt(document.getElementById('interval').value),
                    comment_batch: parseInt(document.getElementById('comment_batch').value),
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
                } else {
                    const error = await response.json();
                    alert('Error: ' + error.detail);
                }
            }
            
            async function stopScraper(subreddit) {
                const response = await fetch(`/scrapers/${subreddit}/stop`, { method: 'POST' });
                if (response.ok) {
                    alert('Scraper stopped!');
                    loadScrapers();
                } else {
                    alert('Error stopping scraper');
                }
            }
            
            async function getStats(subreddit) {
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
            }
            
            async function getLogs(subreddit) {
                const response = await fetch(`/scrapers/${subreddit}/logs`);
                const logs = await response.json();
                const logsText = `
r/${subreddit} Logs:
━━━━━━━━━━━━━━━━━━━━━━
${logs.logs}
                `;
                alert(logsText);
            }
            
            // Load scrapers on page load and refresh every 15 seconds
            loadScrapers();
            setInterval(loadScrapers, 15000);
        </script>
    </body>
    </html>
    """
    return html

@app.get("/scrapers")
async def list_scrapers():
    """List all active scrapers and their status"""
    result = {}
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
    if config.subreddit in active_scrapers:
        scraper_info = active_scrapers[config.subreddit]
        if "container_name" in scraper_info:
            container_status = check_container_status(scraper_info["container_name"])
            if container_status == "running":
                raise HTTPException(status_code=400, detail="Scraper already running for this subreddit")
    
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
        "container_name": f"reddit-scraper-{config.subreddit}"
    }

@app.post("/scrapers/{subreddit}/stop")
async def stop_scraper(subreddit: str):
    """Stop a running scraper container"""
    if subreddit not in active_scrapers:
        raise HTTPException(status_code=404, detail="Scraper not found")
    
    scraper_info = active_scrapers[subreddit]
    if "container_name" in scraper_info:
        try:
            # Stop the Docker container
            result = subprocess.run([
                "docker", "stop", scraper_info["container_name"]
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                scraper_info["status"] = "stopped"
                print(f"Stopped container {scraper_info['container_name']} for r/{subreddit}")
            else:
                # Try force kill if stop didn't work
                subprocess.run([
                    "docker", "kill", scraper_info["container_name"]
                ], capture_output=True, text=True)
                scraper_info["status"] = "stopped"
                print(f"Force killed container {scraper_info['container_name']} for r/{subreddit}")
                
        except subprocess.TimeoutExpired:
            # Force kill if timeout
            subprocess.run([
                "docker", "kill", scraper_info["container_name"]
            ], capture_output=True, text=True)
            scraper_info["status"] = "stopped"
            print(f"Timeout - force killed container {scraper_info['container_name']} for r/{subreddit}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error stopping container: {str(e)}")
    
    scraper_info["status"] = "stopped"
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
    if subreddit not in active_scrapers:
        return {"status": "not_found", "message": "No scraper found for this subreddit"}
    
    scraper_info = active_scrapers[subreddit]
    
    # Check if container is still running
    if "container_name" in scraper_info:
        container_status = check_container_status(scraper_info["container_name"])
        if container_status == "running":
            status = "running"
        elif container_status == "exited":
            status = "stopped"
        elif container_status is None:
            status = "stopped"  # Container doesn't exist
        else:
            status = container_status
        container_id = scraper_info.get("container_id")
        container_name = scraper_info.get("container_name")
    else:
        status = scraper_info["status"]
        container_id = None
        container_name = None
    
    return {
        "subreddit": subreddit,
        "status": status,
        "container_id": container_id,
        "container_name": container_name,
        "started_at": scraper_info["started_at"],
        "config": scraper_info["config"].dict() if scraper_info["config"] else None,
        "last_error": scraper_info.get("last_error")
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

@app.delete("/scrapers/{subreddit}")
async def remove_scraper(subreddit: str):
    """Remove a scraper (stop it first if running)"""
    if subreddit in active_scrapers:
        # Stop if running
        await stop_scraper(subreddit)
        # Remove from tracking
        del active_scrapers[subreddit]
        return {"message": f"Scraper removed for r/{subreddit}"}
    else:
        raise HTTPException(status_code=404, detail="Scraper not found")

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
    for subreddit, info in active_scrapers.items():
        if "container_name" in info:
            container_status = check_container_status(info["container_name"])
            if container_status == "running":
                running_containers += 1
    
    return {
        "status": "healthy",
        "active_scrapers": len(active_scrapers),
        "running_containers": running_containers,
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000) 