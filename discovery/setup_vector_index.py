#!/usr/bin/env python3
"""
MongoDB Atlas Vector Search Index Setup

Creates vector search indexes for semantic subreddit search.
This enables fast similarity searches using cosine distance.

Usage:
    python setup_vector_index.py                           # Create combined index on metadata (default)
    python setup_vector_index.py --embedding-type persona  # Create persona embedding index
    python setup_vector_index.py --embedding-type all      # Create both combined and persona indexes
    python setup_vector_index.py --collection discovery    # Create index on discovery collection
    python setup_vector_index.py --collection both         # Create indexes on both collections
    python setup_vector_index.py --drop                    # Drop existing index and recreate
"""

import os
import sys
import argparse
import logging
import time
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.operations import SearchIndexModel

# Add parent directory to path for imports when run from discovery/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DISCOVERY_CONFIG, EMBEDDING_WORKER_CONFIG, COLLECTIONS, PERSONA_SEARCH_CONFIG

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('vector-index-setup')

# Load environment variables
load_dotenv()

# MongoDB connection
MONGODB_URI = os.getenv('MONGODB_URI')
if not MONGODB_URI:
    logger.error("MONGODB_URI environment variable not set")
    sys.exit(1)

client = MongoClient(MONGODB_URI)
db = client.noldo

# Collection configurations
COLLECTION_CONFIGS = {
    "discovery": {
        "collection": db.subreddit_discovery,
        "index_name": DISCOVERY_CONFIG["vector_index_name"],
        "description": "Discovered subreddits (via discover_subreddits.py)"
    },
    "metadata": {
        "collection": db[COLLECTIONS["SUBREDDIT_METADATA"]],
        "index_name": EMBEDDING_WORKER_CONFIG["metadata_vector_index_name"],
        "description": "Actively scraped subreddits (via reddit_scraper.py)"
    }
}

# Embedding type configurations
EMBEDDING_CONFIGS = {
    "combined": {
        "path": "embeddings.combined_embedding",
        "index_suffix": "",  # Uses default index name
        "description": "Topic-focused embeddings (subreddit content)"
    },
    "persona": {
        "path": "embeddings.persona_embedding",
        "index_suffix": "_persona",  # Appends to make unique index name
        "description": "Persona-focused embeddings (audience profiles)"
    }
}


def list_existing_indexes(collection):
    """List all existing search indexes on the collection."""
    try:
        indexes = list(collection.list_search_indexes())
        if indexes:
            logger.info(f"\n📋 Existing search indexes on {collection.name}:")
            for idx in indexes:
                logger.info(f"   - {idx.get('name', 'unnamed')} (status: {idx.get('status', 'unknown')})")
            return indexes
        else:
            logger.info(f"\n📋 No existing search indexes found on {collection.name}")
            return []
    except Exception as e:
        logger.warning(f"Could not list indexes (might not be supported on this MongoDB version): {e}")
        return []


def drop_index(collection, index_name: str):
    """Drop an existing search index."""
    try:
        logger.info(f"\n🗑️  Dropping index: {index_name} from {collection.name}")
        collection.drop_search_index(index_name)
        logger.info(f"   ✓ Index dropped successfully")
        time.sleep(2)  # Wait a bit before recreating
    except Exception as e:
        logger.error(f"   ✗ Error dropping index: {e}")


