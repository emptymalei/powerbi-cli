# Cache System

The PowerBI CLI includes a cache system for API call results, allowing you to:
- Avoid redundant API calls
- Work offline with previously fetched data
- Version data over time for analysis
- Use cached data as a database for other tasks

## Features

- **JSON Format**: All cached data is stored in JSON format for easy analysis
- **Versioning**: Each cache entry is timestamped for version tracking
- **Local and Remote Storage**: Support for both local paths and cloud storage (S3, etc.) via cloudpathlib
- **Configurable**: Easy configuration through CLI commands
- **Extensible**: Designed to be used as a database for analysis

## Setup

### Configure Cache Folder

```bash
# Local path
pbi config set-cache-folder ~/PowerBI/cache

# Cloud path (S3)
pbi config set-cache-folder s3://my-bucket/powerbi-cache
```

### Enable/Disable Caching

```bash
# Enable caching (default)
pbi config enable-cache

# Disable caching
pbi config disable-cache

# Check status
pbi config show
```

## Usage

### Using Cache with Commands

Most API-based commands support caching. Here's an example with the `workspaces list` command:

```bash
# Fetch from API and cache the result
pbi workspaces list --top 1000

# Use cached data if available (falls back to API if not cached)
pbi workspaces list --use-cache

# Only use cache (fails if not cached)
pbi workspaces list --cache-only
```

### Managing Cache

```bash
# List all cached data
pbi cache list

# List versions for a specific cache key
pbi cache list -k workspaces

# Clear specific cache key
pbi cache clear -k workspaces

# Clear specific version
pbi cache clear -k workspaces -v 20240101_120000

# Clear all cache
pbi cache clear
```

## Cache Structure

The cache is organized as follows:

```
cache_folder/
├── workspaces/
│   ├── 20240101_120000/
│   │   └── workspaces.json
│   └── 20240102_150000/
│       └── workspaces.json
└── apps/
    └── 20240101_130000/
        └── apps.json
```

Each cache file contains:
- `cache_key`: The key identifying the cached data
- `cached_at`: ISO timestamp when data was cached
- `version`: Version identifier (timestamp-based)
- `metadata`: Additional metadata about the request
- `data`: The actual cached data

## Example: Using Cache as a Database

```python
from pbi_cli.cache import CacheManager
import json

# Load cached data
cache = CacheManager(cache_folder="~/PowerBI/cache")
data = cache.load("workspaces", version="latest")

# Access the cached data
workspaces = data["data"]["value"]
print(f"Found {len(workspaces)} workspaces")

# Analyze metadata
print(f"Cached at: {data['cached_at']}")
print(f"Request parameters: {data['metadata']}")
```

## Cloud Storage

The cache system supports cloud storage via cloudpathlib. Ensure you have the necessary credentials configured for your cloud provider.

### S3 Example

```bash
# Configure AWS credentials (standard AWS CLI configuration)
aws configure

# Set S3 cache folder
pbi config set-cache-folder s3://my-bucket/powerbi-cache

# Use as normal
pbi workspaces list
```

## Best Practices

1. **Use Versioning**: The cache automatically versions data with timestamps. Keep multiple versions for time-series analysis.

2. **Regular Cleanup**: Clear old cache versions periodically to save space:
   ```bash
   pbi cache clear -k workspaces -v <old_version>
   ```

3. **Offline Analysis**: Use `--cache-only` for analysis tasks to ensure you're working with a consistent snapshot:
   ```bash
   pbi workspaces list --cache-only
   ```

4. **Metadata**: The cache stores request metadata, making it easy to understand what parameters were used:
   ```json
   {
     "metadata": {
       "top": 1000,
       "expand": ["reports", "dashboards"],
       "filter": "state eq 'Active'"
     }
   }
   ```
