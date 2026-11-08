# HA Deploy

Minimal Home Assistant app for deploy/publish plumbing
(build on device, ingress UI, host config mapping, watchdog).

## Install
1. In Home Assistant: **Settings → Apps → Repositories**.
2. Add this repository's URL.
3. Install **HA Deploy** → Start.
4. Open **Deploy** from the sidebar (or mobile app).

## Setup (deploy key)
Before using status checks, copy the server's deploy key to `/share`:

1. On the HA server, copy the private key (e.g. `/data/.ssh/id_ed25519`)
   to `/share/ha-deploy-key`.
2. Restart the HA Deploy app. It loads the key into `/data/id_ed25519`
   (mode 600) and uses it for `git fetch`.

The deploy key is repository-scoped (only the HA config repo), so the app
cannot push to your other repos.

## Status check (v2026.2.0)
On page load the app runs `git fetch` + `git status` and shows a summary:
clean / only additions / only deletions / contains updates / untracked only /
diverged / behind origin / auth error. Use **Refresh** to re-run; the
"checked Xs ago" timer updates live.

## Test button
Click **Create test file**. This writes `/homeassistant/ha-deploy-test.txt` containing `OHAI`.
Verify the file appears in your HA config directory.

## Credit
Derived from [hassio-addons/addon-ssh](https://github.com/hassio-addons/addon-ssh) (MIT).
