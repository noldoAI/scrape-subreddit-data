#!/usr/bin/env python3
"""
Vector Search Index Setup for MongoDB Atlas or Azure Cosmos DB

Creates vector search indexes for semantic subreddit search.
Supports both MongoDB Atlas and Azure Cosmos DB for MongoDB vCore.

Usage:
    python setup_vector_index.py                           # Create combined index on metadata (default)
    python setup_vector_index.py --embedding-type persona  # Create persona embedding index
    python setup_vector_index.py --embedding-type all      # Create both combined and persona indexes
    python setup_vector_index.py --collection discovery    # Create index on discovery collection
    python setup_vector_index.py --collection both         # Create indexes on both collections
    python setup_vector_index.py --drop                    # Drop existing index and recreate
    python setup_vector_index.py --backend cosmos          # Use Azure Cosmos DB backend
"""

import os
import sys
import argparse
import logging
import time
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import OperationFailure

# Add parent directory to path for imports when run from discovery/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DISCOVERY_CONFIG, EMBEDDING_WORKER_CONFIG, EMBEDDING_CONFIG, COLLECTIONS, PERSONA_SEARCH_CONFIG

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


def detect_backend():
    """
    Detect whether we're connected to MongoDB Atlas or Azure Cosmos DB.

    Returns:
        str: "atlas" or "cosmos"
    """
    try:
        # Try to get server info
        server_info = client.server_info()
        version = server_info.get('version', '')

        # Azure Cosmos DB typically shows version like "4.0.0" or "3.6.0"
        # and has specific modules
        if 'cosmosdb' in str(server_info).lower():
            return "cosmos"

        # Check if it's Azure by trying an Atlas-specific command
        try:
            db.command("listSearchIndexes", "test")
            return "atlas"
        except OperationFailure as e:
            if "CommandNotFound" in str(e) or "UnrecognizedCommand" in str(e):
                return "cosmos"
            return "atlas"
    except Exception as e:
        logger.warning(f"Could not detect backend, assuming cosmos: {e}")
        return "cosmos"


def list_existing_indexes_atlas(collection):
    """List all existing search indexes on the collection (MongoDB Atlas)."""
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
        logger.warning(f"Could not list Atlas indexes: {e}")
        return []


def list_existing_indexes_cosmos(collection):
    """List all existing indexes on the collection (Azure Cosmos DB)."""
    try:
        indexes = list(collection.list_indexes())
        vector_indexes = []
        if indexes:
            logger.info(f"\n📋 Existing indexes on {collection.name}:")
            for idx in indexes:
                idx_name = idx.get('name', 'unnamed')
                # Check if it's a vector index
                if 'cosmosSearchOptions' in idx or idx_name.endswith('_vector'):
                    vector_indexes.append(idx)
                    logger.info(f"   - {idx_name} (vector index)")
                else:
                    logger.info(f"   - {idx_name}")
            return vector_indexes
        else:
            logger.info(f"\n📋 No indexes found on {collection.name}")
            return []
    except Exception as e:
        logger.warning(f"Could not list indexes: {e}")
        return []


def drop_index_atlas(collection, index_name: str):
    """Drop an existing search index (MongoDB Atlas)."""
    try:
        logger.info(f"\n🗑️  Dropping Atlas index: {index_name} from {collection.name}")
        collection.drop_search_index(index_name)
        logger.info(f"   ✓ Index dropped successfully")
        time.sleep(2)
    except Exception as e:
        logger.error(f"   ✗ Error dropping index: {e}")


def drop_index_cosmos(collection, index_name: str):
    """Drop an existing index (Azure Cosmos DB)."""
    try:
        logger.info(f"\n🗑️  Dropping Cosmos index: {index_name} from {collection.name}")
        collection.drop_index(index_name)
        logger.info(f"   ✓ Index dropped successfully")
        time.sleep(2)
    except Exception as e:
        logger.error(f"   ✗ Error dropping index: {e}")


def create_vector_search_index_atlas(collection, index_name: str, embedding_type: str = "combined"):
    """
    Create MongoDB Atlas Vector Search index for subreddit embeddings.
    """
    embedding_config = EMBEDDING_CONFIGS.get(embedding_type, EMBEDDING_CONFIGS["combined"])
    embedding_path = embedding_config["path"]
    dimensions = EMBEDDING_CONFIG["dimensions"]

    logger.info(f"\n🔧 Creating Atlas vector search index...")
    logger.info(f"   Collection: {db.name}.{collection.name}")
    logger.info(f"   Index name: {index_name}")
    logger.info(f"   Embedding path: {embedding_path}")
    logger.info(f"   Dimensions: {dimensions}")

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
                                "dimensions": dimensions,
                                "similarity": "cosine"
                            }
                        }
                    }
                }
            }
        }
    }

    try:
        result = collection.create_search_index(index_definition)
        logger.info(f"   ✓ Index creation initiated: {result}")
        return True
    except Exception as e:
        logger.error(f"\n❌ Error creating Atlas index: {e}")
        return False


