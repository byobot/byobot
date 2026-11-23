# Contributing to byobot

Thanks for helping. Issues and pull requests are welcome, from forks or branches.

## Setup and tests

Layout, setup, tests and the contract-first rules are in [`AGENTS.md`](AGENTS.md). They apply to people and agents alike.

## Workflow

1. Fork the repository (or branch, if you have access) and create a branch named `type/short-description`, e.g. `fix/sync-tie-break`. Types: `feature`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `ci`, `build`, `style`, `revert`, `release`.
2. Make one logical change per commit, with a one-line lowercase message. Commits made by an agent end with its name in brackets, e.g. `fix tie-break on equal timestamps [claude]`.
3. Open a pull request against `main`: one summary line, at most three short bullets, one line on how you tested. CI must pass. Maintainers merge.

New exercises for the shipped library go through the same flow; see [`library/README.md`](library/README.md).

## Commit identity

The repository is public, so commit metadata is too. Use your GitHub noreply address, never a personal email:

```bash
git config user.email "ID+USERNAME@users.noreply.github.com"
```

Find yours under GitHub Settings > Emails, and turn on "Keep my email addresses private" and "Block command line pushes that expose my email".

## Privacy

byobot handles health data. Use only example data in fixtures, tests, screenshots and issues: device `Example iPhone`, hostname `example-laptop`, addresses in `192.0.2.0/24`, location id `example-gym`. Keep real names, device names, hostnames, IPs, places and health data out of code, commits, branch names, issues and pull requests.

## Security and conduct

Report vulnerabilities privately as described in [`SECURITY.md`](SECURITY.md), not in public issues. Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

Contributions are licensed under Apache-2.0, as section 5 of the [license](LICENSE) describes.
