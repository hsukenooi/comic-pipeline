# Security Policy

## Supported versions

This is an actively developed monorepo; only the latest `main` receives security
updates.

## Reporting a vulnerability

Please do **not** report security vulnerabilities through public GitHub issues.

Instead, email **hsukenooi@googlemail.com** with a description of the
vulnerability and steps to reproduce it. You can expect an initial response
within a few days.

Several components handle credentials — `gixen-cli` stores `GIXEN_USERNAME` /
`GIXEN_PASSWORD` and `locg-cli` stores LOCG login/session cookies — so please
give a chance to issue a fix before disclosing publicly. Once the issue is
confirmed and a fix is available, disclosure will be coordinated with you.
