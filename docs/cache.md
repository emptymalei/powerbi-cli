# Cache (legacy)

!!! note "The data lake replaced the cache"

    The commands keep what they fetch in the [data lake](lake.md) now. This page is for
    anyone who still has entries in the older cache, and so that old links keep working.

## What changed

The older cache kept **one entry per command** (`workspaces`, `apps_user`,
`user_access_<user>`), whatever options the command was given and whoever was signed in.
`pbi workspaces list --use-cache --top 5` could therefore answer with the workspaces that
an earlier `--top 1000` call had fetched, or with those of another tenant.

The [data lake](lake.md) stores every answer under the request that produced it: the
endpoint, its parameters and the tenant. Use it with the same cache folder you already
configured:

```bash
pbi config set-cache-folder ~/PowerBI/cache    # the lake is ~/PowerBI/cache/lake
pbi workspaces list                            # asks the API, stores the answer
pbi workspaces list --use-cache                # reuses the stored answer
pbi lake ls                                    # see what is stored
```

Entries of the older cache are not read by the commands and are not migrated to the lake.
They stay in the cache folder until you remove them with `pbi cache clear`.

## The commands

`pbi cache list` and `pbi cache clear` work on the older layout only:

```bash
# List the cache keys, or the versions of one key
pbi cache list
pbi cache list -k workspaces

# Clear one version, all versions of a key, or the whole older cache
pbi cache clear -k workspaces -v 20240101_120000
pbi cache clear -k workspaces
pbi cache clear
```

`pbi cache clear` never touches the data lake, which is the `lake` folder in the same cache
folder: clearing the whole cache keeps it, and `lake` is refused as a cache key. To delete
old versions from the lake use [`pbi lake prune`](lake.md#look-into-the-lake-pbi-lake).

## The layout of the older cache

```text
cache_folder/
├── workspaces/
│   ├── 20240101_120000/
│   │   └── workspaces.json
│   └── 20240102_150000/
│       └── workspaces.json
├── apps_user/
│   └── 20240101_130000/
│       └── apps_user.json
└── lake/                        the data lake (not part of the older cache)
```

Each file holds `cache_key`, `cached_at`, `version`, `metadata` (the options of the call)
and `data` (the API response). To read an old entry from Python:

```python
from pbi_cli.cache import CacheManager

cache = CacheManager(cache_folder="~/PowerBI/cache")
entry = cache.load("workspaces", version="latest")
print(entry["cached_at"], entry["metadata"], len(entry["data"]["value"]))
```
