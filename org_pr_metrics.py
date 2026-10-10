#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "typer",
#     "click",
#     "rich",
#     "pydantic>=2",
#     "pydantic-settings[yaml]>=2.15",
#     "pyyaml",
#     "holidays",
#     "pandas",
#     "tabulate",
#     "geopy",
#     "timezonefinder",
# ]
# ///
"""Gather lifecycle metrics for merged PRs in a GitHub org."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self
from zoneinfo import ZoneInfo

import click
import holidays
import pandas as pd
import typer
import typer.rich_utils
import yaml
from geopy.geocoders import Nominatim
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from rich.console import Console
from tabulate import tabulate
from timezonefinder import TimezoneFinder

console = Console(stderr=True)
typer.rich_utils.STYLE_OPTIONS_PANEL_BORDER = "none"
typer.rich_utils.STYLE_COMMANDS_PANEL_BORDER = "none"
app = typer.Typer(no_args_is_help=True)
TABLE_FORMAT = "plain"

CACHE_DIR = Path.home() / ".cache" / "org-pr-metrics"
USER_CACHE_VERSION = 9
PR_CACHE_VERSION = 9
GEO_CACHE_VERSION = 2
FILES_CACHE_VERSION = 1
DEFAULT_PEOPLE_CONFIG = Path(__file__).with_name("org_pr_people.yaml")
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 17
WORKDAY_HOURS = float(WORKDAY_END_HOUR - WORKDAY_START_HOUR)
DEFAULT_TZ = "America/New_York"
DEFAULT_COUNTRY = "US"
GH_RATE_LIMIT_RETRIES = 5
GH_RATE_LIMIT_WAIT_S = 60
GH_TRANSIENT_WAIT_S = 10
GH_TRANSIENT_ERRORS = (
    "TLS handshake timeout",
    "i/o timeout",
    "connection reset",
    "unexpected EOF",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
)

BOT_LOGINS = frozenset(
    {
        "copilot-pull-request-reviewer",
        "copilot",
        "github-copilot",
        "github-actions",
        "dependabot",
        "renovate",
        "cursor",
    }
)


TZ_CANDIDATES: dict[str, str] = {
    "Europe/Warsaw": "PL",
    "Europe/London": "GB",
    "Europe/Kyiv": "UA",
    "Europe/Minsk": "BY",
    "Europe/Istanbul": "TR",
    "Asia/Tbilisi": "GE",
    "Asia/Dubai": "AE",
    "Asia/Kolkata": "IN",
    "America/New_York": "US",
    "America/Chicago": "US",
    "America/Denver": "US",
    "America/Los_Angeles": "US",
    "America/Phoenix": "US",
    "America/Sao_Paulo": "BR",
}

DEFAULT_SIZE_IGNORE_GLOBS = (
    "*.lock",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "bun.lockb",
    ".terraform.lock.hcl",
    "go.sum",
)

SIZE_BUCKETS: tuple[tuple[str, int], ...] = (
    ("XS", 10),
    ("S", 30),
    ("M", 100),
    ("L", 500),
    ("XL", 1000),
    ("XXL", 10**12),
)


def _cache_read(path: Path, version: int) -> dict | None:
    if not path.exists():
        return None
    entry = yaml.safe_load(path.read_text())
    if not entry or entry.get("_v") != version:
        return None
    return entry


def _cache_write(path: Path, version: int, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data["_v"] = version
    path.write_text(yaml.dump(data))


_geolocator = Nominatim(user_agent="gh-location-resolver")


class Person(BaseModel):
    """One login's entry in the people config."""

    model_config = ConfigDict(extra="forbid")

    tz: str | None = Field(default=None, description="IANA timezone override")
    country: str | None = Field(
        default=None, description="Holiday country override (ISO 3166 alpha-2)"
    )
    aliases: list[str] = Field(
        default_factory=list, description="Other GitHub logins of the same person"
    )


