# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/codevexx/harvest/security/advisories/new) instead, with
steps to reproduce and the version you tested. You will get a reply within a few days.

## Scope

- The Windows app and installer in this repository
- The worker agent (`worker/`)
- The hosted service at harvest.codevexx.com, as far as it affects these components

## How updates are protected

The app only installs updates published as releases of this repository, and refuses any download whose
SHA-256 does not match the `.sha256` file released alongside it.
