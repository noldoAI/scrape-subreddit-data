import praw
from datetime import datetime
from dotenv import load_dotenv
import pymongo
import os
import time


load_dotenv()

client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = client["techbro"]
collection = db["reddit_posts"]


reddit = praw.Reddit(
    client_id=os.getenv("R_CLIENT_ID"),
    client_secret=os.getenv("R_CLIENT_SECRET"),
    username=os.getenv("R_USERNAME"),
    password=os.getenv("R_PASSWORD"),
    user_agent=os.getenv("R_USER_AGENT")
)




print(reddit.user.me())