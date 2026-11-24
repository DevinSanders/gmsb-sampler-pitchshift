#!/usr/bin/env python3
"""
Antigravity Pull Request Reviewer, Conflict Resolver & Auto-Merge Engine
Automated conflict resolution, code review, architectural guideline enforcement,
and serialized queue auto-merging.
"""

import os
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error
import re
from typing import Dict, Any, List, Optional, Tuple


def run_command(cmd: List[str], check: bool = True) -> Tuple[int, str, str]:
    """Execute a subprocess command safely and return (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8"
    )
    if check and proc.returncode != 0:
        print(f"[ERROR] Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def get_pr_details(pr_number: int) -> Optional[Dict[str, Any]]:
    """Retrieve PR metadata using GitHub CLI."""
    fields = "number,title,body,author,labels,isDraft,state,headRefName,baseRefName,mergeable,mergeStateStatus,headRefOid"
    for attempt in range(3):
        code, stdout, stderr = run_command(["gh", "pr", "view", str(pr_number), "--json", fields], check=False)
        if code != 0 or not stdout.strip():
            print(f"[WARN] Failed to fetch PR #{pr_number} metadata: {stderr.strip()}", file=sys.stderr)
            return None
        try:
            details = json.loads(stdout)
            if details.get("mergeable") == "UNKNOWN" and attempt < 2:
                time.sleep(3)
                continue
            return details
        except Exception as e:
            print(f"[ERROR] Error parsing PR metadata JSON: {e}", file=sys.stderr)
            return None
    return None


def get_pr_diff(pr_number: int, max_chars: int = 120_000) -> str:
    """Fetch PR diff via GitHub CLI, truncated cleanly if too large."""
    code, stdout, stderr = run_command(["gh", "pr", "diff", str(pr_number)], check=False)
    if code != 0:
        print(f"[WARN] Failed to get diff for PR #{pr_number}: {stderr.strip()}", file=sys.stderr)
        return ""
    diff = stdout.strip()
    if len(diff) > max_chars:
        diff = diff[:max_chars] + f"\n\n... [Diff truncated by Antigravity: {len(stdout) - max_chars} characters omitted]"
    return diff


SELF_CHECK_PREFIXES = ("Antigravity",)


def get_pr_checks(pr_number: int) -> Dict[str, Any]:
    """Retrieve external CI status check results for a PR (excluding Antigravity itself)."""
    code, stdout, stderr = run_command(["gh", "pr", "checks", str(pr_number), "--json", "name,state,bucket"], check=False)
    if code != 0 or not stdout.strip():
        return {"checks": [], "all_passed": False, "any_failed": False, "is_pending": True}
    try:
        raw_checks = json.loads(stdout)
        checks = [
            c for c in raw_checks
            if not any((c.get("name") or "").startswith(prefix) for prefix in SELF_CHECK_PREFIXES)
        ]
        if not checks:
            return {"checks": [], "all_passed": False, "any_failed": False, "is_pending": True}

        all_passed = all(
            c.get("bucket") in ("pass", "skipping") or c.get("state") in ("SUCCESS", "SKIPPED", "NEUTRAL")
            for c in checks
        )
        any_failed = any(
            c.get("bucket") in ("fail", "cancel") or c.get("state") in ("FAILURE", "CANCELLED", "TIMED_OUT", "ERROR")
            for c in checks
        )
        is_pending = any(
            c.get("bucket") == "pending" or c.get("state") in ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED")
            for c in checks
        )
        return {
            "checks": checks,
            "all_passed": all_passed,
            "any_failed": any_failed,
            "is_pending": is_pending,
        }
    except Exception as e:
        print(f"[ERROR] Error parsing checks JSON: {e}", file=sys.stderr)
        return {"checks": [], "all_passed": False, "any_failed": False, "is_pending": True}


def get_latest_build_conclusion(head_ref: str) -> Optional[str]:
    """Check if the latest Build workflow run on head_ref is blocked by action_required."""
    if not head_ref:
        return None
    code, stdout, _ = run_command(
        ["gh", "run", "list", "--branch", head_ref, "--workflow", "Build", "--limit", "1", "--json", "status,conclusion"],
        check=False
    )
    if code != 0 or not stdout.strip():
        return None
    try:
        runs = json.loads(stdout)
        if runs:
            return runs[0].get("conclusion")
    except Exception:
        pass
    return None


def wait_for_ci_checks(pr_number: int, head_ref: Optional[str] = None, timeout_seconds: int = 900, poll_interval: int = 15) -> Dict[str, Any]:
    """Wait for external CI checks to register and complete."""
    print(f"[INFO] Waiting up to {timeout_seconds // 60} minutes for CI checks on PR #{pr_number}...")
    start_time = time.time()
    last_print = 0

    while time.time() - start_time < timeout_seconds:
        checks_info = get_pr_checks(pr_number)
        checks = checks_info.get("checks", [])

        # If no checks are registered yet (right after push), check for action_required or wait briefly
        if not checks:
            elapsed = int(time.time() - start_time)
            if head_ref and elapsed >= 30:
                conclusion = get_latest_build_conclusion(head_ref)
                if conclusion == "action_required":
                    print(f"[BLOCKED] Build workflow for PR #{pr_number} requires manual maintainer approval ('action_required').")
                    return {"checks": [], "all_passed": False, "any_failed": True, "is_pending": False, "action_required": True}
                if elapsed >= 90:
                    print(f"[WARN] No external CI checks reported after {elapsed}s for PR #{pr_number}.")
                    return {"checks": [], "all_passed": False, "any_failed": False, "is_pending": False}
            time.sleep(poll_interval)
            continue

        if checks_info["any_failed"]:
            print(f"[WARN] CI checks failed for PR #{pr_number}.")
            return checks_info

        if checks_info["all_passed"] and not checks_info["is_pending"] and len(checks) > 0:
            print(f"[SUCCESS] All {len(checks)} external CI checks passed for PR #{pr_number}!")
            return checks_info

        # Still running/pending
        now = time.time()
        if now - last_print > 45:
            last_print = now
            elapsed = int(now - start_time)
            pending_names = [
                c.get("name", "unknown") for c in checks
                if c.get("bucket") == "pending" or c.get("state") in ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED")
            ]
            print(f"[INFO] CI checks in progress ({elapsed}s elapsed): waiting on {pending_names}...")

        time.sleep(poll_interval)

    print(f"[WARN] Timed out waiting for CI checks on PR #{pr_number}.")
    return get_pr_checks(pr_number)


def get_existing_bot_reviews(pr_number: int) -> List[Dict[str, Any]]:
    """Fetch list of reviews on the PR from actions / bots."""
    code, stdout, stderr = run_command(["gh", "pr", "view", str(pr_number), "--json", "reviews"], check=False)
    if code != 0 or not stdout.strip():
        return []
    try:
        data = json.loads(stdout)
        reviews = data.get("reviews", [])
        bot_reviews = []
        for r in reviews:
            author = r.get("author", {})
            login = author.get("login", "") if isinstance(author, dict) else str(author)
            if "github-actions" in login or "antigravity" in login.lower() or "bot" in login.lower():
                bot_reviews.append(r)
        return bot_reviews
    except Exception:
        return []


def read_repository_guidelines() -> str:
    """Read local codebase rules and architecture guidelines."""
    guideline_files = [
        ".github/copilot-instructions.md",
        "CONTRIBUTING.md",
        "CLAUDE.md",
        "README.md",
        ".github/dependabot.yml"
    ]
    sections = []
    for path in guideline_files:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    sections.append(f"### File: {path}\n{f.read()[:6000]}")
            except Exception as e:
                print(f"[INFO] Could not read {path}: {e}")
    return "\n\n".join(sections)


def is_dependency_pr(pr_details: Dict[str, Any]) -> bool:
    """Determine if PR is from Dependabot or is a dependency update."""
    author_info = pr_details.get("author", {})
    author_login = author_info.get("login", "") if isinstance(author_info, dict) else str(author_info)
    is_bot = author_info.get("is_bot", False) if isinstance(author_info, dict) else False

    if "dependabot" in author_login.lower() or (is_bot and "dependabot" in author_login.lower()):
        return True

    labels = [l.get("name", "").lower() for l in pr_details.get("labels", []) if isinstance(l, dict)]
    if any(l in ("dependencies", "dependabot", "deps", "nuget") for l in labels):
        return True

    head_ref = pr_details.get("headRefName", "").lower()
    if head_ref.startswith("dependabot/"):
        return True

    return False


def call_gemini_api(prompt: str, api_key: str, model: str = "gemini-2.5-flash") -> Optional[Dict[str, Any]]:
    """Call Google Gemini API expecting JSON output."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json"
        }
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return None
            text = parts[0].get("text", "")
            return json.loads(text)
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        print(f"[ERROR] Gemini API HTTP Error {e.code}: {err_msg}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ERROR] Gemini API invocation failed: {e}", file=sys.stderr)
        return None


