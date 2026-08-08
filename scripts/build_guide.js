const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, TableOfContents,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat, AlignmentType, PageBreak
} = require("docx");

const FONT = "Calibri";
const MONO = "Consolas";

function h1(text) {
  return new Paragraph({ text, heading: HeadingLevel.HEADING_1, spacing: { before: 400, after: 200 } });
}
function h2(text) {
  return new Paragraph({ text, heading: HeadingLevel.HEADING_2, spacing: { before: 300, after: 150 } });
}
function h3(text) {
  return new Paragraph({ text, heading: HeadingLevel.HEADING_3, spacing: { before: 200, after: 100 } });
}
function p(text, opts = {}) {
  return new Paragraph({
    spacing: { after: 160 },
    children: [new TextRun({ text, font: FONT, size: 22, ...opts })],
  });
}
function pMixed(runs, opts = {}) {
  return new Paragraph({ spacing: { after: 160 }, ...opts, children: runs });
}
function code(text) {
  const lines = text.split("\n");
  return lines.map((line, i) => new Paragraph({
    spacing: { after: i === lines.length - 1 ? 160 : 0 },
    shading: { type: ShadingType.CLEAR, fill: "F0F0F0" },
    children: [new TextRun({ text: line.length ? line : " ", font: MONO, size: 20 })],
  }));
}
function bullet(text, level = 0) {
  return new Paragraph({
    numbering: { reference: "bullets", level },
    spacing: { after: 100 },
    children: [new TextRun({ text, font: FONT, size: 22 })],
  });
}
function bulletMixed(runs, level = 0) {
  return new Paragraph({
    numbering: { reference: "bullets", level },
    spacing: { after: 100 },
    children: runs,
  });
}
function bold(text) { return new TextRun({ text, font: FONT, size: 22, bold: true }); }
function reg(text) { return new TextRun({ text, font: FONT, size: 22 }); }
function mono(text) { return new TextRun({ text, font: MONO, size: 20, shading: { type: ShadingType.CLEAR, fill: "F0F0F0" } }); }

function twoColTable(rows, headers, widths) {
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((htext, i) => new TableCell({
      width: { size: widths[i], type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: "2F5496" },
      children: [new Paragraph({ children: [new TextRun({ text: htext, bold: true, color: "FFFFFF", font: FONT, size: 20 })] })],
    })),
  });
  const dataRows = rows.map(r => new TableRow({
    children: r.map((cellText, i) => new TableCell({
      width: { size: widths[i], type: WidthType.DXA },
      children: [new Paragraph({ children: [new TextRun({ text: cellText, font: FONT, size: 20 })] })],
    })),
  }));
  return new Table({
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA },
    columnWidths: widths,
    rows: [headerRow, ...dataRows],
  });
}