class PeopleConfig(BaseSettings):
    """Per-login overrides and aliases, read from the people YAML file."""

    model_config = SettingsConfigDict(extra="forbid")

    people: dict[str, Person] = Field(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, YamlConfigSettingsSource(settings_cls)

    @model_validator(mode="after")
    def _aliases_name_one_person(self) -> Self:
        seen: set[str] = set()
        for person in self.people.values():
            for alias in person.aliases:
                if alias in seen or alias in self.people:
                    raise ValueError(
                        f"alias {alias} is listed twice or also has its own entry"
                    )
                seen.add(alias)
        return self

    def person(self, login: str) -> Person:
        return self.people.get(login) or Person()

    def alias_map(self) -> dict[str, str]:
        """Return {alias login: main login}."""
        return {
            alias: login
            for login, person in self.people.items()
            for alias in person.aliases
        }


def load_people_config(path: Path) -> PeopleConfig:
    """Load the people config from a YAML file; a missing file gives an empty config."""

    class PeopleConfigFile(PeopleConfig):
        model_config = SettingsConfigDict(yaml_file=path)

    return PeopleConfigFile()


def fetch_user_infos(
    logins: set[str], org: str, people: PeopleConfig
) -> dict[str, dict]:
    """Return {login: user_info} for the given human logins."""
    info_map: dict[str, dict] = {}
    total = len(logins)
    cached_count = 0

    with console.status("") as status:
        for i, login in enumerate(sorted(logins), 1):
            if _cache_read(_user_cache_path(login), USER_CACHE_VERSION) is not None:
                cached_count += 1
            status.update(
                f"[bold green]Resolving timezones… {i}/{total} "
                f"({cached_count} cached)[/bold green] {login}"
            )
            info_map[login] = _fetch_user_info(login, org, people.person(login))

    by_country = Counter(u["country"] for u in info_map.values())
    console.print(
        f"Resolved [bold]{total}[/bold] people: "
        + ", ".join(f"{n} {c}" for c, n in by_country.most_common())
        + "."
    )
    return info_map


def _fetch_user_info(login: str, org: str, override: Person) -> dict:
    """Resolve a GitHub user's IANA timezone and holiday country.

    Timezone: people-config override, then the zone that best fits the user's
    commit offsets, then the zone at their geocoded profile location.
    Country: override, then the geocoded country, then the zone's country.
    """
    cache_path = _user_cache_path(login)
    cached = _cache_read(cache_path, USER_CACHE_VERSION)
    if cached is not None and not (override.tz or override.country):
        return cached

    result = subprocess.run(
        ["gh", "api", f"/users/{login}"],
        capture_output=True,
        text=True,
        check=False,
    )
    profile = json.loads(result.stdout) if result.returncode == 0 else {}
    name = (profile.get("name") or "").strip() or None
    location = (profile.get("location") or "").strip() or None
    geo_country, geo_tz = _geocode_location(location) if location else (None, None)

    tz, tz_method = override.tz, "config"
    if not tz:
        tz, tz_method = _infer_zone_from_commits(login, org), "commits"
    if not tz:
        tz, tz_method = geo_tz, "geocode"
    if not tz:
        console.print(
            f"[red]Error:[/red] could not determine a timezone for [bold]{login}[/bold] "
            f"(no usable commits or profile location). Add it to {DEFAULT_PEOPLE_CONFIG.name}."
        )
        raise SystemExit(1)

    country, country_method = override.country, "config"
    if not country:
        country, country_method = geo_country, "geocode"
    if not country:
        country, country_method = TZ_CANDIDATES.get(tz, DEFAULT_COUNTRY), "timezone"

    info = {
        "tz": tz,
        "tz_method": tz_method,
        "country": country,
        "country_method": country_method,
        "name": name,
        "location": location,
    }
    _cache_write(cache_path, USER_CACHE_VERSION, info)
    return info


def _geocode_location(raw: str) -> tuple[str | None, str | None]:
    """Geocode a free-text location and return (holiday country, IANA timezone)."""
    h = hashlib.blake2b(raw.lower().encode()).hexdigest()[:16]
    cache_path = CACHE_DIR / f"geo_{h}.yaml"
    cached = _cache_read(cache_path, GEO_CACHE_VERSION)
    if cached is not None:
        return cached.get("country"), cached.get("tz")

    loc = _geolocator.geocode(
        raw,
        language="en",
        addressdetails=True,
    )
    country = tz = None
    if loc:
        code = loc.raw.get("address", {}).get("country_code", "").upper()
        country = code if code in holidays.list_supported_countries() else None
        tz = _timezone_finder().timezone_at(lat=loc.latitude, lng=loc.longitude)

    _cache_write(cache_path, GEO_CACHE_VERSION, {"country": country, "tz": tz})
    return country, tz


@lru_cache(maxsize=1)
def _timezone_finder() -> TimezoneFinder:
    return TimezoneFinder()


def _infer_zone_from_commits(login: str, org: str) -> str | None:
    """Return the candidate IANA zone that matches most of the user's commit offsets.

    Commit search returns each author date with the offset of the committer's
    machine (e.g. +02:00 in CEST, +01:00 in CET), so checking every candidate
    zone's offset on each commit's date tells daylight-saving zones apart.
    Returns None with fewer than 3 commits or when no zone matches at least
    half of them.
    """
    result = _run_gh(
        [
            "gh",
            "api",
            f"search/commits?q=author:{login}+org:{org}+merge:false&sort=author-date&order=desc&per_page=100",
        ]
    )
    if result.returncode != 0:
        if "rate limit" in result.stderr:
            console.print(
                f"[red]Error:[/red] search API still rate limited for "
                f"[bold]{login}[/bold] after {GH_RATE_LIMIT_RETRIES} retries"
            )
            raise SystemExit(1)
        return None

    dates = [
        datetime.fromisoformat(author_date)
        for item in json.loads(result.stdout).get("items", [])
        if (author_date := item.get("commit", {}).get("author", {}).get("date", ""))
    ]
    if len(dates) < 3:
        return None

    scores = {
        zone: sum(
            1
            for dt in dates
            if dt.utcoffset() == dt.astimezone(ZoneInfo(zone)).utcoffset()
        )
        for zone in TZ_CANDIDATES
    }
    best = max(scores, key=scores.get)
    return best if scores[best] * 2 >= len(dates) else None


def _is_bot(login: str | float | None) -> bool:
    if not isinstance(login, str) or not login:
        return True
    return login.endswith("[bot]") or login.lower() in BOT_LOGINS


def _user_cache_path(login: str) -> Path:
    return CACHE_DIR / f"user_{login}.yaml"


class PRMetrics(BaseModel):
    """Lifecycle timestamps for a single pull request."""

    repo: str = Field(description="Full repository name (owner/repo)")
    number: int = Field(description="PR number within the repository")
    author: str | None = Field(
        default=None, description="GitHub login of the PR author"
    )
    person: str | None = Field(
        default=None,
        description=(
            "Main login of the PR author: the login the author is an alias of "
            "in the people config, else the author"
        ),
    )
    author_country: str = Field(
        default=DEFAULT_COUNTRY,
        description="Author's holiday country (ISO 3166 alpha-2)",
    )
    author_tz: str = Field(
        default=DEFAULT_TZ,
        description="Author's IANA timezone, e.g. Europe/Warsaw",
    )
    title: str = Field(description="PR title")
    url: str = Field(description="GitHub URL of the pull request")
    state: str = Field(description="Current state: OPEN, CLOSED, or MERGED")
    created_at: str = Field(
        description="ISO-8601 timestamp when the PR was created (may be as draft)"
    )
    ready_for_review_at: str | None = Field(
        description=(
            "ISO-8601 timestamp when the PR became ready for review. "
            "For PRs created as draft this is the ReadyForReviewEvent time; "
            "for PRs created directly as non-draft this equals created_at; "
            "null if the PR is still a draft"
        )
    )
    first_reviewed_at: str | None = Field(
        description=(
            "ISO-8601 time the first human review (APPROVED, CHANGES_REQUESTED or "
            "COMMENTED) was submitted; reviews by bots and by the author are "
            "excluded; null if never reviewed by a human"
        )
    )
    first_reviewer: str | None = Field(
        description="GitHub login of the first human reviewer; null if never reviewed"
    )
    first_review_started_at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 time of the first inline comment in the first human review; "
            "null when that review has no inline comments"
        ),
    )
    first_approved_at: str | None = Field(
        default=None,
        description="ISO-8601 time the first human approval was submitted; null if none",
    )
    first_approver: str | None = Field(
        default=None, description="GitHub login of the first human approver"
    )
    merged_at: str | None = Field(
        description="ISO-8601 timestamp when the PR was merged; null if not merged"
    )
    time_to_merge_bh: float | None = Field(
        default=None,
        description="Working hours from creation to merge, in the author's timezone",
    )
    time_to_first_review_bh: float | None = Field(
        default=None,
        description=(
            "Working hours from ready-for-review to the first human review "
            "being submitted; 0 when it came while the PR was a draft"
        ),
    )
    time_to_approval_bh: float | None = Field(
        default=None,
        description=(
            "Working hours from ready-for-review to the first human approval; "
            "0 when it came while the PR was a draft; null if never approved"
        ),
    )
    first_review_duration_min: float | None = Field(
        default=None,
        description=(
            "Clock minutes from the first inline comment of the first human review "
            "to its submission; null when that review has no inline comments"
        ),
    )
    files_changed: int | None = Field(
        default=None, description="Files changed, excluding ignored paths"
    )
    lines_changed: int | None = Field(
        default=None,
        description="Additions plus deletions, excluding ignored paths",
    )
    lines_changed_all: int | None = Field(
        default=None, description="Additions plus deletions across every file"
    )


