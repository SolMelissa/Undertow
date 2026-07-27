"""
On-demand credential configuration: Reddit OAuth and the Hydrus Client API key. Equivalent
of Configure-ApiKeys.ps1. Shows what's already configured (masked) and only prompts for what
you choose to (re)set.

Note: Python's `open(path, "w", encoding="utf-8")` never writes a BOM, unlike Windows
PowerShell 5.1's `Set-Content -Encoding UTF8` (which silently prepends one and breaks
anything strict about encoding, like Python's own json module reading it back) - so unlike
the PS1 version, this doesn't need a Set-Utf8NoBom workaround at all.
"""

from __future__ import annotations

import json
import re
import subprocess
import webbrowser

from . import config


def mask_secret(value: str | None) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def get_reddit_status() -> tuple[bool, str | None]:
    try:
        # utf-8-sig: this file can get written by PowerShell's Set-Content -Encoding UTF8,
        # which prepends a BOM that plain "utf-8" decoding fails on - that failure was getting
        # silently swallowed by the except below and misreported as "not configured" even when
        # it actually was. Same class of bug as the one in api_client.get_daemon_api_info().
        cfg = json.loads(config.GALLERY_DL_CONFIG_FILE.read_text(encoding="utf-8-sig"))
        cid = cfg.get("extractor", {}).get("reddit", {}).get("client-id")
        configured = bool(cid and cid != "PLACEHOLDER_SET_BELOW")
        return configured, cid
    except (OSError, json.JSONDecodeError, AttributeError):
        return False, None


# Every gallery-dl extractor that reads an api-key/client-id/client-secret config value (found
# by grepping site-packages/gallery_dl/extractor/*.py for `.config("api-key"/"client-id"/
# "client-secret")` - see the extractor source for the authoritative list if gallery-dl adds or
# removes one). Reddit and Hydrus keep their own dedicated tabs (Reddit's flow is a multi-step
# OAuth dance, not a flat credential; Hydrus isn't a gallery-dl extractor at all) - everything
# else lands here. `fields` are written to GALLERY_DL_USER_CONFIG_FILE under
# extractor.<id>.<field key>; the first field is what get_service_key_status checks for
# "configured" status.
SERVICE_KEY_REGISTRY: list[dict] = [
    {
        "id": "tumblr",
        "label": "Tumblr",
        "url": "https://www.tumblr.com/oauth/apps",
        "note": "Register an application (any placeholder name/URL is fine), then copy its "
                "OAuth Consumer Key below. Fixes \"Daily API rate limit exceeded\" errors, "
                "which happen because gallery-dl's default key is shared across every user.",
        "fields": [{"key": "api-key", "label": "OAuth Consumer Key"}],
    },
    {
        "id": "imgur",
        "label": "Imgur",
        "url": "https://imgur.com/account/settings/apps",
        "note": "Register an application (OAuth 2 authorization without a callback URL works "
                "for anonymous/client-ID-only usage), then copy its Client ID below.",
        "fields": [{"key": "client-id", "label": "Client ID"}],
    },
    {
        "id": "deviantart",
        "label": "DeviantArt",
        "url": "https://www.deviantart.com/developers/apps",
        "note": "Register an application, then copy its Client ID and Client Secret below.",
        "fields": [
            {"key": "client-id", "label": "Client ID"},
            {"key": "client-secret", "label": "Client Secret"},
        ],
    },
    {
        "id": "civitai",
        "label": "Civitai",
        "url": "https://civitai.com/user/account",
        "note": "Under \"API Keys\", add a new key, then paste it below.",
        "fields": [{"key": "api-key", "label": "API Key"}],
    },
    {
        "id": "mangadex",
        "label": "MangaDex",
        "url": "https://mangadex.org/settings",
        "note": "Under the \"API Clients\" tab, create a personal client, then paste its ID and "
                "Secret below. Only needed for logged-in features (e.g. your follows list) - "
                "public manga/chapter downloads work without it.",
        "fields": [
            {"key": "client-id", "label": "Client ID"},
            {"key": "client-secret", "label": "Client Secret"},
        ],
    },
    {
        "id": "pixeldrain",
        "label": "Pixeldrain",
        "url": "https://pixeldrain.com/user/api_keys",
        "note": "Copy your account's API key below.",
        "fields": [{"key": "api-key", "label": "API Key"}],
    },
    {
        "id": "wallhaven",
        "label": "Wallhaven",
        "url": "https://wallhaven.cc/settings/account",
        "note": "Your API key is shown under \"API Key\" on the account settings page.",
        "fields": [{"key": "api-key", "label": "API Key"}],
    },
    {
        "id": "weasyl",
        "label": "Weasyl",
        "url": "https://www.weasyl.com/control/apikeys",
        "note": "Generate an API key, then paste it below.",
        "fields": [{"key": "api-key", "label": "API Key"}],
    },
    {
        "id": "derpibooru",
        "label": "Derpibooru",
        "url": "https://derpibooru.org/registrations/edit",
        "note": "Your API key is shown under account settings. Only needed for private/"
                "restricted content - public search works without it.",
        "fields": [{"key": "api-key", "label": "API Key"}],
    },
    {
        "id": "ponybooru",
        "label": "Ponybooru",
        "url": "https://ponybooru.org/registrations/edit",
        "note": "Your API key is shown under account settings. Only needed for private/"
                "restricted content - public search works without it.",
        "fields": [{"key": "api-key", "label": "API Key"}],
    },
    {
        "id": "twibooru",
        "label": "Twibooru",
        "url": "https://twibooru.org/registrations/edit",
        "note": "Your API key is shown under account settings. Only needed for private/"
                "restricted content - public search works without it.",
        "fields": [{"key": "api-key", "label": "API Key"}],
    },
    {
        "id": "rule34",
        "label": "Rule34",
        "url": "https://rule34.xxx/index.php?page=account&s=options",
        "note": "Under \"API Access Credentials\", copy your API key and user ID below.",
        "fields": [
            {"key": "api-key", "label": "API Key"},
            {"key": "user-id", "label": "User ID"},
        ],
    },
    {
        "id": "blogger",
        "label": "Blogger",
        "url": "https://console.cloud.google.com/apis/library/blogger.googleapis.com",
        "note": "Enable the Blogger API v3 on a Google Cloud project, create an API key "
                "credential, then paste it below.",
        "fields": [{"key": "api-key", "label": "API Key"}],
    },
]


