# Security

This repository is a local research prototype. **Do not expose it to the public internet.**

- The default service is intended for `127.0.0.1` / a trusted LAN only.
- Pairing codes, bearer credentials and the public APK entry point are demo-grade, not a hardened authentication system.
- Never commit `.env`, API keys, real health data, databases, or device pairing credentials.
- Use synthetic or replay data for demos. The repository does not bundle real health data.
- If you find a security issue, report it through GitHub's private security advisory rather than a public issue, and do not include real user data in the report.

This software is provided "as is", without warranty of any kind. See [LICENSE](LICENSE).
