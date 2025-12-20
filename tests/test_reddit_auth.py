#!/usr/bin/env python3
"""Test Reddit authentication with PRAW for web app using OAuth2 flow."""

import praw
import random
import string
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

CLIENT_ID = "vSGVr30g42ZJXX9BL36Xzw"
CLIENT_SECRET = "58nHLwX3uAg3snY7wJ8KzVmubwetNA"
REDIRECT_URI = "http://localhost:8080/callback"
USER_AGENT = "TestWebApp/1.0 by CelebrationOk4516"

# Store the authorization code
auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    """Handle the OAuth2 callback from Reddit."""

    def do_GET(self):
        global auth_code
        parsed = urlparse(self.path)

        if parsed.path == "/callback":
            query = parse_qs(parsed.query)

            if "code" in query:
                auth_code = query["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Success!</h1><p>You can close this window.</p>")
            elif "error" in query:
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                error = query.get("error", ["unknown"])[0]
                self.wfile.write(f"<h1>Error: {error}</h1>".encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logging


def test_oauth2_auth():
    """Test Reddit API authentication with OAuth2 flow for web app."""
    global auth_code

    print("Reddit OAuth2 Web App Authentication")
    print("=" * 50)

    # Create Reddit instance for OAuth2
    reddit = praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        user_agent=USER_AGENT
    )

    # Generate random state for security
    state = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

    # Get authorization URL
    scopes = ["identity", "read", "history"]
    auth_url = reddit.auth.url(scopes=scopes, state=state, duration="permanent")

    print(f"\n1. Opening browser for authorization...")
    print(f"   URL: {auth_url}\n")

    # Start local server to receive callback
    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.timeout = 120  # 2 minute timeout

    # Open browser
    webbrowser.open(auth_url)

    print("2. Waiting for authorization (check your browser)...")
    print("   Log in as: CelebrationOk4516")
    print("   Password: Bitpulse2023!\n")

    # Wait for callback
    while auth_code is None:
        server.handle_request()

    server.server_close()

    print("3. Got authorization code, exchanging for token...")

    # Exchange code for refresh token
    refresh_token = reddit.auth.authorize(auth_code)
    print(f"   Refresh token: {refresh_token[:20]}...")

    # Test authentication
    try:
        user = reddit.user.me()
        print(f"\n✓ Successfully authenticated as: {user.name}")
        print(f"  Account karma: {user.link_karma + user.comment_karma}")

        # Test subreddit access
        subreddit = reddit.subreddit("python")
        print(f"\n  Test subreddit: r/{subreddit.display_name}")
        print(f"  Subscribers: {subreddit.subscribers}")

        print("\n" + "=" * 50)
        print("SAVE THIS REFRESH TOKEN FOR FUTURE USE:")
        print(refresh_token)
        print("=" * 50)

        return True

    except Exception as e:
        print(f"\n✗ Authentication failed: {e}")
        return False


def test_with_refresh_token(refresh_token: str):
    """Test authentication using an existing refresh token."""
    print("Testing with Refresh Token")
    print("=" * 50)

    try:
        reddit = praw.Reddit(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            refresh_token=refresh_token,
            user_agent=USER_AGENT
        )

        user = reddit.user.me()
        print(f"\n✅ Successfully authenticated as: {user.name}")
        print(f"   Account karma: {user.link_karma + user.comment_karma}")

        # Test subreddit access
        subreddit = reddit.subreddit("python")
        print(f"\n   Test subreddit: r/{subreddit.display_name}")
        print(f"   Subscribers: {subreddit.subscribers:,}")

        # Test fetching posts
        print(f"\n   Recent posts from r/python:")
        for i, post in enumerate(subreddit.new(limit=3)):
            print(f"   {i+1}. {post.title[:50]}...")

        print("\n✅ All tests passed!")
        return True

    except Exception as e:
        print(f"\n❌ Authentication failed: {e}")
        return False


def test_api_validation_endpoint(refresh_token: str = None, password: str = None):
    """Test the API's /accounts/validate endpoint."""
    import requests

    print("\nTesting API Validation Endpoint")
    print("=" * 50)

    credentials = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": "CelebrationOk4516",
        "user_agent": USER_AGENT
    }

    if refresh_token:
        credentials["refresh_token"] = refresh_token
        print("Using refresh_token auth (web app)")
    elif password:
        credentials["password"] = password
        print("Using password auth (script app)")
    else:
        print("❌ Need either refresh_token or password")
        return False

    try:
        response = requests.post(
            "http://localhost:8000/accounts/validate",
            json=credentials,
            timeout=15
        )
        result = response.json()

        if result.get("valid"):
            print(f"\n✅ Credentials valid!")
            print(f"   Username: {result.get('username')}")
        else:
            print(f"\n❌ Validation failed: {result.get('error')}")
            print(f"   Error type: {result.get('error_type')}")

        return result.get("valid", False)

    except requests.exceptions.ConnectionError:
        print("\n❌ Cannot connect to API server at localhost:8000")
        print("   Make sure the API is running: docker-compose -f docker-compose.api.yml up")
        return False
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return False


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == "--get-token":
            # Run OAuth flow to get a new refresh token
            test_oauth2_auth()
        elif sys.argv[1] == "--test-token":
            # Test with existing refresh token
            if len(sys.argv) > 2:
                test_with_refresh_token(sys.argv[2])
            else:
                print("Usage: python test_reddit_auth.py --test-token <refresh_token>")
        elif sys.argv[1] == "--test-api":
            # Test the API validation endpoint
            if len(sys.argv) > 2:
                test_api_validation_endpoint(refresh_token=sys.argv[2])
            else:
                print("Usage: python test_reddit_auth.py --test-api <refresh_token>")
        else:
            print("Usage:")
            print("  python test_reddit_auth.py --get-token      # Get new refresh token via OAuth")
            print("  python test_reddit_auth.py --test-token <token>  # Test existing token")
            print("  python test_reddit_auth.py --test-api <token>    # Test API validation endpoint")
    else:
        # Default: run OAuth flow
        test_oauth2_auth()