def call_gemini_raw_text(prompt: str, api_key: str, model: str = "gemini-2.5-flash") -> Optional[str]:
    """Call Google Gemini API for free-form text/code output (e.g. conflict resolution)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1
        }
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return None
            return parts[0].get("text", "")
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        print(f"[ERROR] Gemini API HTTP Error {e.code}: {err_msg}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ERROR] Gemini API invocation failed: {e}", file=sys.stderr)
        return None


def resolve_file_conflict(
    file_path: str,
    conflicted_content: str,
    pr_details: Dict[str, Any],
    guidelines: str,
    api_key: str,
    model: str
) -> Optional[str]:
    """Ask Antigravity/Gemini to resolve conflict markers in a specific file."""
    prompt = f"""You are Antigravity, an expert software architect and automated code maintainer.
A Git merge conflict occurred while merging 'origin/main' into the pull request branch.

File to resolve: {file_path}

Repository Guidelines & Architectural Constraints:
{guidelines}

PR Information:
- PR #{pr_details.get('number')}: {pr_details.get('title')}
- Description: {pr_details.get('body', '')[:1000]}

Conflicted File Content:
The file below contains standard Git conflict markers (<<<<<<< HEAD, =======, >>>>>>> origin/main).
- HEAD contains the PR branch's proposed changes.
- origin/main contains the target branch changes from recently merged PRs.

