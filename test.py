import praw
from datetime import datetime
from dotenv import load_dotenv
import pymongo
import os
import time
from rate_limits import check_rate_limit
from praw.models import MoreComments


load_dotenv()

client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client["techbro"]
posts_collection = db["reddit_posts"]
comments_collection = db["reddit_comments"]


reddit = praw.Reddit(
    client_id=os.getenv("R_CLIENT_ID"),
    client_secret=os.getenv("R_CLIENT_SECRET"),
    username=os.getenv("R_USERNAME"),
    password=os.getenv("R_PASSWORD"),
    user_agent=os.getenv("R_USER_AGENT")
)


print(f"Authenticated as: {reddit.user.me()}")



# Specify the subreddit
subreddit_name = "wallstreetbets"
subreddit = reddit.subreddit(subreddit_name)

print(dir(subreddit))

# Basic Info
data = {
    "display_name": subreddit.display_name,
    "title": subreddit.title,
    "public_description": subreddit.public_description,
    "description": subreddit.description,
    "url": subreddit.url,
    "subscribers": subreddit.subscribers,
    "active_user_count": subreddit.accounts_active,
    "over_18": subreddit.over18,
    "lang": subreddit.lang,
    "created_utc": datetime.fromtimestamp(subreddit.created_utc).strftime('%Y-%m-%d %H:%M:%S'),
    "submission_type": subreddit.submission_type,
    "submission_text": subreddit.submit_text,
    "submission_text_label": subreddit.submit_text_label,
    "user_is_moderator": subreddit.user_is_moderator,
    "user_is_subscriber": subreddit.user_is_subscriber,
    "quarantine": subreddit.quarantine,
    "advertiser_category": subreddit.advertiser_category,
    "is_enrolled_in_new_modmail": subreddit.is_enrolled_in_new_modmail,
    "primary_color": subreddit.primary_color,
    "show_media": subreddit.show_media,
    "show_media_preview": subreddit.show_media_preview,
    "spoilers_enabled": subreddit.spoilers_enabled,
    "allow_videos": subreddit.allow_videos,
    "allow_images": subreddit.allow_images,
    "allow_polls": subreddit.allow_polls,
    "allow_discovery": subreddit.allow_discovery,
    "allow_prediction_contributors": subreddit.allow_prediction_contributors,
    "has_menu_widget": subreddit.has_menu_widget,
    "icon_img": subreddit.icon_img,
    "community_icon": subreddit.community_icon,
    "banner_img": subreddit.banner_img,
    "banner_background_image": subreddit.banner_background_image,
    "mobile_banner_image": subreddit.mobile_banner_image,
}

# Print all
print(f"\n📊 Full Stats for r/{subreddit.display_name}\n{'-'*60}")
for key, value in data.items():
    print(f"{key}: {value}")