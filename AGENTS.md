# AGENTS.md — HA Deploy app

Docker-based Home Assistant app (repository root). Not the HA config repo.

## Memories
Session-persistent context lives in `.opencode/memories/`. Each file is a
markdown note named by topic. When a topic is resolved, move the file to
`.opencode/memories/resolved/` rather than deleting it. Never store secrets.

## Working style
- One item at a time when asking Adrian to decide; wait for confirmation.
- Informational lists use plain numbers; choices use letter identifiers
  derived from the choice text.
- Smallest change necessary; preserve formatting.

## Secrets
If you detect secrets or credentials anywhere, stop and notify Adrian.
Never print a secret's value. Use env vars / app options, never inline.

## App conventions
- Base image: `ghcr.io/hassio-addons/base` (S6 overlay). `init: false`.
- Service registered via `rootfs/etc/s6-overlay/user-bundles.d/user/contents.d/<name>`.
- Ingress listens on the port in `config.yaml` (`ingress_port`).
- Host config is mapped at `/config` (`homeassistant_config:rw`).