def _read_user_config() -> dict:
    try:
        return json.loads(config.GALLERY_DL_USER_CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _looks_like_placeholder(value: str | None) -> bool:
    """hydownloader's shipped gallery-dl-user-config.json ships some fields pre-filled with
    prompts like "your rule34 api key here" instead of leaving them null - without this, those
    would read as "configured" just because the field is a non-empty string."""
    if not value:
        return False
    low = value.lower()
    return "your " in low and " here" in low


def get_service_key_status(service_id: str) -> tuple[bool, dict[str, str | None]]:
    """Returns (configured, {field_key: current value or None}) for one SERVICE_KEY_REGISTRY
    entry, read from the overlay config file. "configured" means the first (primary) field
    has a real value - secondary fields (e.g. deviantart's client-secret) are reported but
    don't gate the status on their own since some extractors work with just the primary
    field. Placeholder text left over from hydownloader's shipped config counts as unset."""
    entry = next((e for e in SERVICE_KEY_REGISTRY if e["id"] == service_id), None)
    if entry is None:
        return False, {}
    section = _read_user_config().get("extractor", {}).get(service_id, {})
    values = {}
    for f in entry["fields"]:
        v = section.get(f["key"])
        values[f["key"]] = None if _looks_like_placeholder(v) else v
    primary_key = entry["fields"][0]["key"]
    return bool(values.get(primary_key)), values


def list_service_key_statuses() -> list[dict]:
    """SERVICE_KEY_REGISTRY entries annotated with live configured/masked-value status, sorted
    with the still-missing ones first - what the web UI's "Other Services" list and the console
    flow both render from."""
    out = []
    for entry in SERVICE_KEY_REGISTRY:
        configured, values = get_service_key_status(entry["id"])
        out.append({
            **entry,
            "configured": configured,
            "masked_values": {k: mask_secret(v) for k, v in values.items()},
        })
    out.sort(key=lambda s: (s["configured"], s["label"]))
    return out


def apply_service_key(service_id: str, values: dict[str, str]) -> tuple[bool, str]:
    """Writes the given field values for one SERVICE_KEY_REGISTRY entry into
    GALLERY_DL_USER_CONFIG_FILE (creating it if missing), merging into any existing content
    rather than overwriting the whole file - other extractors' settings may already live
    there. Blank values are skipped rather than clearing an existing key by accident."""
    entry = next((e for e in SERVICE_KEY_REGISTRY if e["id"] == service_id), None)
    if entry is None:
        return False, f"unknown service: {service_id}"
    valid_keys = {f["key"] for f in entry["fields"]}
    given = {k: v.strip() for k, v in values.items() if k in valid_keys and (v or "").strip()}
    if not given:
        return False, "no value given"

    cfg = _read_user_config()
    cfg.setdefault("extractor", {}).setdefault(service_id, {}).update(given)
    config.GALLERY_DL_USER_CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return True, f"Saved {entry['label']} key(s). Takes effect on that service's next subscription check - no daemon restart needed."


def get_hydrus_key_status() -> tuple[bool, str | None]:
    try:
        content = config.IMPORT_JOBS_FILE.read_text(encoding="utf-8-sig")
        m = re.search(r"defAPIKey\s*=\s*[\"']([0-9a-fA-F]{64})[\"']", content)
        if m:
            return True, m.group(1)
    except OSError:
        pass
    return False, None


def apply_reddit_app_config(client_id: str, reddit_username: str) -> tuple[bool, str]:
    """Writes the custom Reddit app's client-id + a matching User-Agent into gallery-dl's
    config, without running the OAuth flow itself (see run_reddit_oauth_capture) - split out
    so the web UI can drive each step as its own request/response instead of one long blocking
    call. Shared by the console flow (_configure_reddit_oauth) and the web UI's equivalent
    route, so both write the exact same fields the exact same way."""
    if not client_id:
        return False, "no client ID given"
    try:
        cfg = json.loads(config.GALLERY_DL_CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        return False, f"couldn't read {config.GALLERY_DL_CONFIG_FILE}: {e}"
    user_agent = f"gallery-dl:hydownloader-pipeline:v1.0 (by /u/{reddit_username})" if reddit_username else "gallery-dl:hydownloader-pipeline:v1.0"
    cfg.setdefault("extractor", {}).setdefault("reddit", {})["client-id"] = client_id
    cfg["extractor"]["reddit"]["user-agent"] = user_agent
    config.GALLERY_DL_CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return True, "Saved client ID and User-Agent. Next, run the OAuth step below."


def run_reddit_oauth_capture(timeout: float = 120.0) -> tuple[bool, str]:
    """Runs gallery-dl's own `oauth:reddit` flow (opens a browser tab, waits for the redirect
    to its local listener on :6414) and captures its stdout/stderr instead of letting it write
    straight to this process's console - the web UI has no console to show that to. The
    refresh-token gallery-dl prints still has to be copied by hand into
    apply_reddit_refresh_token afterward: gallery-dl's exact print format isn't something this
    package controls or wants to depend on parsing, so showing the raw output and asking the
    user to paste the token (same as the original console flow) is safer than guessing a regex
    that might silently break on a gallery-dl update."""
    try:
        result = subprocess.run(
            ["gallery-dl", "oauth:reddit"], capture_output=True, text=True, timeout=timeout
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip() or "(no output)"
    except FileNotFoundError:
        return False, "gallery-dl isn't on PATH - check diagnostics."
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s waiting for the OAuth redirect."
    except OSError as e:
        return False, str(e)


def apply_reddit_refresh_token(refresh_token: str) -> tuple[bool, str]:
    if not refresh_token:
        return False, "no refresh token given"
    try:
        cfg = json.loads(config.GALLERY_DL_CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        return False, f"couldn't read {config.GALLERY_DL_CONFIG_FILE}: {e}"
    cfg.setdefault("extractor", {}).setdefault("reddit", {})["refresh-token"] = refresh_token
    config.GALLERY_DL_CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return True, "Saved. Reddit OAuth is now configured."


def run_reddit_shared_test_capture(subreddit: str = "pics", timeout: float = 30.0) -> tuple[bool, str]:
    """--simulate test of gallery-dl's own built-in shared OAuth client - no app registration
    needed, nothing gets saved. Same command the console version runs, just with output
    captured for display instead of streamed straight to the console."""
    subreddit = subreddit.strip() or "pics"
    try:
        result = subprocess.run(
            ["gallery-dl", "--simulate", f"https://www.reddit.com/r/{subreddit}/", "--range", "1-3"],
            capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip() or "(no output - check errors below)"
    except FileNotFoundError:
        return False, "gallery-dl isn't on PATH - check diagnostics."
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s."
    except OSError as e:
        return False, str(e)


def apply_hydrus_key(api_key: str) -> tuple[bool, str]:
    """Writes the Hydrus Client API key into hydownloader's import-jobs script. Split out from
    _configure_hydrus_key so both the console flow and the web UI's route write the key the
    exact same way (regex-replacing apiURL/apiKey in the file) instead of drifting apart."""
    if not api_key:
        return False, "no API key given"
    if not config.IMPORT_JOBS_FILE.exists():
        return False, f"{config.IMPORT_JOBS_FILE} not found - has hydownloader been set up yet?"
    content = config.IMPORT_JOBS_FILE.read_text(encoding="utf-8-sig")
    content = re.sub(r"(apiURL\s*=\s*)['\"][^'\"]*['\"]", rf'\1"{config.HYDRUS_API_URL}"', content)
    content = re.sub(r"(apiKey\s*=\s*)['\"][^'\"]*['\"]", rf'\1"{api_key}"', content)
    config.IMPORT_JOBS_FILE.write_text(content, encoding="utf-8")
    return True, "Saved. Hydrus API key is now configured. (This does NOT touch hydownloader-config.json's own separate 'daemon.access-key' secret.)"


def show_status() -> None:
    reddit_ok, reddit_cid = get_reddit_status()
    hydrus_ok, hydrus_key = get_hydrus_key_status()
    print()
    print("=====================================================")
    print(" CURRENT CREDENTIAL STATUS")
    print("=====================================================")
    print(f"1) Hydrus API key: {'configured - ' + mask_secret(hydrus_key) if hydrus_ok else 'NOT configured'}")
    missing = [s["label"] for s in list_service_key_statuses() if not s["configured"]]
    if not reddit_ok:
        missing.insert(0, "Reddit (optional - shared client works without it)")
    if missing:
        print(f"2) Other services: {len(missing)} missing a key ({', '.join(missing)}) - see option 2 below")
    else:
        print("2) Other services: all configured")
    print("=====================================================")


def _configure_reddit_oauth() -> None:
    print()
    print("Note: this step is optional. gallery-dl already ships with its own built-in, shared")
    print("OAuth client and uses it automatically if you skip this - that's normally enough for")
    print("downloading public subreddits/galleries. Registering your own app below only buys you")
    print("a private (non-shared) rate limit and access to quarantined/private subreddits. Try")
    print("option 2 first if you just want to confirm Reddit downloading works at all.")
    print()
    print("You need a Reddit 'installed app' to get OAuth working:")
    print("  1. Log into Reddit, go to https://www.reddit.com/prefs/apps")
    print("  2. Click 'create another app', choose type 'installed app' (NOT web app/script)")
    print("  3. Set redirect uri to: http://localhost:6414/")
    print("  4. Click 'create app', then copy the client ID (the string under the app name)")
    print()
    print("If the CAPTCHA on that page won't load: this has been a known issue since Reddit")
    print("rolled out a 'Responsible Builder Policy' requiring manual approval for new apps")
    print("(mid-2026). File a ticket at https://support.reddithelp.com/hc/en-us/requests/new")
    print("(category: Developer Platform & Data API Usage) - describe the use case as personal,")
    print("non-commercial archival, not scraping/resale. No published turnaround time.")
    webbrowser.open("https://www.reddit.com/prefs/apps")
    client_id = input("Paste your Reddit client ID here (or press Enter to cancel): ").strip()
    if not client_id:
        print("Cancelled.")
        return

    reddit_username = input("Your Reddit username (for the User-Agent header): ").strip()
    ok, msg = apply_reddit_app_config(client_id, reddit_username)
    if not ok:
        print(msg)
        return

    print()
    print(">>> Running the OAuth authorization flow (will open a browser tab)...")
    subprocess.run(["gallery-dl", "oauth:reddit"])
    print()
    print("Copy the 'refresh-token' gallery-dl just printed:")
    refresh_token = input("Paste it here to save it automatically (or press Enter to edit the file by hand later): ").strip()
    if refresh_token:
        _, msg = apply_reddit_refresh_token(refresh_token)
        print(msg)
    else:
        print(f"OK - edit {config.GALLERY_DL_CONFIG_FILE} by hand later (extractor.reddit.refresh-token).")


def _test_reddit_shared_client() -> None:
    print()
    print("This tests whether gallery-dl can already reach Reddit using its own built-in shared")
    print("OAuth client - no app registration needed. Uses --simulate, so nothing gets saved.")
    test_sub = input("Subreddit to test against (Enter for 'pics'): ").strip() or "pics"
    print()
    print(f">>> Running: gallery-dl --simulate https://www.reddit.com/r/{test_sub}/ --range 1-3")
    subprocess.run(["gallery-dl", "--simulate", f"https://www.reddit.com/r/{test_sub}/", "--range", "1-3"])
    print()
    print("If file URLs printed above with no errors: Reddit downloading already works right")
    print("now via the shared default client. You likely don't need option 1 unless you want")
    print("better rate limits or access to quarantined/private subreddits.")
    print("If it errored (rate-limit / blocked / auth error) instead: option 1, or a support")
    print("ticket if the CAPTCHA is still broken, is the next step.")


def _configure_service_keys() -> None:
    """Reddit gets folded in here alongside every other gallery-dl service (its own dedicated
    OAuth sub-flow, same as the web UI's "Other Services" card) rather than keeping separate
    top-level menu options - Hydrus is the only credential that stays on its own listing, since
    it isn't a gallery-dl extractor at all."""
    reddit_ok, reddit_cid = get_reddit_status()
    reddit_row = {"id": "reddit", "label": "Reddit", "configured": reddit_ok}
    statuses = [reddit_row] + list_service_key_statuses()
    print()
    print("All known services:")
    for i, s in enumerate(statuses, start=1):
        status = "configured" if s["configured"] else "NOT configured"
        print(f"  [{i}] {s['label']} - {status}")
    print(f"  [{len(statuses) + 1}] back")
    choice = input("Choice: ").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(statuses)):
        return
    picked = statuses[int(choice) - 1]

    if picked["id"] == "reddit":
        print()
        print("  [1] (Re)configure Reddit OAuth (custom app - better rate limits + private/quarantined subs)")
        print("  [2] Test Reddit downloads WITHOUT a custom app (uses gallery-dl's built-in shared client)")
        sub = input("Choice: ").strip()
        if sub == "1":
            _configure_reddit_oauth()
        elif sub == "2":
            _test_reddit_shared_client()
        return

    entry = next(e for e in SERVICE_KEY_REGISTRY if e["id"] == picked["id"])
    print()
    print(entry["note"])
    webbrowser.open(entry["url"])
    values = {}
    for field in entry["fields"]:
        values[field["key"]] = input(f"Paste {field['label']} (or press Enter to skip): ").strip()
    ok, msg = apply_service_key(entry["id"], values)
    print(msg)


def _configure_hydrus_key() -> None:
    print()
    print("Get this from Hydrus: services -> manage services -> enable Client API (port 45869),")
    print("then services -> review services -> Client API -> generate a new access key.")
    api_key = input("Paste your Hydrus Client API access key now (or press Enter to cancel): ").strip()
    if not api_key:
        print("Cancelled.")
        return
    _, msg = apply_hydrus_key(api_key)
    print(msg)


def run() -> None:
    while True:
        show_status()
        print()
        print("What do you want to do?")
        print("  [1] (Re)configure Hydrus API key")
        print("  [2] Configure a service key (Reddit, Tumblr, Imgur, DeviantArt, ...)")
        print("  [Q] Done / close")
        choice = input("Choice: ").strip()

        if choice == "1":
            _configure_hydrus_key()
        elif choice == "2":
            _configure_service_keys()
        elif choice.lower() in ("q", "quit", "exit"):
            print("Done.")
            break
        else:
            print("Not a valid choice.")

    print()
    input("Press Enter to continue")
