# Security policy

## Reporting a vulnerability

Please don't open a public issue. Report privately through GitHub: the repository's **Security** tab > **Report a vulnerability** ([direct link](https://github.com/byobot/byobot/security/advisories/new)).

Include what is affected, steps to reproduce and the impact you expect. Use example data only; never send real health data. We'll follow up in the advisory and credit you when a fix ships, unless you'd rather not be named.

## Supported versions

byobot is pre-alpha. Only the latest `main` is supported.

## Scope

Most useful are reports on:

- LAN sync (`byobot serve`): pairing, certificate pinning, unpairing, access without pairing
- the local store: escaping the read-only `query` command, corrupting the append-only event log
- the CLI and MCP server: injection through command arguments or JSON input
- the iPhone app: data stored or sent outside the device and the paired laptop

Bugs in upstream connectors belong with those projects.
