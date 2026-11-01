# Security and data handling

Please use GitHub's **private vulnerability report** option (Security → Advisories) for security issues. Do not publish passwords, Star AutoLogin files, credentials or executable game assets in issues or pull requests.

Keep `~/.war3-dedicated/instance/`, AMP instance directories, `vendor/`, `data/`, `control/account-input.json`, saved Star profiles, Wine prefix, replay files, tokens, SSH keys and Docker socket access private. An AMP control container can access the host Docker socket and thus has effectively host-root privileges; restrict who can modify it.

The released source is a fresh, intentionally sanitized Git history. Historical internal evaluation records and proprietary artifacts are not part of this repository. Future contributors should run `python3 scripts/check-public-tree.py` before committing and inspect diffs, added files, metadata and logs manually. Automated checks cannot guarantee the absence of all secrets.