def gh(args: list[str], *, paginate: bool = False) -> str:
    cmd = ["gh"]
    if paginate:
        cmd += ["api", "--paginate", *args]
    else:
        cmd += args
    result = _run_gh(cmd)
    if result.returncode != 0:
        console.print(f"[red]gh failed:[/red] {result.stderr.strip()}")
        raise SystemExit(1)
    return result.stdout


def _run_gh(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a gh command, retrying on rate limits and transient network errors."""
    for attempt in range(1, GH_RATE_LIMIT_RETRIES + 1):
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return result
        if "rate limit" in result.stderr:
            reason, wait_s = "GitHub rate limit hit", GH_RATE_LIMIT_WAIT_S
        elif any(err in result.stderr for err in GH_TRANSIENT_ERRORS):
            reason, wait_s = "Transient GitHub network error", GH_TRANSIENT_WAIT_S
        else:
            return result
        console.print(
            f"[yellow]{reason}, retry {attempt}/{GH_RATE_LIMIT_RETRIES} "
            f"in {wait_s}s…[/yellow]"
        )
        time.sleep(wait_s)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _search_window(org: str, start: datetime, end: datetime) -> list[dict]:
    date_range = f"{start:%Y-%m-%d}..{end:%Y-%m-%d}"
    raw = gh(
        [
            "search",
            "prs",
            "--owner",
            org,
            "--merged",
            "--merged-at",
            date_range,
            "--limit",
            "999",
            "--json",
            "number,title,repository,createdAt,url,state,author",
        ]
    )
    return json.loads(raw) if raw.strip() else []


def search_prs(org: str, since: datetime, until: datetime) -> list[dict]:
    """Search merged PRs, auto-splitting windows that hit the 1000-result cap."""
    all_prs: list[dict] = []
    queue: list[tuple[datetime, datetime]] = []

    window_start = since
    window_size = timedelta(days=7)
    while window_start < until:
        window_end = min(window_start + window_size, until)
        queue.append((window_start, window_end))
        window_start = window_end

    with console.status("[bold green]Searching merged PRs…") as status:
        while queue:
            start, end = queue.pop(0)
            date_range = f"{start:%Y-%m-%d}..{end:%Y-%m-%d}"
            status.update(f"[bold green]Searching PRs merged {date_range}…")

            batch = _search_window(org, start, end)

            if len(batch) >= 999 and (end - start) > timedelta(days=1):
                mid = start + (end - start) / 2
                queue.insert(0, (start, mid))
                queue.insert(1, (mid, end))
                console.print(
                    f"[yellow]Splitting:[/yellow] {date_range} hit 999 results, "
                    f"bisecting into two windows"
                )
            else:
                all_prs.extend(batch)
                if len(batch) >= 999:
                    console.print(
                        f"[yellow]Warning:[/yellow] {date_range} hit 999 results "
                        "on a single day — some PRs may be missing"
                    )

    seen: set[str] = set()
    deduped = []
    for pr in all_prs:
        if pr["url"] not in seen:
            seen.add(pr["url"])
            deduped.append(pr)

    console.print(f"Found [bold]{len(deduped)}[/bold] merged PRs total.")
    return deduped


PR_DETAILS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      isDraft
      mergedAt
      reviews(first: 50, states: [APPROVED, CHANGES_REQUESTED, COMMENTED]) {
        nodes {
          state
          createdAt
          submittedAt
          author { login }
          comments(first: 1) { nodes { createdAt } }
        }
      }
      timelineItems(first: 1, itemTypes: [READY_FOR_REVIEW_EVENT]) {
        nodes {
          ... on ReadyForReviewEvent { createdAt }
        }
      }
    }
  }
}
"""


PR_FILES_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      files(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { path additions deletions }
      }
    }
  }
}
"""


def _parse_owner_repo(pr: dict) -> tuple[str, str]:
    owner, repo = pr["repository"]["nameWithOwner"].split("/", 1)
    return owner, repo


def _pr_cache_path(url: str) -> Path:
    h = hashlib.blake2b(url.encode()).hexdigest()[:16]
    return CACHE_DIR / f"pr_{h}.yaml"


def _fetch_single_pr_details(pr: dict) -> dict:
    owner, repo = _parse_owner_repo(pr)
    number = pr["number"]
    url = pr["url"]
    pr_author = (pr.get("author") or {}).get("login")

    cached = _cache_read(_pr_cache_path(url), PR_CACHE_VERSION)
    if cached is not None:
        return cached

    raw = gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={PR_DETAILS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"number={number}",
        ]
    )
    data = json.loads(raw)
    pr_data = data.get("data", {}).get("repository", {}).get("pullRequest", {})

    review_nodes = pr_data.get("reviews", {}).get("nodes", [])
    timeline_nodes = pr_data.get("timelineItems", {}).get("nodes", [])

    ready_for_review_at = None
    if timeline_nodes and timeline_nodes[0].get("createdAt"):
        ready_for_review_at = timeline_nodes[0]["createdAt"]
    elif not pr_data.get("isDraft", False):
        ready_for_review_at = pr["createdAt"]

    all_reviewers = [
        (review.get("author") or {}).get("login") for review in review_nodes
    ]

    human_reviews = sorted(
        (
            review
            for review in review_nodes
            if review.get("submittedAt")
            and not _is_bot((review.get("author") or {}).get("login"))
            and (review.get("author") or {}).get("login") != pr_author
        ),
        key=lambda review: review["submittedAt"],
    )
    first = human_reviews[0] if human_reviews else None
    first_comment_times = [
        c["createdAt"] for c in ((first or {}).get("comments") or {}).get("nodes", [])
    ]
    approval = next((r for r in human_reviews if r["state"] == "APPROVED"), None)

    result = {
        "merged_at": pr_data.get("mergedAt"),
        "first_reviewed_at": first["submittedAt"] if first else None,
        "first_reviewer": first["author"]["login"] if first else None,
        "first_review_started_at": (
            min(first["createdAt"], *first_comment_times)
            if first_comment_times
            else None
        ),
        "first_approved_at": approval["submittedAt"] if approval else None,
        "first_approver": approval["author"]["login"] if approval else None,
        "ready_for_review_at": ready_for_review_at,
        "all_reviewers": all_reviewers,
    }

    _cache_write(_pr_cache_path(url), PR_CACHE_VERSION, result)
    return result


def fetch_pr_details(prs: list[dict]) -> dict[str, dict]:
    """Return {url: {merged_at, first_reviewed_at, ready_for_review_at}} per PR."""
    results: dict[str, dict] = {}
    total = len(prs)
    cached_count = 0

    with console.status("") as status:
        for i, pr in enumerate(prs, 1):
            owner, repo = _parse_owner_repo(pr)
            url = pr["url"]

            is_cached = _cache_read(_pr_cache_path(url), PR_CACHE_VERSION) is not None
            if is_cached:
                cached_count += 1

            status.update(
                f"[bold green]Fetching details… {i}/{total} "
                f"({cached_count} cached)[/bold green] "
                f"{owner}/{repo}#{pr['number']}"
            )

            results[url] = _fetch_single_pr_details(pr)

    if cached_count:
        console.print(
            f"Used [bold]{cached_count}[/bold] cached PR details, "
            f"fetched {total - cached_count} from API."
        )

    return results


def fetch_pr_files(prs: list[dict]) -> dict[str, list[list]]:
    """Return {url: [[path, additions, deletions], ...]} per PR."""
    results: dict[str, list[list]] = {}
    total = len(prs)
    cached_count = 0

    with console.status("") as status:
        for i, pr in enumerate(prs, 1):
            owner, repo = _parse_owner_repo(pr)
            url = pr["url"]
            if _cache_read(_pr_files_cache_path(url), FILES_CACHE_VERSION) is not None:
                cached_count += 1
            status.update(
                f"[bold green]Fetching changed files… {i}/{total} "
                f"({cached_count} cached)[/bold green] "
                f"{owner}/{repo}#{pr['number']}"
            )
            results[url] = _fetch_single_pr_files(pr)

    if cached_count:
        console.print(
            f"Used [bold]{cached_count}[/bold] cached file lists, "
            f"fetched {total - cached_count} from API."
        )
    return results


def summarize_ignored(
    files_map: dict[str, list[list]], ignore_globs: Sequence[str]
) -> Counter[str]:
    """Return lines changed per ignore glob, crediting each file to its first match."""
    ignored: Counter[str] = Counter()
    for files in files_map.values():
        for path, additions, deletions in files:
            glob = _matching_glob(path, ignore_globs)
            if glob is not None:
                ignored[glob] += additions + deletions
    return ignored


def _pr_files_cache_path(url: str) -> Path:
    h = hashlib.blake2b(url.encode()).hexdigest()[:16]
    return CACHE_DIR / f"files_{h}.yaml"


def _fetch_single_pr_files(pr: dict) -> list[list]:
    url = pr["url"]
    cached = _cache_read(_pr_files_cache_path(url), FILES_CACHE_VERSION)
    if cached is not None:
        return cached["files"]

    owner, repo = _parse_owner_repo(pr)
    raw = gh(
        [
            "graphql",
            "-f",
            f"query={PR_FILES_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"number={pr['number']}",
            "--jq",
            "(.data.repository.pullRequest.files.nodes // [])[]"
            " | [.path, .additions, .deletions] | @json",
        ],
        paginate=True,
    )
    files = [json.loads(line) for line in raw.splitlines() if line.strip()]
    _cache_write(_pr_files_cache_path(url), FILES_CACHE_VERSION, {"files": files})
    return files


def _matching_glob(path: str, ignore_globs: Sequence[str]) -> str | None:
    """Return the first glob matching the full path or the file name, else None."""
    name = path.rsplit("/", 1)[-1]
    for glob in ignore_globs:
        if fnmatch.fnmatchcase(path, glob) or fnmatch.fnmatchcase(name, glob):
            return glob
    return None


def _pr_size(files: list[list], ignore_globs: Sequence[str]) -> tuple[int, int, int]:
    """Return (files kept, lines kept, lines across every file) for one PR."""
    kept = [
        (additions, deletions)
        for path, additions, deletions in files
        if _matching_glob(path, ignore_globs) is None
    ]
    return (
        len(kept),
        sum(a + d for a, d in kept),
        sum(a + d for _, a, d in files),
    )


def _default_user_info() -> dict:
    return {"country": DEFAULT_COUNTRY, "tz": DEFAULT_TZ}


def enrich_prs(
    prs: list[dict],
    details_map: dict[str, dict],
    user_info: dict[str, dict],
    files_map: dict[str, list[list]],
    ignore_globs: Sequence[str],
    aliases: dict[str, str],
) -> list[PRMetrics]:
    enriched = []
    for pr in prs:
        owner, repo = _parse_owner_repo(pr)
        details = details_map.get(pr["url"], {})
        files_kept, lines_kept, lines_all = _pr_size(
            files_map.get(pr["url"], []), ignore_globs
        )
        author_login = (pr.get("author") or {}).get("login")
        person = aliases.get(author_login, author_login)
        author = user_info.get(person) or _default_user_info()
        enriched.append(
            PRMetrics(
                repo=f"{owner}/{repo}",
                number=pr["number"],
                author=author_login,
                person=person,
                author_country=author["country"],
                author_tz=author["tz"],
                title=pr["title"],
                url=pr["url"],
                state=pr["state"],
                created_at=pr["createdAt"],
                ready_for_review_at=details.get("ready_for_review_at"),
                first_reviewed_at=details.get("first_reviewed_at"),
                first_reviewer=details.get("first_reviewer"),
                first_review_started_at=details.get("first_review_started_at"),
                first_approved_at=details.get("first_approved_at"),
                first_approver=details.get("first_approver"),
                merged_at=details.get("merged_at"),
                files_changed=files_kept,
                lines_changed=lines_kept,
                lines_changed_all=lines_all,
            )
        )
    return enriched


@lru_cache(maxsize=64)
def _holiday_set(year: int, country: str) -> set[date]:
    return set(holidays.country_holidays(country, years=year).keys())


def _is_non_workday(d: date, country: str) -> bool:
    return d.weekday() >= 5 or d in _holiday_set(d.year, country)


def _business_hours(
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
    country: str,
    tz: str,
) -> float | None:
    """Count working hours between two timestamps.

    Only time inside WORKDAY_START_HOUR–WORKDAY_END_HOUR local time on the
    zone's workdays counts; weekends and the country's public holidays count
    as zero. Daylight-saving changes follow the IANA zone. Returns 0.0 when
    end is not after start, and None when either timestamp is missing.
    """
    if pd.isna(start) or pd.isna(end):
        return None
    if end <= start:
        return 0.0

    zone = ZoneInfo(tz)
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    total = 0.0
    day = local_start.date()
    while day <= local_end.date():
        if not _is_non_workday(day, country):
            opens = datetime(
                day.year, day.month, day.day, WORKDAY_START_HOUR, tzinfo=zone
            )
            closes = datetime(
                day.year, day.month, day.day, WORKDAY_END_HOUR, tzinfo=zone
            )
            overlap = (min(local_end, closes) - max(local_start, opens)).total_seconds()
            total += max(overlap, 0.0) / 3600
        day += timedelta(days=1)
    return total


def _format_minutes(minutes: float) -> str:
    if pd.isna(minutes):
        return "—"
    hours, mins = divmod(int(round(minutes)), 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def _format_biz_hours(hours: float) -> str:
    if pd.isna(hours):
        return "—"
    if hours < 1:
        return f"{int(hours * 60)}m"
    days, remainder_h = divmod(hours, WORKDAY_HOURS)
    d = int(days)
    h = int(remainder_h)
    m = int((remainder_h - h) * 60)
    if d:
        return f"{d}d {h}h {m}m"
    return f"{h}h {m}m"


TIMESTAMP_COLUMNS = (
    "created_at",
    "ready_for_review_at",
    "first_reviewed_at",
    "first_review_started_at",
    "first_approved_at",
    "merged_at",
)


def build_dataframe(prs: list[PRMetrics]) -> pd.DataFrame:
    df = pd.DataFrame([pr.model_dump() for pr in prs])

    for col in TIMESTAMP_COLUMNS:
        df[col] = pd.to_datetime(df[col], utc=True)

    def author_hours(start: str, end: str) -> pd.Series:
        return df.apply(
            lambda r: _business_hours(
                r[start], r[end], r["author_country"], r["author_tz"]
            ),
            axis=1,
        )

    df["time_to_merge_bh"] = author_hours("created_at", "merged_at")
    df["time_to_first_review_bh"] = author_hours(
        "ready_for_review_at", "first_reviewed_at"
    )
    df["time_to_approval_bh"] = author_hours("ready_for_review_at", "first_approved_at")
    df["first_review_duration_min"] = (
        df["first_reviewed_at"] - df["first_review_started_at"]
    ).dt.total_seconds() / 60
    df["first_review_approved"] = (
        (df["first_reviewed_at"] == df["first_approved_at"])
        & (df["first_reviewer"] == df["first_approver"])
    ).where(df["first_reviewed_at"].notna())

    df = df.sort_values("merged_at", ascending=False).reset_index(drop=True)
    return df


def display_sample(df: pd.DataFrame, n: int = 5) -> None:
    hour_columns = {
        "time_to_merge_bh": "TTM",
        "time_to_first_review_bh": "TTR",
        "time_to_approval_bh": "TTA",
    }
    sample = df.head(n)[
        [
            "url",
            "author",
            "author_country",
            "author_tz",
            "first_reviewer",
            "created_at",
            "merged_at",
            *hour_columns,
            "first_review_duration_min",
        ]
    ].copy()
    all_dates = pd.concat([sample["created_at"].dropna(), sample["merged_at"].dropna()])
    same_year = all_dates.dt.year.nunique() == 1
    date_fmt = "%m-%d %H:%M" if same_year else "%Y-%m-%d %H:%M"
    sample["created_at"] = sample["created_at"].dt.strftime(date_fmt)
    sample["merged_at"] = sample["merged_at"].dt.strftime(date_fmt)
    for col in hour_columns:
        sample[col] = sample[col].map(_format_biz_hours)
    sample["first_review_duration_min"] = sample["first_review_duration_min"].map(
        _format_minutes
    )

    sample = sample.rename(
        columns={
            "url": "PR",
            "author": "Author",
            "author_country": "CC",
            "author_tz": "TZ",
            "first_reviewer": "1st Reviewer",
            "created_at": "Created",
            "merged_at": "Merged",
            **hour_columns,
            "first_review_duration_min": "Review",
        }
    )

    click.echo(f"\nSample (most recent {n} PRs):")
    click.echo(
        tabulate(
            sample.values.tolist(),
            headers=sample.columns.tolist(),
            tablefmt=TABLE_FORMAT,
        )
    )


def _quantile_row(series: pd.Series, label: str) -> list | None:
    series = series.dropna()
    if series.empty:
        return None
    dist = series.quantile([0.50, 0.75, 0.90])
    return [
        label,
        int(series.count()),
        _format_biz_hours(dist[0.50]),
        _format_biz_hours(dist[0.75]),
        _format_biz_hours(dist[0.90]),
    ]


def display_stats(df: pd.DataFrame) -> None:
    stat_headers = ["Metric", "n", "p50", "p75", "p90"]
    note = (
        f"({WORKDAY_START_HOUR:02d}:00–{WORKDAY_END_HOUR:02d}:00 local on workdays, "
        "excl. weekends & public holidays; 1d = "
        f"{WORKDAY_HOURS:g}h)"
    )

    repo_rows: list[list] = []
    quantiles = [0.50, 0.75, 0.90]
    repo_sort_key: dict[str, float] = {}
    repo_data: dict[str, list[list]] = {}
    for repo, group in df.groupby("repo"):
        short = repo.split("/", 1)[1]
        ttr = group["time_to_first_review_bh"].dropna()
        repo_sort_key[short] = ttr.quantile(0.90) if len(ttr) >= 3 else 0.0
        repo_data[short] = []
        for col, metric in [
            ("time_to_merge_bh", "TTM"),
            ("time_to_first_review_bh", "TTR"),
            ("time_to_approval_bh", "TTA"),
        ]:
            series = group[col].dropna()
            if series.empty:
                continue
            dist = series.quantile(quantiles)
            repo_data[short].append(
                [
                    short,
                    metric,
                    int(series.count()),
                    _format_biz_hours(dist[0.50]),
                    _format_biz_hours(dist[0.75]),
                    _format_biz_hours(dist[0.90]),
                ]
            )
    for short in sorted(repo_sort_key, key=repo_sort_key.get, reverse=True):
        repo_rows.extend(repo_data[short])

    if repo_rows:
        click.echo(f"\nPer-repository business hours {note}:")
        click.echo(
            tabulate(
                repo_rows,
                headers=["Repo", "Metric", "n", "p50", "p75", "p90"],
                tablefmt=TABLE_FORMAT,
            )
        )

    rows = []
    for col, label in [
        ("time_to_merge_bh", "Time to merge (created → merged)"),
        ("time_to_first_review_bh", "Time to first review (ready → submitted)"),
        ("time_to_approval_bh", "Time to approval (ready → first approval)"),
    ]:
        row = _quantile_row(df[col], label)
        if row:
            rows.append(row)

    click.echo(f"\nOverall distribution — business hours {note}:")
    hdrs = stat_headers
    click.echo(tabulate(rows, headers=hdrs, tablefmt=TABLE_FORMAT))
    reviewed = df["first_reviewed_at"].notna().sum()
    unapproved = (
        df["first_reviewed_at"].notna() & df["first_approved_at"].isna()
    ).sum()
    first_approved = int(df["first_review_approved"].eq(True).sum())
    click.echo(
        f"Times use the PR author's timezone and holidays. {unapproved} of {reviewed} "
        "reviewed PRs merged without a human approval and have no TTA. "
        f"The first human review was an approval on {first_approved} of {reviewed} "
        f"reviewed PRs ({first_approved / max(reviewed, 1):.0%})."
    )

    duration = df["first_review_duration_min"].dropna()
    if duration.empty:
        return
    dist = duration.quantile([0.50, 0.75, 0.90])
    click.echo(
        "\nFirst human review duration — clock time, first inline comment → submitted:"
    )
    click.echo(
        tabulate(
            [
                [
                    "First review duration",
                    int(duration.count()),
                    *(_format_minutes(dist[q]) for q in (0.50, 0.75, 0.90)),
                ]
            ],
            headers=stat_headers,
            tablefmt=TABLE_FORMAT,
        )
    )
    click.echo(
        f"Covers the {len(duration)} first reviews ({len(duration) / max(reviewed, 1):.0%}) "
        f"with inline comments. {(duration < 1).sum()} were submitted within a minute "
        f"of their first comment; {(duration > 8 * 60).sum()} span more than 8 hours, "
        "which includes breaks. GitHub does not record reading time before the first comment."
    )


def display_size_stats(
    df: pd.DataFrame, ignore_globs: Sequence[str], ignored: Counter[str]
) -> None:
    """Print the PR size distribution, size buckets, ignored lines and largest PRs."""
    lines = df["lines_changed"].dropna()
    if lines.empty:
        return
    all_lines = int(df["lines_changed_all"].sum())

    click.echo(
        f"\nPR size — lines changed (additions + deletions) across {len(lines)} "
        f"merged PRs, ignoring: {', '.join(ignore_globs) or 'nothing'}"
    )
    dist = lines.quantile([0.50, 0.75, 0.90, 0.99])
    click.echo(
        tabulate(
            [
                [
                    "Lines changed",
                    int(lines.count()),
                    *(int(dist[q]) for q in (0.50, 0.75, 0.90, 0.99)),
                    int(lines.max()),
                ],
                [
                    "Files changed",
                    int(df["files_changed"].count()),
                    *(
                        int(df["files_changed"].quantile(q))
                        for q in (0.50, 0.75, 0.90, 0.99)
                    ),
                    int(df["files_changed"].max()),
                ],
            ],
            headers=["Metric", "n", "p50", "p75", "p90", "p99", "max"],
            tablefmt=TABLE_FORMAT,
        )
    )

    rows = []
    lower = 0
    for label, upper in SIZE_BUCKETS:
        in_bucket = lines[(lines >= lower) & (lines < upper)]
        bounds = f"{lower}+" if upper >= 10**12 else f"{lower}–{upper - 1}"
        rows.append(
            [
                label,
                bounds,
                len(in_bucket),
                f"{len(in_bucket) / len(lines):.0%}",
                f"{in_bucket.sum() / max(lines.sum(), 1):.0%}",
            ]
        )
        lower = upper
    click.echo("\nSize buckets:")
    click.echo(
        tabulate(
            rows,
            headers=["Size", "Lines", "PRs", "% of PRs", "% of lines"],
            tablefmt=TABLE_FORMAT,
        )
    )

    if ignored:
        click.echo(
            f"\nIgnored {sum(ignored.values()):,} of {all_lines:,} lines changed:"
        )
        click.echo(
            tabulate(
                [[glob, f"{n:,}"] for glob, n in ignored.most_common()],
                headers=["Glob", "Lines"],
                tablefmt=TABLE_FORMAT,
            )
        )

    largest = df.nlargest(5, "lines_changed")
    click.echo("\nLargest PRs after ignored paths:")
    click.echo(
        tabulate(
            [
                [
                    r["url"],
                    r["author"],
                    f"{int(r['lines_changed']):,}",
                    r["files_changed"],
                ]
                for _, r in largest.iterrows()
            ],
            headers=["PR", "Author", "Lines", "Files"],
            tablefmt=TABLE_FORMAT,
        )
    )


def write_yaml(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out_df = df.copy()
    for col in TIMESTAMP_COLUMNS:
        out_df[col] = out_df[col].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out_df = out_df.astype(object).where(out_df.notna(), None)
    data = out_df.to_dict(orient="records")
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    )
    console.print(f"\nWrote [bold]{len(data)}[/bold] PRs to [cyan]{path}[/cyan]")


def _clear_cache_if_requested(clear_cache: bool) -> None:
    if clear_cache and CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        console.print("[yellow]Cache cleared.[/yellow]")


def _fetch_org_members(org: str) -> list[str]:
    """Return login names of all members in a GitHub org."""
    raw = gh(
        [
            "api",
            "--paginate",
            f"/orgs/{org}/members?per_page=100",
        ],
        paginate=True,
    )
    members = json.loads(raw) if raw.strip() else []
    return sorted(m["login"] for m in members if not _is_bot(m.get("login")))


@app.command()
def users(
    org: Annotated[str, typer.Option(help="GitHub org to scan")],
    out: Annotated[Path, typer.Option(help="Output YAML file path")] = Path(
        "user_info.yaml"
    ),
    clear_cache: Annotated[
        bool,
        typer.Option("--clear-cache", help="Clear user cache before running"),
    ] = False,
    login: Annotated[
        str,
        typer.Option(help="Specific logins to fetch (comma-separated)"),
    ] = "",
    people_config: Annotated[
        Path,
        typer.Option(help="YAML with per-login timezone/country overrides and aliases"),
    ] = DEFAULT_PEOPLE_CONFIG,
) -> None:
    """Fetch user info (location, timezone detection) for org members."""
    _clear_cache_if_requested(clear_cache)
    people = load_people_config(people_config)
    aliases = people.alias_map()

    if login:
        logins = [s.strip() for s in login.split(",") if s.strip()]
    else:
        console.print(f"Fetching members of [bold]{org}[/bold]…")
        logins = _fetch_org_members(org)
        console.print(f"Found [bold]{len(logins)}[/bold] members.")
    persons = sorted({aliases.get(user_login, user_login) for user_login in logins})

    results: list[dict] = []
    with console.status("") as status:
        for i, person in enumerate(persons, 1):
            status.update(
                f"[bold green]Resolving user info… {i}/{len(persons)}[/bold green] "
                f"{person}"
            )
            entry = people.person(person)
            info = _fetch_user_info(person, org, entry)
            results.append(
                {
                    "login": person,
                    "aliases": entry.aliases,
                    "name": info.get("name"),
                    "location": info.get("location"),
                    "country": info["country"],
                    "country_method": info["country_method"],
                    "tz": info["tz"],
                    "tz_method": info["tz_method"],
                }
            )

    by_country = Counter(r["country"] for r in results)
    console.print(
        f"\nResolved [bold]{len(results)}[/bold] people: "
        + ", ".join(f"{n} {c}" for c, n in by_country.most_common())
    )

    by_method: dict[str, int] = {}
    for r in results:
        by_method[r["tz_method"]] = by_method.get(r["tz_method"], 0) + 1
    method_parts = [f"{m}: {c}" for m, c in sorted(by_method.items())]
    console.print(f"Detection methods: {', '.join(method_parts)}")

    headers = [
        "Login",
        "Aliases",
        "Name",
        "Location",
        "Country",
        "TZ",
        "TZ from",
        "Country from",
    ]
    rows = [
        [
            r["login"],
            ", ".join(r["aliases"]) or "—",
            r["name"] or "—",
            r["location"] or "—",
            r["country"],
            r["tz"],
            r["tz_method"],
            r["country_method"],
        ]
        for r in results
    ]
    click.echo("\n" + tabulate(rows, headers=headers, tablefmt=TABLE_FORMAT))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        yaml.dump(
            results, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
    )
    console.print(f"\nWrote [bold]{len(results)}[/bold] people to [cyan]{out}[/cyan]")


@app.command()
def prs(
    org: Annotated[str, typer.Option(help="GitHub org to scan")],
    days: Annotated[int, typer.Option(help="How many days back to search")] = 90,
    until: Annotated[
        datetime | None,
        typer.Option(
            formats=["%Y-%m-%d"],
            help="Last day of the window (UTC); defaults to now",
        ),
    ] = None,
    out: Annotated[Path, typer.Option(help="Output YAML file path")] = Path(
        "pr_metrics.yaml"
    ),
    clear_cache: Annotated[
        bool,
        typer.Option("--clear-cache", help="Clear PR details cache before running"),
    ] = False,
    sample_n: Annotated[
        int,
        typer.Option("--sample-n", help="Number of recent PRs to show in sample table"),
    ] = 5,
    repo: Annotated[
        str,
        typer.Option(
            help="Only include these repos (comma-separated, short or owner/repo)"
        ),
    ] = "",
    without_repo: Annotated[
        str,
        typer.Option(help="Exclude these repos (comma-separated, short or owner/repo)"),
    ] = "",
    ignore_path: Annotated[
        list[str] | None,
        typer.Option(
            "--ignore-path",
            help=(
                "Glob of file paths to leave out of PR size; repeatable. Matched "
                "against the full path and the file name; * also matches /"
            ),
        ),
    ] = None,
    no_default_ignores: Annotated[
        bool,
        typer.Option(
            "--no-default-ignores",
            help=f"Count lock files and the rest of: {', '.join(DEFAULT_SIZE_IGNORE_GLOBS)}",
        ),
    ] = False,
    people_config: Annotated[
        Path,
        typer.Option(help="YAML with per-login timezone/country overrides and aliases"),
    ] = DEFAULT_PEOPLE_CONFIG,
) -> None:
    """Gather lifecycle metrics for merged PRs in a GitHub org."""
    _clear_cache_if_requested(clear_cache)

    now = until.replace(tzinfo=UTC) if until else datetime.now(UTC)
    since = now - timedelta(days=days)

    prs_data = search_prs(org, since, now)
    if not prs_data:
        console.print("[yellow]No PRs found.[/yellow]")
        raise SystemExit(0)

    ignore_globs = [
        *(() if no_default_ignores else DEFAULT_SIZE_IGNORE_GLOBS),
        *(ignore_path or []),
    ]
    details_map = fetch_pr_details(prs_data)
    people = load_people_config(people_config)
    aliases = people.alias_map()
    authors = {(pr.get("author") or {}).get("login") for pr in prs_data}
    user_info = fetch_user_infos(
        {aliases.get(login, login) for login in authors if not _is_bot(login)},
        org,
        people,
    )
    files_map = fetch_pr_files(prs_data)
    enriched = enrich_prs(
        prs_data, details_map, user_info, files_map, ignore_globs, aliases
    )
    df = build_dataframe(enriched)

    def _normalize_repos(raw: str) -> set[str]:
        return {r if "/" in r else f"{org}/{r}" for r in raw.split(",") if r.strip()}

    if repo:
        include = _normalize_repos(repo)
        included = df["repo"].isin(include)
        excluded_n = (~included).sum()
        if excluded_n:
            console.print(
                f"[yellow]Limiting to {included.sum()} PRs "
                f"in repos: {', '.join(sorted(include))}[/yellow]"
            )
        df = df[included].reset_index(drop=True)

    if without_repo:
        exclude = _normalize_repos(without_repo)
        excluded = df["repo"].isin(exclude)
        if excluded.any():
            console.print(
                f"[yellow]Filtered out {excluded.sum()} PRs "
                f"from repos: {', '.join(sorted(exclude))}[/yellow]"
            )
            df = df[~excluded].reset_index(drop=True)

    reviewed_df = df[df["first_reviewed_at"].notna()].reset_index(drop=True)
    skipped = len(df) - len(reviewed_df)
    if skipped:
        console.print(
            f"[yellow]Excluded {skipped} PRs without a human review from stats.[/yellow]"
        )

    display_sample(reviewed_df, n=sample_n)
    display_stats(reviewed_df)
    in_scope = {url: files_map[url] for url in df["url"] if url in files_map}
    display_size_stats(df, ignore_globs, summarize_ignored(in_scope, ignore_globs))
    write_yaml(df, out)

    human_persons = df.loc[~df["author"].map(_is_bot), "person"]
    console.print(
        f"\n[bold]Summary:[/bold] {len(df)} merged PRs, "
        f"{len(reviewed_df)} with human reviews (used in stats), "
        f"{len(human_persons)} by {human_persons.nunique()} people"
    )


if __name__ == "__main__":
    app()
