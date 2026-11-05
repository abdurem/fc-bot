# Ubuntu deployment

On pull requests, GitHub Actions runs tests, validates Compose, and builds the image. On pushes to `main`, it also publishes a commit-tagged image to `ghcr.io/abdurem/fc-bot`. Once deployment is enabled, Actions connects to your server over SSH, pulls the exact image digest, and recreates the bot while keeping the SQLite volume. The image is private by default when first published; keep its package visibility private in GitHub.

## 1. Prepare the Ubuntu server

Install Docker Engine and the Compose plugin using the [official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/). The workflow currently builds for **amd64**; run `uname -m` on the server and confirm `x86_64`. If it says `aarch64`, adjust the image build for ARM before deployment.

Use an SSH user that can run Docker without a password prompt. Assuming the user is `deploy`:

```sh
sudo usermod -aG docker deploy
sudo install -d -m 700 -o deploy -g deploy /opt/fc-bot
```

Log out and back in so the Docker group applies. Docker group membership grants control of the host; use a dedicated deployment account.

Create `/opt/fc-bot/.env` **on the server**, owned by the deployment user with mode `600`:

```dotenv
BOT_TOKEN=your_actual_token
ALLOWED_CHAT_ID=your_group_id
```

`ALLOWED_CHAT_ID` is optional. Do not put the token in the workflow, image, or repository. Use an editor to enter it without placing it in shell history.

## 2. Set up GitHub Actions SSH access

Generate a separate CI deployment key on your own machine:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/fc_bot_deploy -C fc-bot-actions
```

Leave its passphrase empty for unattended CI. Append the contents of `fc_bot_deploy.pub` to the deployment user's `~/.ssh/authorized_keys` on the server. Store the **private** key only in the GitHub secret below. This key is for Actions → server; it is separate from your machine → GitHub key.

Get the server's SSH host public key from the server console (for example `/etc/ssh/ssh_host_ed25519_key.pub`) or collect it with `ssh-keyscan -H your-server` and verify its fingerprint through a trusted connection. For a custom SSH port use `ssh-keyscan -p PORT -H your-server`.

In [repository Actions settings](https://github.com/abdurem/fc-bot/settings/secrets/actions), add these **repository secrets**:

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | Server IP or hostname |
| `DEPLOY_USER` | SSH username, e.g. `deploy` |
| `DEPLOY_SSH_KEY` | Complete private deployment key |
| `DEPLOY_KNOWN_HOSTS` | Verified `known_hosts` line(s), including hostname/IP and key |

Add these **repository variables**:

| Variable | Value |
| --- | --- |
| `DEPLOY_PATH` | `/opt/fc-bot`, or your writable deployment directory |
| `DEPLOY_PORT` | SSH port, defaults to `22` |
| `DEPLOY_ENABLED` | Set to `true` only once the server and secrets are ready |

The deployment uses the `production` GitHub environment, which GitHub creates if it does not exist. Optional environment approval rules will pause each deployment for review.

## 3. Enable and run

Push the workflow to `main`. Tests and image publishing run even while `DEPLOY_ENABLED` is unset; deployment is skipped. The workflow uses GitHub's built-in short-lived `GITHUB_TOKEN` for private registry authentication, so no registry password or PAT is needed. If an existing GHCR package is not linked to this repo, grant the repo Actions access in the package settings.

Stop any previous bot instance using this token, including one on your own machine. Only one long-polling instance can run per token.

If there is an existing database on another host, back it up while the old bot is stopped and restore it to the production volume before first deployment. Production defaults to `fc-bot_league-data`; set `FC_BOT_VOLUME` in the server `.env` if reusing a differently named existing volume. Do not run two Compose projects against the same database. A default Compose deployment from a folder named `fc-bot` already uses this volume name and project name.

Set `DEPLOY_ENABLED=true`, then open **Actions → Test, publish and deploy → Run workflow** on `main`. Future pushes automatically test, publish, and deploy.

`up --wait` verifies that the container starts; it does not verify Telegram connectivity or a working conversation. After the first deployment, send `/start` in the group and check the logs on the server:

```sh
cd /opt/fc-bot
export FC_BOT_IMAGE=$(cat deployed-image.txt)
docker compose -p fc-bot -f compose.prod.yaml logs --tail=50
```

No inbound application port or domain is needed; the server requires outbound HTTPS to Telegram and GHCR, and SSH access from the GitHub runner.

## Recovery

If pulling fails, the old container remains running. A failure after container recreation may require manual recovery; there is no automatic rollback. Revert the broken application commit on `main` to test, publish, and deploy the previous code again. Database migrations are not automatically undone; keep database backups before schema changes.

Never run `docker compose down -v` unless you want to erase all match history. Registry credentials are temporary and removed after each deployment; manual private-image pulls outside CI need a separate GHCR login.
