# BaluHost Plugin Market

Community plugin marketplace for [BaluHost](https://github.com/Xveyn/Baluhost). This repository is the upstream source for the plugin index that BaluHost installations fetch via the in-app **Marketplace** tab.

Every folder under `plugins/` is one publishable plugin. CI builds a signed `index.json` and per-plugin `.bhplugin` archives from the contents of this repo and publishes them to the BaluHost marketplace CDN.

## How plugins work

A BaluHost plugin is a Python package that runs in-process inside the BaluHost backend. It can expose API routes, background tasks, a dashboard panel, and a React UI bundle. Plugins are discovered from their **static manifest** (`plugin.json`) — BaluHost reads metadata without ever executing plugin code, so the marketplace listing is safe and cheap.

At install time BaluHost:

1. Resolves the plugin's declared Python dependencies against the locked Core environment.
2. Downloads the `.bhplugin` archive and verifies its SHA-256 checksum against `index.json`.
3. Installs any plugin-private dependencies into an isolated per-plugin `site-packages/` directory (pure-Python wheels only — no C extensions).
4. Extracts the plugin source to the external plugins directory (`/var/lib/baluhost/plugins/<name>/` in production) where it survives Core updates.
5. The user reviews the requested permissions and enables the plugin.

Plugins declare their compatibility window with `min_baluhost_version` / `max_baluhost_version`. After a Core update, BaluHost re-runs the resolver for every installed plugin and surfaces "broken by Core update" notifications so users can act before the plugin silently stops loading.

## Repository layout

```
BaluHost-Plugin-Market/
├── README.md                      # this file
└── plugins/
    ├── <plugin_name>/
    │   ├── plugin.json            # static manifest (required)
    │   ├── __init__.py            # PluginBase subclass (entrypoint)
    │   ├── requirements.txt       # pure-Python deps, pinned
    │   ├── ui/
    │   │   └── bundle.js          # optional frontend bundle
    │   └── ...                    # any additional Python modules
    └── ...
```

Each plugin folder is self-contained. CI packages it into `<plugin_name>-<version>.bhplugin` and adds a corresponding entry to `index.json`.

## Plugin manifest (`plugin.json`)

The manifest is the single source of truth for marketplace listings and dependency resolution. Minimal example:

```json
{
  "manifest_version": 1,
  "name": "weather_station",
  "version": "1.0.0",
  "display_name": "Weather Station",
  "description": "Pulls local weather and exposes a dashboard panel.",
  "author": "Jane Hobby",
  "homepage": "https://github.com/jane/baluhost-weather",
  "category": "monitoring",

  "min_baluhost_version": "1.30.0",
  "max_baluhost_version": null,

  "required_permissions": ["network:outbound", "system:info"],
  "python_requirements": [
    "pyowm==3.3.0",
    "tzdata>=2024.1"
  ],

  "entrypoint": "__init__.py",
  "ui": {
    "bundle": "ui/bundle.js",
    "styles": null
  }
}
```

All fields are validated by the `PluginManifest` schema in the BaluHost backend. See `backend/app/plugins/manifest.py` in the main repo for the authoritative schema.

## Dependency rules

Plugins may depend on two kinds of Python packages:

- **Shared (Core-provided)** — anything already bundled with BaluHost (`fastapi`, `sqlalchemy`, `pydantic`, `httpx`, `cryptography`, …). Declare these in `python_requirements` only as compatibility constraints; BaluHost will never install a second copy.
- **Isolated (plugin-private)** — anything else. Installed into the plugin's private `site-packages/` via `pip install --target` with `--only-binary=:all:`. **Pure-Python wheels only** — packages that require a C build step (`numpy`, `pillow`, `psycopg2`, …) are rejected by the validator. This is a deliberate trade-off for cross-architecture install reliability (x86_64 NAS, ARM Rock Pi).

If two installed plugins declare incompatible constraints on the same package, the resolver refuses the install and shows a human-readable conflict message in the UI.

## Submitting a plugin

1. Fork this repository.
2. Add a new folder under `plugins/<your_plugin_name>/` with the layout above.
3. Validate locally using the BaluHost SDK:
   ```bash
   baluhost-sdk validate plugins/<your_plugin_name>
   baluhost-sdk dry-install plugins/<your_plugin_name>
   ```
   `validate` checks the manifest schema and rejects forbidden dependencies. `dry-install` runs the full resolver against the current Core environment and prints the same conflict report the marketplace UI would show.
4. Open a pull request. CI re-runs both checks and refuses to merge on failure.

Once merged, the next CI build publishes a new `index.json` and your plugin becomes installable from the in-app Marketplace tab on every BaluHost instance.

## License

Each plugin folder carries its own license (see the plugin's own `LICENSE` file). The repository scaffolding and CI tooling are MIT-licensed.