def create_vector_search_index(collection, index_name: str, embedding_type: str = "combined"):
    """
    Create MongoDB Atlas Vector Search index for subreddit embeddings.

    Index Configuration:
    - Field: embeddings.combined_embedding or embeddings.persona_embedding (768 dimensions)
    - Similarity: cosine (best for normalized embeddings)
    - Filters: subreddit_type, over_18, subscribers (for hybrid search)
    """
    embedding_config = EMBEDDING_CONFIGS.get(embedding_type, EMBEDDING_CONFIGS["combined"])
    embedding_path = embedding_config["path"]

    logger.info(f"\n🔧 Creating vector search index...")
    logger.info(f"   Collection: {db.name}.{collection.name}")
    logger.info(f"   Index name: {index_name}")
    logger.info(f"   Embedding type: {embedding_type} ({embedding_config['description']})")
    logger.info(f"   Embedding path: {embedding_path}")

    # Define the vector search index for Atlas Vector Search
    # Using mappings format for pre-7.0 Atlas clusters
    index_definition = {
        "name": index_name,
        "type": "vectorSearch",
        "definition": {
            "mappings": {
                "dynamic": True,
                "fields": {
                    "embeddings": {
                        "type": "document",
                        "fields": {
                            embedding_path.split('.')[-1]: {
                                "type": "knnVector",
                                "dimensions": 768,
                                "similarity": "cosine"
                            }
                        }
                    }
                }
            }
        }
    }

    try:
        # Create the index using dict format
        result = collection.create_search_index(index_definition)
        logger.info(f"   ✓ Index creation initiated: {result}")
        logger.info(f"\n⏳ Index is being built (this may take 1-5 minutes)...")
        logger.info(f"   You can check status in MongoDB Atlas UI or wait for confirmation below\n")

        # Wait for index to become ready
        max_wait = 300  # 5 minutes
        wait_interval = 10  # Check every 10 seconds
        elapsed = 0

        while elapsed < max_wait:
            time.sleep(wait_interval)
            elapsed += wait_interval

            try:
                indexes = list(collection.list_search_indexes())
                for idx in indexes:
                    if idx.get('name') == index_name:
                        status = idx.get('status', 'UNKNOWN')
                        logger.info(f"   Index status: {status} (elapsed: {elapsed}s)")

                        if status == 'READY':
                            logger.info(f"\n✅ Vector search index created successfully!")
                            logger.info(f"   Status: READY")
                            logger.info(f"   Time taken: {elapsed} seconds")
                            return True
                        elif status in ['FAILED', 'DOES NOT EXIST']:
                            logger.error(f"\n❌ Index creation failed with status: {status}")
                            return False
            except Exception as e:
                logger.warning(f"   Could not check status: {e}")

        logger.warning(f"\n⚠️  Index creation timeout ({max_wait}s)")
        logger.warning(f"   Index may still be building. Check MongoDB Atlas UI.")
        return False

    except Exception as e:
        logger.error(f"\n❌ Error creating index: {e}")
        logger.error(f"\n💡 Troubleshooting:")
        logger.error(f"   1. Ensure you're using MongoDB Atlas (vector search not available in self-hosted MongoDB)")
        logger.error(f"   2. Check that you have embeddings in the collection")
        logger.error(f"   3. Verify cluster version supports vector search (M10+ recommended)")
        return False