def create_vector_search_index_cosmos(collection, index_name: str, embedding_type: str = "combined"):
    """
    Create Azure Cosmos DB for MongoDB vCore vector search index.

    Supports multiple syntax formats for different MongoDB versions.
    """
    embedding_config = EMBEDDING_CONFIGS.get(embedding_type, EMBEDDING_CONFIGS["combined"])
    embedding_path = embedding_config["path"]
    dimensions = EMBEDDING_CONFIG["dimensions"]

    logger.info(f"\n🔧 Creating Cosmos DB vector search index...")
    logger.info(f"   Collection: {db.name}.{collection.name}")
    logger.info(f"   Index name: {index_name}")
    logger.info(f"   Embedding path: {embedding_path}")
    logger.info(f"   Dimensions: {dimensions}")

    # Try multiple syntaxes for different Cosmos DB/MongoDB versions

    # Syntax 1: MongoDB 7.0+ / Cosmos DB vCore with vector-hnsw
    index_definitions = [
        # Format 1: vector-hnsw (newer, faster)
        {
            "name": index_name,
            "key": {embedding_path: "cosmosSearch"},
            "cosmosSearchOptions": {
                "kind": "vector-hnsw",
                "m": 16,
                "efConstruction": 64,
                "similarity": "COS",
                "dimensions": dimensions
            }
        },
        # Format 2: vector-ivf (older, more compatible)
        {
            "name": index_name,
            "key": {embedding_path: "cosmosSearch"},
            "cosmosSearchOptions": {
                "kind": "vector-ivf",
                "numLists": 100,
                "similarity": "COS",
                "dimensions": dimensions
            }
        },
        # Format 3: Simple vector index (MongoDB 8.0 native)
        {
            "name": index_name,
            "key": {embedding_path: "vector"},
            "vectorOptions": {
                "type": "hnsw",
                "dimensions": dimensions,
                "similarity": "cosine"
            }
        }
    ]

    for i, index_definition in enumerate(index_definitions):
        try:
            logger.info(f"   Trying syntax {i+1}...")
            result = db.command({
                "createIndexes": collection.name,
                "indexes": [index_definition]
            })

            if result.get('ok') == 1:
                logger.info(f"   ✓ Index created successfully with syntax {i+1}!")
                logger.info(f"   Response: {result}")
                return True

        except OperationFailure as e:
            error_str = str(e).lower()
            if "already exists" in error_str:
                logger.info(f"   ✓ Index already exists")
                return True
            elif "not valid" in error_str or "not supported" in error_str:
                logger.info(f"   Syntax {i+1} not supported, trying next...")
                continue
            else:
                logger.warning(f"   Syntax {i+1} failed: {e}")
                continue
        except Exception as e:
            logger.warning(f"   Syntax {i+1} error: {e}")
            continue

    # All syntaxes failed
    logger.error(f"\n❌ All index creation attempts failed")
    logger.error(f"\n💡 Vector search may not be enabled. To enable:")
    logger.error(f"   1. Go to Azure Portal → your Cosmos DB account")
    logger.error(f"   2. Settings → Features")
    logger.error(f"   3. Enable 'Vector Search' feature")
    logger.error(f"   4. Wait a few minutes for it to take effect")
    logger.info(f"\n📝 Falling back to application-level similarity search (no index needed)")
    return False


def verify_index_atlas(collection, index_name: str, embedding_type: str = "combined"):
    """Verify that the Atlas vector index is working."""
    embedding_config = EMBEDDING_CONFIGS.get(embedding_type, EMBEDDING_CONFIGS["combined"])
    embedding_path = embedding_config["path"]

    logger.info(f"\n🧪 Verifying Atlas vector search index...")

    sample = collection.find_one({embedding_path: {"$exists": True}})
    if not sample:
        logger.error(f"   ✗ No documents with embeddings found")
        return False

    query_vector = sample['embeddings'][embedding_path.split('.')[-1]]

    try:
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
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]))

        if results:
            logger.info(f"   ✓ Vector search is working!")
            for r in results:
                logger.info(f"      - r/{r['subreddit_name']} (score: {r.get('score', 'N/A')})")
            return True
        return False
    except Exception as e:
        logger.error(f"   ✗ Vector search failed: {e}")
        return False