```
{conflicted_content}
```

Instructions:
1. Reconcile both sides of the conflict:
   - For .csproj or dependency manifests: combine package updates harmoniously. If a package was updated on both sides, prefer the appropriate version that satisfies the PR without causing package downgrade (NU1605) warnings or breaking cross-project version parity.
   - For C# (.cs) code or markdown: merge both sets of changes preserving all features, bug fixes, and architectural boundaries.
2. Remove ALL conflict markers (<<<<<<< HEAD, =======, >>>>>>> origin/main).
3. Return ONLY the complete, resolved file content.
   DO NOT wrap the output in markdown code fences (no ```xml or ```csharp).
   DO NOT include any commentary, explanations, or notes.
"""
    raw_response = call_gemini_raw_text(prompt, api_key, model=model)
    if not raw_response:
        fallback_model = "gemini-2.5-flash" if model != "gemini-2.5-flash" else "gemini-1.5-flash"
        print(f"[WARN] Conflict resolution failed on {model}. Retrying with {fallback_model}...")
        raw_response = call_gemini_raw_text(prompt, api_key, model=fallback_model)

    if not raw_response:
        return None

    resolved = raw_response.strip()
    # Strip markdown block wrappers if present
    if resolved.startswith("```"):
        newline_idx = resolved.find("\n")
        if newline_idx != -1:
            resolved = resolved[newline_idx + 1:]
        if resolved.endswith("```"):
            resolved = resolved[:-3]
        resolved = resolved.strip()

    # Sanity check: verify no conflict markers remain
    if "<<<<<<<" in resolved or "=======" in resolved or ">>>>>>>" in resolved:
        print(f"[ERROR] Conflict markers remain in resolved output for {file_path}", file=sys.stderr)
        return None

    return resolved


def sync_and_resolve_conflicts(
    pr_number: int,
    pr_details: Dict[str, Any],
    guidelines: str,
    api_key: str,
    model: str
) -> Dict[str, Any]:
    """Check out PR branch, merge origin/main, and automatically resolve conflicts."""
    head_ref = pr_details.get("headRefName")
    if not head_ref:
        return {"success": False, "error": "No headRefName"}

    mergeable = pr_details.get("mergeable", "UNKNOWN")
    merge_state = pr_details.get("mergeStateStatus", "UNKNOWN")
    print(f"\n[QUEUE] Checking PR #{pr_number} branch '{head_ref}' (mergeable={mergeable}, mergeStateStatus={merge_state})...")

    if mergeable != "CONFLICTING" and merge_state not in ("DIRTY", "BEHIND"):
        print(f"[INFO] Branch '{head_ref}' has no conflicts and does not require syncing with main. Preserving existing CI checks.")
        return {"success": True, "conflicts_resolved": False, "pushed": False}

    print(f"[QUEUE] Synchronizing PR #{pr_number} branch '{head_ref}' with latest main...")
    run_command(["git", "config", "user.name", "github-actions[bot]"])
    run_command(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])

    # Ensure we have origin/main and the PR branch
    run_command(["git", "fetch", "origin", "main"])
    run_command(["git", "fetch", "origin", head_ref])

    # Check out PR branch locally
    code, _, err = run_command(["git", "checkout", "-B", head_ref, f"origin/{head_ref}"], check=False)
    if code != 0:
        print(f"[ERROR] Could not checkout PR branch {head_ref}: {err}", file=sys.stderr)
        return {"success": False, "error": f"Checkout failed: {err}"}

    # Check commit distance behind origin/main
    _, count_out, _ = run_command(["git", "rev-list", f"HEAD..origin/main", "--count"], check=False)
    commits_behind = int(count_out.strip() or "0")
    print(f"[INFO] Branch '{head_ref}' is {commits_behind} commit(s) behind origin/main.")

    if commits_behind == 0:
        print(f"[INFO] Branch '{head_ref}' is already fully up to date with origin/main.")
        return {"success": True, "conflicts_resolved": False, "pushed": False}

    # Attempt merge with origin/main
    print(f"[INFO] Merging origin/main into '{head_ref}'...")
    code, _, merge_err = run_command(["git", "merge", "origin/main", "--no-edit"], check=False)

    if code == 0:
        print(f"[SUCCESS] Clean merge from origin/main. Pushing updated branch...")
        p_code, _, p_err = run_command(["git", "push", "origin", head_ref], check=False)
        if p_code == 0:
            return {"success": True, "conflicts_resolved": False, "pushed": True}
        else:
            print(f"[WARN] Failed to push merged branch: {p_err}", file=sys.stderr)
            return {"success": False, "error": p_err}

    # Merge conflict encountered!
    print(f"[WARN] Merge conflict detected when merging origin/main into '{head_ref}'!")
    _, diff_out, _ = run_command(["git", "diff", "--name-only", "--diff-filter=U"], check=False)
    conflicted_files = [f.strip() for f in diff_out.strip().split("\n") if f.strip()]

    print(f"[INFO] Conflicted files ({len(conflicted_files)}): {conflicted_files}")
    resolved_files = []

    for file_path in conflicted_files:
        if not os.path.exists(file_path):
            continue

        print(f"[INFO] Antigravity resolving conflict in {file_path}...")
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        resolved = resolve_file_conflict(file_path, content, pr_details, guidelines, api_key, model)
        if not resolved:
            print(f"[ERROR] Could not resolve conflict in {file_path}. Aborting merge.", file=sys.stderr)
            run_command(["git", "merge", "--abort"], check=False)
            comment_body = f"⚠️ **Antigravity Merge Conflict Notice**\n\nA merge conflict in `{file_path}` could not be automatically resolved with `main`. Manual conflict resolution is required."
            run_command(["gh", "pr", "comment", str(pr_number), "--body", comment_body], check=False)
            return {"success": False, "error": f"Failed conflict resolution in {file_path}"}

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(resolved)

        run_command(["git", "add", file_path])
        resolved_files.append(file_path)

    # Commit resolved merge
    commit_msg = f"chore: Antigravity automated conflict resolution with main\n\nResolved files:\n" + "\n".join(f"- {f}" for f in resolved_files)
    c_code, _, c_err = run_command(["git", "commit", "-m", commit_msg], check=False)
    if c_code != 0:
        print(f"[ERROR] Commit after conflict resolution failed: {c_err}", file=sys.stderr)
        run_command(["git", "merge", "--abort"], check=False)
        return {"success": False, "error": c_err}

    print(f"[SUCCESS] Pushing conflict resolution commit to origin/{head_ref}...")
    p_code, _, p_err = run_command(["git", "push", "origin", head_ref], check=False)
    if p_code != 0:
        print(f"[ERROR] Failed to push conflict resolution: {p_err}", file=sys.stderr)
        return {"success": False, "error": p_err}

    # Post comment on PR
    file_list_md = "\n".join(f"- `{f}`" for f in resolved_files)
    comment_body = (
        f"### 🤖 Antigravity Automated Conflict Resolution\n\n"
        f"Merge conflicts with `main` were detected and successfully resolved by Antigravity in the following files:\n"
        f"{file_list_md}\n\n"
        f"The resolution commit has been pushed to `{head_ref}`. CI checks are now running."
    )
    run_command(["gh", "pr", "comment", str(pr_number), "--body", comment_body], check=False)

    return {"success": True, "conflicts_resolved": True, "pushed": True, "resolved_files": resolved_files}


def trigger_next_open_prs(current_pr_number: int, merged: bool = True) -> None:
    """Dispatch the Antigravity queue for the next eligible open PR (one at a time)."""
    print(f"\n[QUEUE] Checking for the next open pull request to process...")
    code, stdout, stderr = run_command(
        ["gh", "pr", "list", "--json", "number,title,isDraft,headRefOid,reviews"],
        check=False
    )
    if code != 0 or not stdout.strip():
        return
    try:
        prs = sorted(json.loads(stdout), key=lambda p: p.get("number", 0))
        for p in prs:
            num = p.get("number")
            if not num or num == current_pr_number or p.get("isDraft"):
                continue

            head_oid = p.get("headRefOid")
            bot_reviews = [
                r for r in (p.get("reviews") or [])
                if "github-actions" in ((r.get("author") or {}).get("login", ""))
                and (r.get("commit") or {}).get("oid") == head_oid
            ]

            if merged:
                # Main just advanced: re-evaluate unreviewed or previously approved PRs, skip CHANGES_REQUESTED on current SHA
                if any(r.get("state") == "CHANGES_REQUESTED" for r in bot_reviews):
                    continue
            else:
                # Current PR wasn't merged: only advance to PRs that have not been reviewed on their current SHA yet
                if bot_reviews:
                    continue

            print(f"[QUEUE] Dispatching Antigravity queue for next PR #{num} ('{p.get('title')}')...")
            run_command(["gh", "workflow", "run", "Antigravity PR Review & Auto-Merge", "-f", f"pr_number={num}"], check=False)
            break
    except Exception as e:
        print(f"[INFO] Note on triggering next open PRs: {e}")


def build_review_prompt(pr_details: Dict[str, Any], diff: str, guidelines: str) -> str:
    repo_name = os.environ.get("GITHUB_REPOSITORY", "unknown")
    return f"""You are Antigravity, an expert software architect and automated code reviewer reviewing a pull request for repository '{repo_name}' (.NET 10 / Game Master Sound Board ecosystem).