def verify_index(collection, index_name: str, embedding_type: str = "combined"):
    """Verify that the vector index is working with a test query."""
    embedding_config = EMBEDDING_CONFIGS.get(embedding_type, EMBEDDING_CONFIGS["combined"])
    embedding_path = embedding_config["path"]

    logger.info(f"\n🧪 Verifying vector search index on {collection.name}...")
    logger.info(f"   Embedding path: {embedding_path}")

    # Get a sample embedding from the database
    sample = collection.find_one({embedding_path: {"$exists": True}})

    if not sample:
        logger.error(f"   ✗ No documents with {embedding_type} embeddings found in {collection.name}.")
        if embedding_type == "persona":
            logger.error("   Run: python generate_embeddings.py --embedding-type persona")
        elif collection.name == "subreddit_discovery":
            logger.error("   Run: python generate_embeddings.py")
        else:
            logger.error("   Wait for embedding worker to process pending subreddits.")
        return False

    # Navigate to nested embedding field
    query_vector = sample['embeddings'][embedding_path.split('.')[-1]]

    try:
        # Test vector search query
        results = list(collection.aggregate([
            {
                "$vectorSearch": {
                    "index": index_name,
                    "path": embedding_path,
                    "queryVector": query_vector,
                    "numCandidates": 10,
                    "limit": 3
                }
            },
            {
                "$project": {
                    "subreddit_name": 1,
                    "title": 1,
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]))

        if results:
            logger.info(f"   ✓ Vector search is working!")
            logger.info(f"   Found {len(results)} results:")
            for r in results:
                logger.info(f"      - r/{r['subreddit_name']} (score: {r['score']:.3f})")
            return True
        else:
            logger.warning("   ⚠️  Vector search returned no results (index may still be building)")
            return False

    except Exception as e:
        logger.error(f"   ✗ Vector search failed: {e}")
        logger.error(f"   Index may not be ready yet. Wait a few minutes and try again.")
        return False


def setup_collection(collection_key: str, drop: bool = False, verify_only: bool = False, embedding_type: str = "combined"):
    """Setup vector index for a specific collection."""
    config = COLLECTION_CONFIGS[collection_key]
    embedding_config = EMBEDDING_CONFIGS.get(embedding_type, EMBEDDING_CONFIGS["combined"])
    collection = config["collection"]

    # Build index name based on embedding type
    base_index_name = config["index_name"]
    if embedding_type == "persona":
        # Use the persona-specific index name from config
        index_name = PERSONA_SEARCH_CONFIG.get("persona_vector_index_name", f"{base_index_name}_persona")
    else:
        index_name = base_index_name

    logger.info(f"\n{'='*80}")
    logger.info(f"Setting up: {config['description']}")
    logger.info(f"Collection: {collection.name}")
    logger.info(f"Embedding type: {embedding_type} ({embedding_config['description']})")
    logger.info(f"Index: {index_name}")
    logger.info(f"{'='*80}")

    # List existing indexes
    existing_indexes = list_existing_indexes(collection)

    if verify_only:
        verify_index(collection, index_name, embedding_type)
        return

    # Drop index if requested
    if drop:
        index_exists = any(idx.get('name') == index_name for idx in existing_indexes)
        if index_exists:
            drop_index(collection, index_name)
        else:
            logger.warning(f"\n⚠️  Index '{index_name}' does not exist, nothing to drop")

    # Check if index already exists
    index_exists = any(idx.get('name') == index_name for idx in existing_indexes)
    if index_exists and not drop:
        logger.info(f"\n✓ Index '{index_name}' already exists. Use --drop to recreate.")
        verify_index(collection, index_name, embedding_type)
        return

    # Create index
    success = create_vector_search_index(collection, index_name, embedding_type)

    if success:
        # Verify index
        logger.info(f"\n{'='*80}\n")
        verify_index(collection, index_name, embedding_type)


def main():
    parser = argparse.ArgumentParser(
        description='Setup MongoDB Atlas Vector Search index for subreddit semantic search',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python setup_vector_index.py                           # Create combined index on metadata (default)
  python setup_vector_index.py --embedding-type persona  # Create persona embedding index
  python setup_vector_index.py --embedding-type all      # Create both combined and persona indexes
  python setup_vector_index.py --collection discovery    # Create index on discovery collection
  python setup_vector_index.py --collection both         # Create indexes on both collections
  python setup_vector_index.py --drop                    # Drop and recreate index
  python setup_vector_index.py --verify-only             # Only verify existing index
        """
    )
    parser.add_argument(
        '--collection',
        type=str,
        default='metadata',
        choices=['discovery', 'metadata', 'both'],
        help='Collection to create index on (default: metadata)'
    )
    parser.add_argument(
        '--embedding-type',
        type=str,
        default='combined',
        choices=['combined', 'persona', 'all'],
        help='Embedding type to index: combined (topic), persona (audience), or all (default: combined)'
    )
    parser.add_argument(
        '--drop',
        action='store_true',
        help='Drop existing index before creating new one'
    )
    parser.add_argument(
        '--verify-only',
        action='store_true',
        help='Only verify existing index without creating'
    )

    args = parser.parse_args()

    logger.info(f"\n{'='*80}")
    logger.info(f"MongoDB Atlas Vector Search Index Setup")
    logger.info(f"{'='*80}")

    # Determine embedding types to process
    if args.embedding_type == 'all':
        embedding_types = ['combined', 'persona']
    else:
        embedding_types = [args.embedding_type]

    # Process collections and embedding types
    if args.collection == 'both':
        for emb_type in embedding_types:
            setup_collection('discovery', args.drop, args.verify_only, emb_type)
            setup_collection('metadata', args.drop, args.verify_only, emb_type)
    else:
        for emb_type in embedding_types:
            setup_collection(args.collection, args.drop, args.verify_only, emb_type)

    # Final summary
    logger.info(f"\n{'='*80}")
    logger.info(f"✅ Setup complete!")
    logger.info(f"\n🎯 Next steps:")
    if 'persona' in embedding_types:
        logger.info(f"   1. Test persona search: python test_persona_search.py \"I'm building SaaS for startups\"")
    else:
        logger.info(f"   1. Test semantic search: python semantic_search_subreddits.py --query 'building b2b saas'")
    logger.info(f"   2. Or use the API: POST /search/subreddits")
    logger.info(f"{'='*80}\n")


if __name__ == "__main__":
    main()
