#!/usr/bin/env python3
"""Create and publish a Hugo diary post."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_TAGS = ["diary"]
DEFAULT_CATEGORIES = ["journal"]


class PublishError(Exception):
    """Base exception type for predictable publish failures."""


class ValidationError(PublishError):
    """Input validation error."""


class AuthError(PublishError):
    """Git authentication/authorization error."""


class GitPushError(PublishError):
    """General git push/commit error."""


class WorkflowError(PublishError):
    """Reserved for CI/workflow status handling."""


@dataclass
class PublishDiaryResult:
    postPath: str
    commitSha: str
    pageUrl: str
    publishedAt: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish diary entry to Hugo blog.")
    parser.add_argument("--title", required=True, help="Post title.")

    body_group = parser.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", help="Post body text.")
    body_group.add_argument("--body-file", help="Path to text/markdown file for body.")

    parser.add_argument("--tags", default="diary", help="Comma-separated tags. Default: diary")
    parser.add_argument("--mood", default="", help="Optional mood text.")
    parser.add_argument("--repo-root", default="", help="Repo root. Default: auto-detect from script path.")
    parser.add_argument("--base-url", default="", help="Override site base URL.")
    parser.add_argument("--skip-push", action="store_true", help="Commit locally but skip git push.")
    parser.add_argument("--no-git", action="store_true", help="Only write markdown file, no git actions.")
    return parser.parse_args()


def detect_repo_root(user_value: str) -> Path:
    if user_value:
        return Path(user_value).resolve()
    return Path(__file__).resolve().parent.parent


def normalize_tags(raw: str) -> list[str]:
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    return tags or DEFAULT_TAGS.copy()


def read_body(body: str | None, body_file: str | None) -> str:
    if body_file:
        return Path(body_file).read_text(encoding="utf-8").strip()
    assert body is not None
    return body.strip()


def validate_input(title: str, body: str) -> None:
    if not title.strip():
        raise ValidationError("title must not be empty")
    if not body.strip():
        raise ValidationError("body must not be empty")


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.lower()).strip("-")
    return cleaned or "post"


def pick_post_path(posts_dir: Path, title: str, now: datetime) -> Path:
    date_prefix = now.strftime("%Y-%m-%d")
    base_slug = slugify(title)
    candidate = posts_dir / f"{date_prefix}-{base_slug}.md"
    if not candidate.exists():
        return candidate

    suffix = 2
    while True:
        candidate = posts_dir / f"{date_prefix}-{base_slug}-{suffix}.md"
        if not candidate.exists():
            return candidate
        suffix += 1


def yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def yaml_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(yaml_quote(v) for v in values) + "]"


def render_post(title: str, body: str, tags: list[str], mood: str, now: datetime) -> str:
    lines = [
        "---",
        f"title: {yaml_quote(title)}",
        f"date: {now.isoformat(timespec='seconds')}",
        "draft: false",
        f"tags: {yaml_list(tags)}",
        f"categories: {yaml_list(DEFAULT_CATEGORIES)}",
        f"mood: {yaml_quote(mood)}",
        "---",
        "",
        body.rstrip(),
        "",
    ]
    return "\n".join(lines)


def git(args: list[str], cwd: Path) -> str:
    env = os.environ.copy()
    env.setdefault("HOME", str(cwd))
    env.setdefault("GIT_CONFIG_GLOBAL", str(cwd / ".gitconfig"))

    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if proc.returncode == 0:
        return (proc.stdout or "").strip()

    msg = (proc.stderr or proc.stdout or "git command failed").strip()
    msg_lower = msg.lower()
    if "auth" in msg_lower or "403" in msg_lower or "permission denied" in msg_lower:
        raise AuthError(msg)
    raise GitPushError(msg)


def ensure_git_repo(repo_root: Path) -> None:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        raise GitPushError(f"not a git repository: {repo_root}")


def read_base_url(repo_root: Path, override: str) -> str:
    if override:
        return override.rstrip("/") + "/"

    config_file = repo_root / "hugo.toml"
    if not config_file.exists():
        return ""
    pattern = re.compile(r"""^\s*baseURL\s*=\s*["']([^"']+)["']\s*$""")
    for line in config_file.read_text(encoding="utf-8-sig").splitlines():
        match = pattern.match(line)
        if match:
            value = match.group(1).strip()
            if value:
                return value.rstrip("/") + "/"
    return ""


def current_branch(repo_root: Path) -> str:
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    return "main" if branch == "HEAD" else branch


def push_with_retry(repo_root: Path, branch: str, retries: int = 2) -> None:
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            git(["push", "origin", branch], repo_root)
            return
        except AuthError:
            raise
        except GitPushError:
            if attempt >= attempts:
                raise
            time.sleep(2 ** (attempt - 1))


def publish() -> PublishDiaryResult:
    args = parse_args()
    repo_root = detect_repo_root(args.repo_root)
    posts_dir = repo_root / "content" / "posts"
    posts_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(TZ)
    body = read_body(args.body, args.body_file)
    validate_input(args.title, body)
    tags = normalize_tags(args.tags)

    post_path = pick_post_path(posts_dir, args.title, now)
    rel_post_path = post_path.relative_to(repo_root).as_posix()

    content = render_post(args.title.strip(), body, tags, args.mood.strip(), now)
    post_path.write_text(content, encoding="utf-8")

    base_url = read_base_url(repo_root, args.base_url)
    page_url = ""
    if base_url:
        slug = post_path.stem
        page_url = f"{base_url}{slug}/"

    commit_sha = ""
    if not args.no_git:
        ensure_git_repo(repo_root)
        git(["add", rel_post_path], repo_root)
        commit_msg = f"feat(diary): publish {now.strftime('%Y-%m-%d')} {args.title.strip()}"
        git(["commit", "-m", commit_msg], repo_root)
        commit_sha = git(["rev-parse", "HEAD"], repo_root)

        if not args.skip_push:
            branch = current_branch(repo_root)
            push_with_retry(repo_root, branch, retries=2)

    return PublishDiaryResult(
        postPath=rel_post_path,
        commitSha=commit_sha,
        pageUrl=page_url,
        publishedAt=now.isoformat(timespec="seconds"),
    )


def main() -> int:
    try:
        result = publish()
        print(json.dumps({"ok": True, **result.__dict__}, ensure_ascii=False))
        return 0
    except PublishError as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
            )
        )
        return 1
    except Exception as exc:  # pragma: no cover - fallback safeguard
        print(
            json.dumps(
                {"ok": False, "error": {"type": "UnexpectedError", "message": str(exc)}},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
