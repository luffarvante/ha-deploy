# AGENTS.md — HA Deploy app

Docker-based Home Assistant app (repository root). Not the HA config repo.

## Remote access
**Never SSH into any server, under any circumstances, with no exceptions.** All
server-side inspection must come from Adrian (he runs the command and pastes the
output).

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

## Code style — Elm-style sum types

The typing aims to replicate Elm's algebraic data types in Python. The goal:
no `None`-checking anywhere, invalid states unrepresentable, exhaustive
handling checked at dev time. Modeled on the pattern in
`~/development/python/kl-spec-queue` (its `CLAUDE.md` "Code rules" and
`src/kl_spec_queue/*.py` are the reference).

- **Sum types via pydantic at runtime**: variants are `BaseModel`s, each with
  `kind: Literal["..."] = "..."` (unique value per variant), combined as
  `Type = Annotated[A | B, Field(discriminator="kind")]`. Fieldless variants
  are fieldless models. pydantic is a runtime dependency (installed in the
  Dockerfile), used to *decode* untrusted JSON at the boundary — parse, don't
  validate.
- **`match` everywhere**: every consumer of a sum type `match`es with explicit
  named variant cases. Combining variants inside one case is fine when the
  behavior is genuinely identical. Never `x.kind == ...` checks; never
  behavior-bearing `case _:` catch-alls. The only `case _:` allowed is the
  `assert_never(never)` exhaustiveness guard, which must be the closing case
  of every `match` over a sum type.
- **No `T | None = None` parameters** as a shortcut, **no `or fallback`**, and
  **no empty-string-as-absence** (`error: str` empty when ok is forbidden —
  absence gets its own variant or a real `None` state).
- **Fixed value sets → `StrEnum`/`IntEnum`**, never bare strings or magic
  numbers.
- **Exit codes are booleans in disguise**: don't carry an `int` exit code in a
  result type unless a consumer reads the value. stderr text and HTTP status
  codes that are actually consumed stay.
- **Nothing raises below the route's last-resort catch**: helpers and steps
  return sum types; failures are variants. The route catch is the final JSON
  guarantee only.
- **Multi-line type declarations get one variant per line.**
- **`pydantic.JsonValue`** for arbitrary JSON at the boundary, never `Any`.
- **Tooling**: basedpyright (`typeCheckingMode = "recommended"`,
  `reportMatchNotExhaustive = "error"`), no per-file relaxations, no
  `# type: ignore` / `# pyright: ignore` without Adrian's OK; ruff (Core's
  trimmed config) and pytest alongside. Everything type-checks: app code and
  tests alike.
