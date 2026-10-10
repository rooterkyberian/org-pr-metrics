# org-pr-metrics

Lifecycle metrics for merged pull requests in a GitHub organization:
time to first review, time to approval and time to merge in working hours,
first-review outcomes, review duration, and PR size.

## Requirements

- [uv](https://docs.astral.sh/uv/) runs the script and installs its dependencies from the inline script metadata.
- [gh](https://cli.github.com/), authenticated with read access to the organization's repositories and members.

## Usage

```bash
./org_pr_metrics.py prs --org <org> --days 90 --out pr_metrics.yaml
./org_pr_metrics.py prs --org <org> --days 90 --until 2025-10-09 --out pr_metrics_2025.yaml
./org_pr_metrics.py users --org <org>
```

`prs` prints percentile tables and PR size buckets, and writes one YAML record per merged PR.
`users` prints the timezone and holiday country resolved for each person in the organization.
`--help` on either command lists the remaining options.

## Working time

Durations count 09:00 to 17:00 on weekdays in the PR author's timezone, excluding public holidays in the author's country.
1d is 8 working hours.

The author's timezone comes from, in order: `org_pr_people.yaml`, the UTC offsets of their commits in the organization, and the geocoded profile location.

## People config

`org_pr_people.yaml` next to the script holds per-login overrides; `--people-config` points at another file.
Copy `org_pr_people.example.yaml` to start; `org_pr_people.yaml` is gitignored.

```yaml
people:
  octocat:
    tz: Europe/Warsaw
    country: PL
    aliases: [octocat-old]
```

`tz` and `country` override the resolved timezone and holiday country.
`aliases` lists other GitHub accounts of the same person.
PRs from an alias take the main login's timezone and country, and their `person` field holds the main login, so per-person counts treat both accounts as one person.
`users` prints one row per person.

The file is validated on load: an unknown key, or an alias listed twice or also given its own entry, stops the script with a validation error.
Environment variables do not override it.

## Search window

GitHub search matches merge dates by calendar day, so `--until 2025-10-09 --days 90` covers merges from 11 Jul to 9 Oct inclusive, 91 dates.

## PR size

Lines changed are additions plus deletions, excluding lock files by default.
`--ignore-path` adds globs, and `--no-default-ignores` counts lock files too.

## Cache

API responses are cached under `~/.cache/org-pr-metrics`.
`--clear-cache` deletes the cache before a run.