def verify_index_cosmos(collection, index_name: str, embedding_type: str = "combined"):
    """Verify that the Cosmos DB vector index is working."""
    embedding_config = EMBEDDING_CONFIGS.get(embedding_type, EMBEDDING_CONFIGS["combined"])
    embedding_path = embedding_config["path"]

    logger.info(f"\n🧪 Verifying Cosmos DB vector search index...")

    sample = collection.find_one({embedding_path: {"$exists": True}})
    if not sample:
        logger.error(f"   ✗ No documents with embeddings found")
        return False

    query_vector = sample['embeddings'][embedding_path.split('.')[-1]]

    try:
        # Azure Cosmos DB for MongoDB vCore uses $search with cosmosSearch
        results = list(collection.aggregate([
            {
                "$search": {
                    "cosmosSearch": {
                        "vector": query_vector,
                        "path": embedding_path,
                        "k": 3
                    },
                    "returnStoredSource": True
                }
            },
            {
                "$project": {
                    "subreddit_name": 1,
                    "similarityScore": {"$meta": "searchScore"}
                }
            }
        ]))

        if results:
            logger.info(f"   ✓ Vector search is working!")
            for r in results:
                logger.info(f"      - r/{r.get('subreddit_name', 'unknown')} (score: {r.get('similarityScore', 'N/A')})")
            return True
        else:
            logger.warning("   ⚠️  Vector search returned no results")
            return False
    except Exception as e:
        logger.error(f"   ✗ Vector search failed: {e}")
        logger.error(f"   This might be expected if index is still building")
        return False


def setup_collection(collection_key: str, drop: bool = False, verify_only: bool = False,
                     embedding_type: str = "combined", backend: str = "auto"):
    """Setup vector index for a specific collection."""
    config = COLLECTION_CONFIGS[collection_key]
    embedding_config = EMBEDDING_CONFIGS.get(embedding_type, EMBEDDING_CONFIGS["combined"])
    collection = config["collection"]

    # Build index name based on embedding type
    base_index_name = config["index_name"]
    if embedding_type == "persona":
        index_name = PERSONA_SEARCH_CONFIG.get("persona_vector_index_name", f"{base_index_name}_persona")
    else:
        index_name = base_index_name

    # Auto-detect backend if not specified
    if backend == "auto":
        backend = detect_backend()
        logger.info(f"\n🔍 Detected backend: {backend.upper()}")

    logger.info(f"\n{'='*80}")
    logger.info(f"Setting up: {config['description']}")
    logger.info(f"Collection: {collection.name}")
    logger.info(f"Embedding type: {embedding_type} ({embedding_config['description']})")
    logger.info(f"Index: {index_name}")
    logger.info(f"Backend: {backend}")
    logger.info(f"{'='*80}")

    # Backend-specific functions
    if backend == "atlas":
        list_indexes = list_existing_indexes_atlas
        drop_index = drop_index_atlas
        create_index = create_vector_search_index_atlas
        verify_index = verify_index_atlas
    else:  # cosmos
        list_indexes = list_existing_indexes_cosmos
        drop_index = drop_index_cosmos
        create_index = create_vector_search_index_cosmos
        verify_index = verify_index_cosmos

    # List existing indexes
    existing_indexes = list_indexes(collection)

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
    success = create_index(collection, index_name, embedding_type)

    if success:
        logger.info(f"\n{'='*80}\n")
        # Give Cosmos DB a moment to build the index
        if backend == "cosmos":
            logger.info("   Waiting 5 seconds for index to be ready...")
            time.sleep(5)
        verify_index(collection, index_name, embedding_type)


def main():
    parser = argparse.ArgumentParser(
        description='Setup vector search index for MongoDB Atlas or Azure Cosmos DB',
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
  python setup_vector_index.py --backend cosmos          # Force Azure Cosmos DB backend
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
    parser.add_argument(
        '--backend',
        type=str,
        default='auto',
        choices=['auto', 'atlas', 'cosmos'],
        help='Database backend: auto (detect), atlas (MongoDB Atlas), cosmos (Azure Cosmos DB)'
    )

    args = parser.parse_args()

    logger.info(f"\n{'='*80}")
    logger.info(f"Vector Search Index Setup")
    logger.info(f"{'='*80}")

    # Determine embedding types to process
    if args.embedding_type == 'all':
        embedding_types = ['combined', 'persona']
    else:
        embedding_types = [args.embedding_type]

    # Process collections and embedding types
    if args.collection == 'both':
        for emb_type in embedding_types:
            setup_collection('discovery', args.drop, args.verify_only, emb_type, args.backend)
            setup_collection('metadata', args.drop, args.verify_only, emb_type, args.backend)
    else:
        for emb_type in embedding_types:
            setup_collection(args.collection, args.drop, args.verify_only, emb_type, args.backend)

    # Final summary
    logger.info(f"\n{'='*80}")
    logger.info(f"✅ Setup complete!")
    logger.info(f"\n🎯 Next steps:")
    if 'persona' in embedding_types:
        logger.info(f"   1. Test persona search: python test_persona_search.py \"I'm building SaaS for startups\"")
    else:
        logger.info(f"   1. Test semantic search: python semantic_search.py --query 'building b2b saas'")
    logger.info(f"   2. Or use the API: POST /search/subreddits")
    logger.info(f"{'='*80}\n")


if __name__ == "__main__":
    main()
