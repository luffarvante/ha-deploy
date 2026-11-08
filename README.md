# HA Deploy

Minimal Home Assistant app used to validate deploy/publish plumbing
(build on device, ingress UI, host `/config` mapping, watchdog).

## Install
1. In Home Assistant: **Settings → Apps → Repositories**.
2. Add this repository's URL.
3. Install **HA Deploy** → Start.
4. Open **Deploy** from the sidebar (or mobile app).

## Usage (v0.1)
Click **Create test file**. This writes `/config/ha-deploy-test.txt` containing `OHAI`.
Verify the file appears in your HA config directory.

## Status
v0.1 is a hello-world. Real deploy/publish logic is planned for v0.2+.

## Credit
Derived from [hassio-addons/addon-ssh](https://github.com/hassio-addons/addon-ssh) (MIT).