Repository guidelines and architectural constraints:
{guidelines}

Pull Request Details:
- Repository: {repo_name}
- Number: #{pr_details.get('number')}
- Title: {pr_details.get('title')}
- Author: {pr_details.get('author', {}).get('login')}
- Target Branch: {pr_details.get('baseRefName')}
- Source Branch: {pr_details.get('headRefName')}
- Description:
{pr_details.get('body', '')[:2000]}

Git Diff:
```diff
{diff}
```

Instructions:
1. Thoroughly inspect the diff against the repository's conventions and .NET 10 best practices:
   - Host app (if applicable): Desktop -> UI -> Core -> PluginApi layering, WaveBuffer aliasing rules, SQLite SchemaMigrations.cs, CommunityToolkit.Mvvm.
   - Plugin / theme repos (if applicable): SoundBoard.PluginApi compatibility, Avalonia theme resource completeness, clean net10.0 packaging without unexpected dependencies.
   - For dependency/NuGet/GitHub Actions upgrades: check for breaking changes, major-version skew against .NET 10 / EF Core 10 / SoundBoard.PluginApi, transitive package downgrades (NU1605), or licensing changes (e.g. FluentAssertions 8+ commercial license, NAudio 3.x breaking changes, xUnit v4 compatibility with Avalonia.Headless.XUnit).