const bodyChildren = [
  new Paragraph({
    children: [new TextRun({ text: "Undertow", bold: true, size: 48, font: FONT })],
    spacing: { after: 80 },
  }),
  new Paragraph({
    children: [new TextRun({ text: "Setup Verification, Maintenance & Usage Guide", size: 28, font: FONT, color: "555555" })],
    spacing: { after: 400 },
  }),
  p("This document covers the gallery-dl -> hydownloader -> Hydrus pipeline installed on this machine: what each piece does, how to run it day to day, and how to fix things when they break. It's organized into two halves: Usage (how to actually use the thing) and Technical Maintenance (how to fix it)."),

  new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-3" }),
  new Paragraph({ children: [new PageBreak()] }),

  h1("Verification Summary"),
  p("As of this writing, the pipeline was tested end-to-end and confirmed working:"),
  bullet("Hydrus Network, the hydownloader daemon, and hydownloader-systray all start correctly and stay running without spawning duplicate instances."),
  bullet("A single-URL download was queued through the systray, downloaded by gallery-dl, automatically picked up by the hydownloader daemon, and imported into Hydrus with hydownloader's provenance tags attached (hydl-import-time, hydl-src-site, hydl-url-id)."),
  bullet("The imported file was confirmed searchable and viewable inside Hydrus."),
  bullet("Registering a custom Reddit OAuth app is currently blocked - Reddit's self-serve “create app” page is broken on Reddit's side (the CAPTCHA never loads, confirmed across multiple attempts and machines), and the replacement process requires manual review via a Reddit support ticket. This turns out not to fully block Reddit downloading, though: gallery-dl ships with its own built-in, pre-registered shared OAuth client and uses it automatically whenever no custom client-id is configured, so public-subreddit downloads should already work with zero setup (see Configure-ApiKeys.ps1, option 2, to test this). A custom app is only needed for a private rate-limit allowance and access to quarantined/private subreddits - Bluesky, Mastodon, Tumblr, Pixiv, and every other gallery-dl-supported site are unaffected regardless."),

  new Paragraph({ children: [new PageBreak()] }),
  h1("Usage"),

  h2("Opening the Pipeline"),
  p("Double-click the “Undertow” shortcut on the Desktop. This runs Launch-HydrusPipeline.ps1, which:"),
  bullet("Starts Hydrus, the hydownloader daemon, and hydownloader-systray - but only whichever of these isn't already running. It never opens a second copy of anything."),
  bullet("Drops you into a text menu in the PowerShell window with four choices."),
  ...code("[1] Search / Discover   - add new subscriptions or single downloads (opens the systray)\n[2] Organize / Tag      - bring Hydrus to the front to tag and sort files\n[3] View / Browse       - bring Hydrus to the front to search and view your library\n[4] Configure API keys  - see/set Reddit and Hydrus credentials\n[Q] Quit - shuts down anything idle, leaves anything busy running"),
  p("Picking 1, 2, or 3 just brings the right window to the front - it doesn't relaunch anything. Picking Q checks whether the hydownloader daemon is actively mid-download or mid-subscription-check: if it's idle, Q shuts it (and the systray) down cleanly; if it's busy, it leaves that piece running and only closes the systray. Simply closing the window does the same idle-check-then-shutdown automatically, so you never have to remember to pick Q first. Hydrus itself is never auto-closed either way - close it yourself when you're done with it. You can reopen the shortcut any time to get the menu back without disturbing whatever's still running."),
  pMixed([bold("Note: "), reg("Options 2 and 3 both bring the same Hydrus window forward - Hydrus doesn't have separate tagging and viewing windows, it's one client where the search results, thumbnails, and tag panel all live side by side. The menu split just reflects the two things you're most likely doing there.")]),

  h2("Search / Discover - Adding Downloads"),
  p("This is what the hydownloader-systray window (menu option 1) is for. There are two ways to bring content in:"),

  h3("Single URL downloads (one-off)"),
  p("Use this for a single post, image, or gallery you want right now."),
  bullet("In the systray, go to the “Single URL queue” tab."),
  bullet("Click “Add...”"),
  bullet("Paste the URL (one per line if adding several)."),
  bullet("Uncheck “Start paused” if you want it to download immediately (leave it checked if you want to review/edit it first - see below)."),
  bullet("Click OK. The daemon picks it up within a few seconds."),
  p("Watch the Status column: it goes from a queued state to either a green “ok” or a red error. Click the “Logs” tab, then “Load single URL log...” and enter the row's ID to see exactly what gallery-dl did - this is the fastest way to diagnose a failed download (see Troubleshooting)."),
  pMixed([bold("Important: "), reg("successfully imported files are automatically removed from the downloaded folder once Hydrus has them. If you check the downloaded folder and it looks empty, that's expected - it does not mean nothing happened. Check Hydrus itself, or the systray's “Import history” tab, to confirm an import.")]),

  h3("Subscriptions (repeated / ongoing)"),
  p("Use this for an artist, account, or search you want to keep getting new content from automatically."),
  bullet("In the systray, go to the “Subscriptions” tab and click “Add...”"),
  bullet("Pick a Downloader (the site) and enter Keywords (usually a username, tag, or search term - for sites gallery-dl supports but hydownloader doesn't have a dedicated downloader for, use downloader “raw” and put the full gallery URL in Keywords)."),
  bullet("Set a Check interval (how often to look for new files) and leave the other fields at their defaults unless you know you need something specific."),
  p("The daemon checks each subscription on its own schedule and pulls in anything new, same auto-import behavior as single URLs."),

  h2("Categorizing / Tagging"),
  p("This happens in Hydrus itself (menu option 2 or 3 - same window)."),
  bullet("Open a search page (or use the one already open) and select one or more thumbnails."),
  bullet("The tag panel on the right side of the window shows existing tags and an entry box for adding new ones. Start typing and Hydrus will autocomplete against tags it already knows about."),
  bullet("Files downloaded through hydownloader already carry some automatic tags (hydl-import-time, hydl-src-site, hydl-sub-id, hydl-url-id, and site-specific tags depending on the extractor - artist/character/etc. where the site provides them). Add your own tags on top of these freely."),
  bullet("To tag many files at once, select them all (Ctrl-click or drag a box) before typing in the tag panel - it applies to the whole selection."),
  p("Favourite tags, tag namespaces (like “creator:” or “series:”), and parent/sibling tag relationships are all standard Hydrus features - see Hydrus's own built-in help (Help menu) for the deeper tagging system if you want to build out a more structured tag hierarchy for the cube-adjacent workflows or anything else you're organizing."),

  h2("Setting Download Locations"),
  p("There are two distinct “locations” in this pipeline and they're easy to mix up:"),
  bulletMixed([bold("Where gallery-dl drops files before import: "), reg("this is the "), mono("downloaded"), reg(" folder inside the hydownloader data directory. It's organized into subfolders by site (reddit, bluesky, mastodon, tumblr, pixiv, etc.) as configured in "), mono("gallery-dl-config.json"), reg(". Files only sit here briefly - once the daemon imports them into Hydrus, they're removed from this folder.")]),
  bulletMixed([bold("Where Hydrus actually stores your library: "), reg("this is Hydrus's own internal file storage, managed entirely by Hydrus (database → manage database → move media files, inside Hydrus). You generally never touch this directly - it's not a normal browsable folder of individual files, Hydrus manages it internally.")]),
  p("To change where gallery-dl saves new site subfolders (e.g. renaming or restructuring the reddit/bluesky/etc. layout), edit the “directory” block under extractor in gallery-dl-config.json. To change where Hydrus stores its actual file library (e.g. moving it to a bigger drive), use Hydrus's own database → manage database → move media files dialog - don't move the folders by hand outside Hydrus."),

  h2("Viewing Images"),
  bullet("Bring Hydrus to the front (menu option 2 or 3)."),
  bullet("Type tags into the search box (top left) to filter, or leave it blank, add the system:everything predicate, and hit the search icon to browse your whole library."),
  bullet("Double-click any thumbnail to open the full media viewer. Arrow keys move to the next/previous file."),
  bullet("Use the sort dropdown (top of the results panel) to sort by import time, filesize, etc."),
  bullet("The tag panel on the right shows tags for whatever's currently selected/open, which is also where you search by clicking existing tags to add them to your search."),

  h2("Cleanly Exiting"),
  p("Closing everything down in the right order avoids interrupted downloads and half-written database state:"),
  bullet("Close Hydrus first: File → Exit (or just the window's X button - Hydrus does its own safe-shutdown/backup routine on close, this can take a few seconds, let it finish)."),
  bullet("Close the systray: right-click its window and close it, or use its own exit option from the Menu button."),
  bullet("The hydownloader daemon runs minimized with no visible window. It's safe to just leave it running in the background even after Hydrus and the systray are closed - it'll just sit idle. If you want to stop it too, run Stop-HydrusPipelineServices.ps1 (see Technical Maintenance)."),
  p("You do not need to shut anything down between sessions if you don't want to - it's fine to leave the whole pipeline running continuously (subscriptions need the daemon running to check on schedule anyway). The shortcut is safe to double-click at any time regardless of what's currently running. And as noted above, the launcher's Q option (and simply closing its window) already only shuts down whatever's actually idle - you don't need to manually check for in-progress downloads before quitting."),

  new Paragraph({ children: [new PageBreak()] }),
  h1("Extended Usage"),
  p("Everything above covers the everyday flow. This section goes deeper into the properties and features that give you more control once you're past just getting things downloading and want to fine-tune it, plus some concrete example flows."),

  h2("Per-download properties (single URLs and subscriptions)"),
  p("Every single URL and every subscription has a set of properties you can edit in the systray before (or after) it runs. The most useful ones:"),
  twoColTable(
    [
      ["Priority", "Higher numbers download first. Default is 0. Bump up anything you want to jump the queue."],
      ["Paused", "Paused entries are never processed. Add something paused, edit its other properties, then unpause it - this is the safe way to configure a download before it fires."],
      ["Filter", "A gallery-dl filter expression limiting what's downloaded (by file type, extension, size, etc. - whatever the site's extractor exposes). See gallery-dl's own filter documentation for the expression syntax."],
      ["Max files", "Caps how many files this URL/subscription check will pull. Single URLs default to unlimited; subscriptions have separate caps for the very first check vs. every check after."],
      ["Additional data", "Freeform text - write comma-separated tags here and they get attached to whatever gets downloaded from this URL/subscription, on top of the automatic hydl- tags."],
      ["Overwrite existing / Ignore anchor", "Force a redownload of files hydownloader already knows about. Normally it skips anything already seen - use these two together if you deliberately want to re-pull something."],
      ["Abort after (subscriptions only)", "Stops a subscription check once it's seen this many already-downloaded files in a row. Raise this for subscriptions on very active accounts if you're worried about missing older backlog."],
      ["Worker ID (subscriptions only)", "Which internal thread a subscription runs on - an advanced multithreading knob. Leave this alone unless you specifically need to parallelize checks across many subscriptions."],
    ],
    ["Property", "What it does"],
    [3000, 6700]
  ),
  p("The general pattern: add new entries paused, dial in Filter / Max files / Additional data / Priority to taste, then unpause."),

  h2("Downloading from sites hydownloader doesn't have a dedicated integration for"),
  p("hydownloader recognizes URLs for a specific list of sites and turns them into downloader-plus-keywords subscription pairs. For anything gallery-dl supports but hydownloader doesn't specifically recognize, use the “raw” downloader: set Downloader to raw and put the full gallery/search URL directly in Keywords. Everything else - check interval, filters, tags, etc. - works exactly the same as a normal subscription."),

  h2("Setting defaults so you don't repeat yourself"),
  p("If you find yourself setting the same Filter, Max files, or other property on every new subscription for a given site (or on everything in general), you can set defaults once in hydownloader-config.json instead:"),
  bullet("url-defaults - applies to every new single URL added via the API/systray."),
  bullet("subscription-defaults-any - applies to every new subscription, regardless of site."),
  bullet("subscription-defaults-<downloadername> - applies only to new subscriptions for that specific downloader (e.g. subscription-defaults-gelbooru), and takes priority over -any."),
  p("Use the same lowercase, underscored property names the database uses (e.g. max_files, abort_after, worker_id) - these usually match the GUI label with spaces replaced by underscores. Example:"),
  ...code("\"url-defaults\": {\n  \"max_files\": 50\n},\n\"subscription-defaults-any\": {\n  \"abort_after\": 2000\n}"),
  p("These only affect newly-created entries - they won't retroactively change subscriptions or URLs you already added."),

  h2("Quick mode - for when you need to shut down soon"),
  p("Quick mode tells the daemon to skip starting subscription checks it estimates will take a while, so you're not stuck waiting on a long gallery scan right when you need your PC free. Toggle it on demand from the systray, or schedule it to turn on automatically during recurring time windows via daemon.enable-quick-mode and daemon.quick-mode-time-intervals in hydownloader-config.json. You can also exempt specific subscriptions or downloaders from ever being skipped, via quick-mode-subscription-id-blacklist / quick-mode-downloader-blacklist."),

  h2("Multiple accounts / cookies for the same site"),
  p("hydownloader supports rotating between different gallery-dl config files, cookies, and cache files based on rules (URL pattern, subscription ID, worker, or downloader) via the cfg-files-rules key in hydownloader-config.json. This is an experimental, advanced feature - it's meant for cases like running two logged-in accounts on the same booru to spread out rate limits, or using a different cookie jar per site. Separately, and automatically, hydownloader also rotates plain cookies.txt files it receives via its own API (e.g. from Hydrus Companion) without you needing to configure anything, always keeping the newest one active."),

  h2("Organizing at scale in Hydrus"),
  p("As your library grows past a few thousand files, Hydrus's own organizing tools do most of the heavy lifting:"),
  bulletMixed([bold("Tag namespaces "), reg("(text before a colon, like "), mono("creator:"), reg(" or "), mono("series:"), reg(") group related tags and let you filter/sort by category instead of one flat tag soup.")]),
  bulletMixed([bold("Tag siblings and parents "), reg("(tags -> manage tag siblings/parents in Hydrus) let you merge alternate spellings into one canonical tag, or make one tag automatically imply another (e.g. a character tag implying its series). Set these up once and they apply retroactively across your whole library.")]),
  bulletMixed([bold("The duplicate filter "), reg("(a built-in Hydrus feature for comparing and resolving near-identical/duplicate files) is worth running periodically on anything downloaded from multiple sources, since the same piece of art often gets re-uploaded across sites at different resolutions. See Hydrus's own Help menu for a full walkthrough - it's a big enough feature to have its own documentation there.")]),

  h2("Example flows"),

  h3("Following a new artist across several sites"),
  bullet("Add one subscription per site the artist posts to (systray -> Subscriptions -> Add), using their username/handle as Keywords."),
  bullet("If they're on a platform without dedicated hydownloader support, use downloader raw with the full profile/gallery URL as Keywords instead."),
  bullet("Set Additional data on each to something like artist:theirname so every file from any of these subscriptions gets that tag automatically, regardless of what the site itself provides natively."),
  bullet("Leave Check interval at a sensible default (a few hours to a day) unless they post unusually often."),

  h3("One-off archiving a whole gallery or thread"),
  bullet("Add it as a single URL, left paused."),
  bullet("Set Max files to 0/blank for no limit (or a specific number if you only want the first N)."),
  bullet("Add any tags you want in Additional data, then unpause."),
  bullet("If you need to re-grab it later (e.g. new pages were added), re-add the same URL with Overwrite existing and Ignore anchor both on - otherwise hydownloader just skips everything it already has."),

  h3("A subscription keeps erroring / redownloading the same files"),
  bullet("Check the subscription's log (systray Logs tab -> “Load subscription log...”) for the actual error - a persistent per-post error on one site is the classic cause (see the note on archive-mode in Technical Maintenance -> Common Issues & Fixes)."),
  bullet("If it's rate-limiting (HTTP 429), lower how aggressively it's checked: increase Check interval, or reduce Max files (regular check) so each run pulls less."),
  bullet("If one very noisy subscription is affecting everything else, consider raising its Abort after so it stops scanning sooner once it hits already-seen files."),

  h3("Getting through a busy week without missing subscriptions"),
  bullet("Turn on Quick mode from the systray before a stretch where you might need to shut your PC down on short notice - the daemon will skip starting subscription checks estimated to run long, but will still run quick ones."),
  bullet("Anything skipped this way isn't lost - it'll simply be picked up on its next scheduled check once Quick mode is off."),

  h3("Cleaning up a library with a lot of cross-posted duplicates"),
  bullet("Set up tag siblings/parents first (Hydrus -> tags -> manage tag siblings/parents) so re-tagging survives the dedup pass."),
  bullet("Run Hydrus's duplicate filter (see its Help menu) periodically to compare visually similar files and pick which copy - usually the highest quality one - to keep."),
  bullet("Favor keeping the copy with the most complete tags/source info if quality is otherwise equal - Hydrus can merge tags from the discarded duplicate onto the keeper as part of resolving the pair."),

  new Paragraph({ children: [new PageBreak()] }),
  h1("Technical Maintenance"),

  h2("File & Folder Reference"),
  twoColTable(
    [
      ["C:\\Users\\Matt\\HydrusPipeline\\hydrus\\", "Hydrus Network install (hydrus_client.exe)"],
      ["C:\\Users\\Matt\\HydrusPipeline\\hydownloader\\", "hydownloader source clone (poetry project - daemon runs from here)"],
      ["C:\\Users\\Matt\\HydrusPipeline\\hydownloader-data\\", "The hydownloader database folder - config, queues, logs"],
      ["  \\hydownloader-config.json", "hydownloader's own settings (daemon host/port/access-key, quick mode, etc.)"],
      ["  \\hydownloader-import-jobs.py", "Hydrus API URL + access key live here (defAPIURL / defAPIKey)"],
      ["  \\downloaded\\", "Where gallery-dl drops files before import (emptied automatically after import)"],
      ["  \\logs\\", "Daemon/systray stdout+stderr logs and per-URL gallery-dl logs"],
      ["C:\\Users\\Matt\\HydrusPipeline\\hydownloader-systray\\", "The separate, prebuilt native systray app + its settings.ini"],
      ["C:\\Users\\Matt\\gallery-dl\\config.json", "Base gallery-dl config (directory layout, site extractor settings)"],
      ["Desktop\\Undertow.lnk", "The shortcut - runs Launch-HydrusPipeline.ps1"],
    ],
    ["Path", "What it is"],
    [5500, 4200]
  ),

  h2("The Scripts"),
  p("All scripts live in the same folder this document was generated in. They're all safe to re-run any time."),
  bulletMixed([bold("Setup-HydrusPipeline.ps1"), reg(" - full install/repair script. Installs prerequisites via winget if missing, clones/updates hydownloader, initializes the database, patches any missing config keys, downloads the systray if needed, and starts everything. Use this for first-time setup or if something needs reinstalling.")]),
  bulletMixed([bold("Launch-HydrusPipeline.ps1"), reg(" - the lightweight day-to-day launcher (what the Desktop shortcut runs). Starts only what isn't already running, then shows the menu.")]),
  bulletMixed([bold("Configure-ApiKeys.ps1"), reg(" - shows current Reddit/Hydrus credential status (masked) and lets you set or replace either one. This is also reachable as option 4 from the launcher menu.")]),
  bulletMixed([bold("Stop-HydrusPipelineServices.ps1"), reg(" - stops the hydownloader daemon and systray only (leaves Hydrus itself running). Use this before re-running Setup if you've changed hydownloader-config.json by hand and need the daemon to pick up the new values, since editing the file doesn't affect an already-running process.")]),
  bulletMixed([bold("Create-DesktopShortcut.ps1"), reg(" - (re)creates the Desktop shortcut. Only needed if the shortcut gets deleted.")]),

  h2("Credential Management"),
  p("Two completely separate secrets are involved, and they're easy to confuse:"),
  twoColTable(
    [
      ["hydownloader's daemon.access-key", "Auto-generated by hydownloader itself. Used by the systray to authenticate to the hydownloader daemon's own API. Lives in hydownloader-config.json. Never set this by hand - if it's ever missing, Setup-HydrusPipeline.ps1 generates a fresh one automatically."],
      ["Hydrus Client API key", "Generated inside Hydrus (services → review services → Client API → generate access key). Used by hydownloader's importer to push downloaded files into Hydrus. Lives in hydownloader-import-jobs.py (defAPIKey). Set/changed via Configure-ApiKeys.ps1, option 2."],
    ],
    ["Secret", "Purpose / where it lives"],
    [3200, 6500]
  ),
  p("Reddit OAuth (client ID, user-agent, refresh token) lives in gallery-dl's config.json and is set via Configure-ApiKeys.ps1, option 1. This step is genuinely optional: gallery-dl includes its own built-in, shared default OAuth client and will use it automatically with no configuration at all, which is normally enough for downloading public subreddits and galleries. Configuring a custom app only buys you a private (non-shared) rate-limit allowance and access to quarantined/private subreddits - use Configure-ApiKeys.ps1, option 2, to test whether you actually need it before fighting with Reddit's app-creation page."),
  pMixed([bold("Why the app-creation page is broken: "), reg("as of mid-2026 Reddit formalized a “Responsible Builder Policy” requiring explicit manual approval before any new app gets full API access, on top of the CAPTCHA bug on the "), mono("reddit.com/prefs/apps"), reg(" page itself. The support-ticket route referenced above (via Reddit's own help center, category Developer Platform & Data API Usage) is the documented path around both issues, though there's no published turnaround time and small/personal-use requests are commonly rejected outright - worth describing the use case as personal/non-commercial archival, not scraping or resale, when filing it.")]),
  pMixed([bold("Credentials are never re-prompted automatically. "), reg("Setup-HydrusPipeline.ps1 only reports status (configured / not configured) on each run - it will not nag you to re-enter something you've already skipped. Use Configure-ApiKeys.ps1 (or menu option 4) whenever you actually want to add or change a credential.")]),

  h2("Common Issues & Fixes"),

  h3("“The client is already running” dialog in Hydrus"),
  p("This happens if Hydrus is launched a second time while already running (e.g. double-clicking hydrus_client.exe directly, or calling a generic “open app” action instead of focusing the existing window). Click “wait a bit, then try again” and close the dialog - do not click “force it” unless you're sure the old copy is actually stuck, since that can kill a healthy running instance. Always use the Desktop shortcut / launcher menu to reach Hydrus rather than launching hydrus_client.exe directly - the launcher checks for a running copy first and just brings its window forward instead of starting a new one."),

  h3("hydownloader daemon won't start / crashes immediately"),
  p("Usually caused by a config key hydownloader expects being missing from hydownloader-config.json (this happened once during setup - a KeyError on shared-db-override). Re-run Setup-HydrusPipeline.ps1: it scans the config for a known set of default keys and adds any that are missing, without touching existing values (so it's safe to re-run and won't regenerate the real access-key or overwrite anything you've configured). Check the daemon's stderr log afterward if it still won't start:"),
  ...code("C:\\Users\\Matt\\HydrusPipeline\\hydownloader-data\\logs\\daemon-launch-stderr.log"),

  h3("systray shows “Network error (status 0). Host not found”"),
  p("This means the systray's settings.ini has a blank or mismatched apiURL - usually because it was generated before the daemon's host/port/access-key were correctly written to hydownloader-config.json. Re-running Setup-HydrusPipeline.ps1 regenerates settings.ini from whatever's currently in the config. If the daemon was already running with a stale config when this happens, run Stop-HydrusPipelineServices.ps1 first, then Setup-HydrusPipeline.ps1, so the daemon actually restarts with the corrected values instead of continuing to run with what it loaded at its last start."),
  pMixed([reg("Note: hydownloader only actually serves HTTPS if "), mono("daemon.ssl"), reg(" is true "), bold("and"), reg(" a "), mono("server.pem"), reg(" file exists in the data folder - otherwise it silently falls back to plain HTTP regardless of the config flag. Setup-HydrusPipeline.ps1 checks for the actual file before deciding whether to write "), mono("https://"), reg(" or "), mono("http://"), reg(" into the systray's apiURL, so this should self-correct.")]),

  h3("A single URL or subscription shows a red “http error”"),
  p("Open the systray's Logs tab → “Load single URL log...” (or “Load subscription log...”) and enter the row's ID to see gallery-dl's actual request/response trail. A common cause is the source site rate-limiting (HTTP 429) - this happened during testing against a Wikimedia file with an unusually large revision history, where gallery-dl tried to fetch every historical revision and got rate-limited partway through. Retrying with a more specific/direct URL, or just waiting and retrying later, usually resolves it. This is a source-site behavior, not a pipeline bug."),

  h3("A subscription keeps redownloading files it already has"),
  p("hydownloader only marks a downloaded file as “seen” once a subscription check finishes cleanly. If a site has some persistent error on a specific post that a subscription passes every time it runs (or the daemon is interrupted mid-check), the check never fully finishes cleanly, so those files never get marked as seen - and the next check downloads them all over again (it won't re-save a file already on disk, but it does re-request it, wasting time and bandwidth). This is deliberate: hydownloader defaults to never marking files as downloaded on an errored check (gallery-dl's archive-mode: memory), because the alternative (archive-mode: file) risks silently skipping older backlog files instead. If one specific site is consistently affected, you can opt just that site into the faster-but-riskier behavior by adding \"archive-mode\": \"file\" to its block in gallery-dl-user-config.json. Either way, the systray's “missed subscription checks” list is the place to check for anything that might have been skipped, so you can go back and manually grab it."),

  h3("Reddit downloads don't work"),
  p("First, confirm this is actually happening - gallery-dl's built-in shared default OAuth client (see Credential Management above) should handle public subreddit and gallery downloads with zero configuration. Run Configure-ApiKeys.ps1, option 2, to test a public subreddit with --simulate (no files saved) and see whether it's actually failing."),
  bullet("If the test succeeds: Reddit downloading already works. The remaining blocker is only the custom-app registration (needed for a private rate-limit allowance and access to quarantined/private subreddits), which is still gated by Reddit's broken CAPTCHA / manual approval process - see the Verification Summary and Credential Management section above."),
  bulletMixed([bold("If the test fails with a rate-limit or "), reg("\"blocked by network security\"-style error: "), reg("the shared default client is being throttled (it's shared across every gallery-dl user, so this happens more the busier it gets). This is exactly the case a custom app's private client-id fixes - worth pushing through app registration (or the support-ticket route) for Reddit specifically, even though other sites don't need it.")]),
  bulletMixed([bold("If the test fails with an outright authentication error: "), reg("something in gallery-dl-config.json's reddit block may be malformed (e.g. a leftover placeholder client-id) - check extractor.reddit in the config file for a stray "), mono("\"client-id\": \"PLACEHOLDER_SET_BELOW\""), reg(" or similar and remove it so gallery-dl falls back to its own default.")]),

  h3("Need to fully restart everything from a clean state"),
  bullet("Run Stop-HydrusPipelineServices.ps1 (stops daemon + systray, leaves Hydrus running)."),
  bullet("Close Hydrus normally (File → Exit)."),
  bullet("Re-run Setup-HydrusPipeline.ps1, which will start all three fresh and re-verify the config on the way."),

  h2("Logs & Diagnostics"),
  twoColTable(
    [
      ["daemon-launch-stdout.log / -stderr.log", "hydownloader daemon's own console output"],
      ["systray-launch-stdout.log / -stderr.log", "systray's own console output"],
      ["single-urls-<ID>-gallery-dl-latest.txt", "Full gallery-dl request/response trail for one single-URL download (also viewable via the systray's Logs tab)"],
    ],
    ["File (under hydownloader-data\\logs)", "What it shows"],
    [4200, 5500]
  ),
  p("The systray's Status tab (“Request status update”) shows a live one-line summary per worker (single URLs, autoimporter, each subscription) - this is the fastest first check when something seems stuck."),

  h2("Updating hydownloader"),
  p("hydownloader is an actively developed project on GitLab (not GitHub - the upstream repo is gitgud.io/thatfuckingbird/hydownloader). To update to the latest version:"),
  bullet("Run Stop-HydrusPipelineServices.ps1 first."),
  ...code("cd C:\\Users\\Matt\\HydrusPipeline\\hydownloader\ngit pull\npython -m poetry install"),
  bullet("Then re-run Setup-HydrusPipeline.ps1 to restart everything and re-check the config for any new keys the update might have introduced."),
  p("hydownloader-systray is a separate repository with its own prebuilt binary releases. Setup-HydrusPipeline.ps1 currently downloads a specific pinned build; check gitgud.io/thatfuckingbird/hydownloader-assets for newer builds if you want to update it independently."),

  h2("Backing Up"),
  p("The two things worth backing up regularly:"),
  bulletMixed([bold("hydownloader-data\\"), reg(" - contains your subscriptions, download history/queue, and config. Losing this loses your subscription list and dedup history (though not your actual downloaded files, which live in Hydrus).")]),
  bulletMixed([bold("Hydrus's own database"), reg(" - this is your actual media library plus all tags. Use Hydrus's built-in database → backup database feature rather than copying files by hand, since Hydrus's storage isn't a simple flat folder structure.")]),
];

const doc = new Document({
  numbering: {
    config: [{
      reference: "bullets",
      levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 360, hanging: 260 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "◦", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 260 } } } },
      ],
    }],
  },
  sections: [{
    properties: {
      page: { size: { width: 12240, height: 15840 }, margin: { top: 1080, bottom: 1080, left: 1080, right: 1080 } },
    },
    children: bodyChildren,
  }],
});

Packer.toBuffer(doc).then(buf => {
  require("fs").writeFileSync(require("path").join(__dirname, "..", "docs", "Hydrus_Pipeline_Guide.docx"), buf);
  console.log("done, elements:", bodyChildren.length);
});
