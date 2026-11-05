"""Deploy an already published private image, without transferring the bot token."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


def main():
    required = ('DEPLOY_HOST', 'DEPLOY_USER', 'DEPLOY_SSH_KEY', 'DEPLOY_KNOWN_HOSTS',
                'DEPLOY_PATH', 'DEPLOY_PORT', 'FC_BOT_IMAGE', 'GH_USER', 'GH_TOKEN')
    config = {key: os.environ.get(key, '').strip() for key in required}
    missing = [key for key, value in config.items() if not value]
    if missing:
        raise SystemExit('Missing deployment settings: ' + ', '.join(missing))
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', config['DEPLOY_HOST']) or config['DEPLOY_HOST'].startswith('-'):
        raise SystemExit('Invalid DEPLOY_HOST')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', config['DEPLOY_USER']):
        raise SystemExit('Invalid DEPLOY_USER')
    if not config['DEPLOY_PORT'].isdigit() or not 1 <= int(config['DEPLOY_PORT']) <= 65535:
        raise SystemExit('Invalid DEPLOY_PORT')
    if not config['DEPLOY_PATH'].startswith('/') or config['DEPLOY_PATH'] == '/':
        raise SystemExit('DEPLOY_PATH must be an absolute application directory')
    if not re.fullmatch(r'ghcr\.io/[a-z0-9_./-]+@sha256:[a-f0-9]{64}', config['FC_BOT_IMAGE']):
        raise SystemExit('Expected a GHCR image pinned by digest')

    with tempfile.TemporaryDirectory(prefix='fc-deploy-') as folder:
        key, hosts = Path(folder) / 'key', Path(folder) / 'known_hosts'
        key.write_text(config['DEPLOY_SSH_KEY'] + '\n')
        key.chmod(0o600)
        hosts.write_text(config['DEPLOY_KNOWN_HOSTS'] + '\n')
        ssh = ['ssh', '-F', '/dev/null', '-i', str(key), '-p', config['DEPLOY_PORT'],
               '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', f'UserKnownHostsFile={hosts}', '-o', 'ConnectTimeout=15',
               f"{config['DEPLOY_USER']}@{config['DEPLOY_HOST']}"]

        def remote(command, data=None):
            subprocess.run([*ssh, command], input=data, text=True, check=True, timeout=300)

        path = shlex.quote(config['DEPLOY_PATH'])
        # Check prerequisites before modifying the running deployment.
        remote(f'cd {path} && test -s .env && docker compose version >/dev/null && docker info >/dev/null')
        # The short-lived workflow token authenticates pulls of the private package.
        # Keep registry credentials in a temporary directory on the server.
        registry_dir = config['DEPLOY_PATH'] + '/.registry-' + os.urandom(8).hex()
        registry = shlex.quote(registry_dir)
        try:
            remote(f'umask 077; mkdir -p {registry}; docker --config {registry} login ghcr.io --username {shlex.quote(config["GH_USER"])} --password-stdin', config['GH_TOKEN'])
            destination = shlex.quote(config['DEPLOY_PATH'] + '/compose.prod.yaml')
            remote(f'umask 077; cat > {destination}', Path('compose.prod.yaml').read_text())
            image = shlex.quote(config['FC_BOT_IMAGE'])
            compose = 'docker compose -p fc-bot -f compose.prod.yaml'
            remote(f'''set -eu
cd {path}
export DOCKER_CONFIG={registry}
export FC_BOT_IMAGE={image}
{compose} pull
{compose} up -d --no-build --wait --wait-timeout 90
printf '%s\\n' "$FC_BOT_IMAGE" > deployed-image.txt
{compose} ps
''')
        finally:
            remote(f'rm -rf -- {registry}')


if __name__ == '__main__':
    main()