2. Choose one Verdict:
   - "APPROVE": Changes are safe, adhere to conventions, and no breaking bugs/violations were found.
   - "COMMENT": Minor suggestions, advisory notes, or informative feedback; non-blocking.
   - "REQUEST_CHANGES": Identified breaking changes, architectural boundary violations, licensing pitfalls, or bugs.
3. If this is a Dependabot/dependency PR, evaluate whether the bump is safe to auto-merge. If it introduces breaking changes or requires manual code adaptation, explain why.

Return ONLY a valid JSON object matching this schema:
{{
  "verdict": "APPROVE" | "COMMENT" | "REQUEST_CHANGES",
  "summary": "Concise 1-3 sentence summary of the PR and impact.",
  "confidence_score": 95,
  "assessment": {{
    "architecture": "Assessment of architectural layer compliance",
    "correctness": "Assessment of runtime correctness, null safety, bugs",
    "dependencies": "Assessment of package compatibility, breaking changes, licenses"
  }},
  "findings": [
    {{
      "severity": "info" | "warning" | "error",
      "message": "Specific finding explanation",
      "suggestion": "Actionable recommendation or fix"
    }}
  ],
  "auto_merge_recommendation": {{
    "eligible": true,
    "reason": "Clear explanation of why it is or is not safe for auto-merge"
  }}
}}
"""


def format_review_markdown(review: Dict[str, Any], pr_details: Dict[str, Any]) -> str:
    verdict = review.get("verdict", "COMMENT")
    verdict_badge = {
        "APPROVE": "✅ **APPROVE**",
        "COMMENT": "💬 **COMMENT**",
        "REQUEST_CHANGES": "❌ **REQUEST CHANGES**"
    }.get(verdict, verdict)

    lines = [
        "## 🤖 Antigravity Automated PR Review",
        "",
        f"**Verdict**: {verdict_badge}  ",
        f"**Confidence**: {review.get('confidence_score', 90)}%",
        "",
        f"### 📋 Summary\n{review.get('summary', 'No summary provided.')}",
        "",
        "### 🏗️ Architectural & Quality Assessment",
    ]

    assess = review.get("assessment", {})
    if assess.get("architecture"):
        lines.append(f"- **Architecture**: {assess['architecture']}")
    if assess.get("correctness"):
        lines.append(f"- **Correctness**: {assess['correctness']}")
    if assess.get("dependencies"):
        lines.append(f"- **Dependencies & Licensing**: {assess['dependencies']}")

    findings = review.get("findings", [])
    if findings:
        lines.extend(["", "### 🔍 Key Findings & Suggestions"])
        for f in findings:
            sev_icon = {"error": "🚫", "warning": "⚠️", "info": "ℹ️"}.get(f.get("severity"), "•")
            lines.append(f"- {sev_icon} **{f.get('severity', 'Note').title()}**: {f.get('message')}")
            if f.get("suggestion"):
                lines.append(f"  *Suggestion*: {f.get('suggestion')}")

    auto_merge = review.get("auto_merge_recommendation", {})
    lines.extend([
        "",
        "### ⚡ Auto-Merge Status",
        f"- **Eligible for Auto-Merge**: {'Yes' if auto_merge.get('eligible') else 'No'}",
        f"- **Reason**: {auto_merge.get('reason', 'N/A')}",
        "",
        "---",
        "*Automated by [Google Antigravity](https://antigravity.google) PR Review & Merge Gate*"
    ])

    return "\n".join(lines)


def submit_review_to_github(pr_number: int, review: Dict[str, Any], pr_details: Dict[str, Any]) -> bool:
    """Submit the review to GitHub using gh CLI."""
    body = format_review_markdown(review, pr_details)
    verdict = review.get("verdict", "COMMENT")

    review_flag = "--comment"
    if verdict == "APPROVE":
        review_flag = "--approve"
    elif verdict == "REQUEST_CHANGES":
        review_flag = "--request-changes"

    cmd = ["gh", "pr", "review", str(pr_number), review_flag, "--body", body]
    code, stdout, stderr = run_command(cmd, check=False)
    if code == 0:
        print(f"[SUCCESS] Submitted review ({verdict}) for PR #{pr_number}")
        return True
    else:
        print(f"[WARN] Failed to submit review as {review_flag} ({stderr.strip()}). Falling back to comment...", file=sys.stderr)
        fallback_cmd = ["gh", "pr", "review", str(pr_number), "--comment", "--body", body]
        fb_code, _, fb_err = run_command(fallback_cmd, check=False)
        return fb_code == 0


def execute_automerge_if_eligible(
    pr_number: int,
    pr_details: Dict[str, Any],
    review: Dict[str, Any]
) -> bool:
    """Evaluate auto-merge policy, wait for CI checks, and merge if qualifying."""
    is_dep = is_dependency_pr(pr_details)
    verdict = review.get("verdict")
    auto_eligible = review.get("auto_merge_recommendation", {}).get("eligible", False)

    print(f"\n[POLICY] Evaluating Auto-Merge Policy for PR #{pr_number}:")
    print(f"         - Is Dependency Update: {is_dep}")
    print(f"         - Review Verdict: {verdict}")
    print(f"         - Review Auto-Merge Eligible: {auto_eligible}")

    if not is_dep:
        print(f"[POLICY] PR #{pr_number} is not a dependency update. Non-dependency PRs require manual maintainer approval.")
        return False

    if verdict != "APPROVE" or not auto_eligible:
        print(f"[POLICY] PR #{pr_number} did not receive approval for auto-merge. Manual intervention required.")
        return False

    # Wait for CI checks to complete
    checks_info = wait_for_ci_checks(pr_number, head_ref=pr_details.get("headRefName"), timeout_seconds=900, poll_interval=15)
    print(f"[INFO] Final CI Checks Status: all_passed={checks_info['all_passed']}, any_failed={checks_info['any_failed']}")

    if checks_info.get("action_required"):
        comment_body = (
            "ℹ️ **Antigravity Auto-Merge Waiting on Workflow Approval**\n\n"
            "The CI `Build` workflow on the latest commit is currently in `action_required` state and needs manual approval in the Actions tab before merging."
        )
        run_command(["gh", "pr", "comment", str(pr_number), "--body", comment_body], check=False)
        return False

    if checks_info["any_failed"]:
        print(f"[BLOCKED] PR #{pr_number} has failing CI checks. Auto-merge aborted.")
        return False

    if checks_info["all_passed"] and len(checks_info["checks"]) > 0:
        print(f"[INFO] All CI checks passed! Merging PR #{pr_number} via squash merge...")
        merge_cmd = [
            "gh", "pr", "merge", str(pr_number),
            "--squash",
            "--delete-branch",
            "--subject", f"Merge PR #{pr_number}: {pr_details.get('title', '')}"
        ]
        code, out, err = run_command(merge_cmd, check=False)
        if code == 0:
            print(f"[SUCCESS] 🚀 PR #{pr_number} successfully auto-merged!")
            return True
        else:
            err_detail = err.strip() or out.strip()
            print(f"[ERROR] Could not complete merge: {err_detail}", file=sys.stderr)
            if "workflow" in err_detail.lower():
                comment_body = (
                    "ℹ️ **Antigravity Auto-Merge Blocked by GitHub Token Permissions**\n\n"
                    "This PR was approved and all CI checks passed, but GitHub's default `GITHUB_TOKEN` "
                    "does not have `workflow` permission to merge PRs that modify `.github/workflows/*` files.\n\n"
                    "Please merge this PR manually."
                )
                run_command(["gh", "pr", "comment", str(pr_number), "--body", comment_body], check=False)
            return False
    else:
        print(f"[INFO] CI checks did not pass or timed out. Auto-merge skipped.")
        return False


def main():
    print("=================================================================")
    print("  Antigravity PR Reviewer, Conflict Resolver & Auto-Merge Queue  ")
    print("=================================================================")

    pr_number_str = os.environ.get("PR_NUMBER")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")

    if not pr_number_str and event_path and os.path.exists(event_path):
        with open(event_path, "r", encoding="utf-8") as f:
            event_data = json.load(f)
            pr_data = event_data.get("pull_request")
            if pr_data:
                pr_number_str = str(pr_data.get("number"))

    if not pr_number_str:
        print("[ERROR] PR_NUMBER environment variable or event data not found.", file=sys.stderr)
        sys.exit(1)

    pr_number = int(pr_number_str)
    print(f"[INFO] Starting serialized processing for PR #{pr_number}...")

    pr_details = get_pr_details(pr_number)
    if not pr_details:
        print(f"[ERROR] Could not fetch PR details for #{pr_number}.", file=sys.stderr)
        sys.exit(1)

    if pr_details.get("state") != "OPEN":
        print(f"[INFO] PR #{pr_number} is already {pr_details.get('state')}. Exiting queue.")
        return

    if pr_details.get("isDraft"):
        print(f"[INFO] PR #{pr_number} is currently a draft. Skipping until marked ready for review.")
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("\n[NOTICE] GEMINI_API_KEY is not configured in repository secrets.")
        print("To enable Antigravity AI reviews and auto-merge:")
        print("  1. Obtain a Gemini API key at https://aistudio.google.com/app/api-keys")
        print("  2. Add secret GEMINI_API_KEY under Actions Secrets: gh secret set GEMINI_API_KEY")
        return

    guidelines = read_repository_guidelines()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # Step 1: Synchronize branch with origin/main & resolve conflicts automatically
    sync_result = sync_and_resolve_conflicts(pr_number, pr_details, guidelines, api_key, model)
    if not sync_result.get("success"):
        print(f"[WARN] Synchronization/conflict resolution was unsuccessful. Halting auto-merge.")
        return

    # If new commits were pushed (e.g. conflict resolution or sync commit), wait for initial CI registration
    if sync_result.get("pushed"):
        print(f"[INFO] Branch was updated with main/conflict fixes. Pausing 15s for CI trigger...")
        time.sleep(15)

    # Refresh PR details and diff after sync
    pr_details = get_pr_details(pr_number)
    head_oid = pr_details.get("headRefOid")

    # Step 2: Check if current head commit already has an APPROVED bot review
    bot_reviews = get_existing_bot_reviews(pr_number)
    already_approved = any(
        r.get("state") == "APPROVED" and (r.get("commit") or {}).get("oid") == head_oid
        for r in bot_reviews
    )

    if already_approved:
        print(f"[INFO] Commit {head_oid[:8]} already has an APPROVED Antigravity review. Skipping duplicate review.")
        review = {
            "verdict": "APPROVE",
            "auto_merge_recommendation": {"eligible": True, "reason": "Previously approved for current commit."}
        }
    else:
        diff = get_pr_diff(pr_number)
        print(f"[INFO] Querying Antigravity ({model}) for PR review...")
        prompt = build_review_prompt(pr_details, diff, guidelines)
        review = call_gemini_api(prompt, api_key, model=model)

        if not review:
            fallback_model = "gemini-2.5-flash" if model != "gemini-2.5-flash" else "gemini-1.5-flash"
            print(f"[WARN] {model} query failed. Attempting fallback to {fallback_model}...")
            review = call_gemini_api(prompt, api_key, model=fallback_model)

        if not review:
            print("[ERROR] Failed to obtain AI review response from Gemini API.", file=sys.stderr)
            sys.exit(1)

        print(f"[INFO] Review generated successfully. Verdict: {review.get('verdict')}")
        submit_review_to_github(pr_number, review, pr_details)

    # Step 3: Evaluate Auto-Merge & Wait on CI checks
    merged = execute_automerge_if_eligible(pr_number, pr_details, review)

    # Step 4: Advance queue to the next eligible open PR (re-evaluating on merge or advancing to unreviewed PRs)
    trigger_next_open_prs(pr_number, merged=merged)


if __name__ == "__main__":
    main()
