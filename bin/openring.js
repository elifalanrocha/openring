#!/usr/bin/env node
// One-command launcher: installs uv (which brings its own Python) if needed,
// runs OpenRing from PyPI, and opens the dashboard when the server is up.
"use strict";
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");

// The PyPI release with this launcher's own version; OPENRING_SOURCE runs a checkout or a git URL instead.
const VERSION = require("../package.json").version;
const SOURCE = process.env.OPENRING_SOURCE;
const PACKAGE = SOURCE ? `openring[kokoro,whisper] @ ${SOURCE}` : `openring[kokoro,whisper]==${VERSION}`;
// Flags such as `--port 8001` go on to `openring`; the launcher reads the port to open the browser.
const userArgs = process.argv.slice(2);
// `npx openring mcp` is an MCP server: its stdout carries the protocol, so the launcher speaks on stderr only.
const MCP = userArgs[0] === "mcp";
const say = (...args) => (MCP ? console.error : console.log)(...args);
const portArg = userArgs.find((_, i) => userArgs[i - 1] === "--port") || (userArgs.find((a) => a.startsWith("--port=")) || "").slice(7);
const PORT = portArg || process.env.PORT || "8000";
const isWin = process.platform === "win32";

function findUv() {
  const candidates = ["uv"];
  const home = os.homedir();
  candidates.push(path.join(home, ".local", "bin", isWin ? "uv.exe" : "uv"));
  candidates.push(path.join(home, ".cargo", "bin", isWin ? "uv.exe" : "uv"));
  for (const c of candidates) {
    const r = spawnSync(c, ["--version"], { stdio: "ignore" });
    if (r.status === 0) return c;
  }
  return null;
}

function installUv() {
  say("  Installing uv (Python manager, one time only)...");
  const r = isWin
    ? spawnSync("powershell", ["-ExecutionPolicy", "ByPass", "-NoProfile", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"], { stdio: MCP ? ["ignore", 2, 2] : "inherit" })
    : spawnSync("sh", ["-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"], { stdio: MCP ? ["ignore", 2, 2] : "inherit" });
  if (r.status !== 0) {
    console.error("\n  Could not install uv automatically. Install it from https://docs.astral.sh/uv/ and run this command again.");
    process.exit(1);
  }
}

function openBrowser(url) {
  if (process.env.OPENRING_NO_BROWSER) return;
  const cmd = isWin ? ["cmd", ["/c", "start", "", url]] : process.platform === "darwin" ? ["open", [url]] : ["xdg-open", [url]];
  try { spawn(cmd[0], cmd[1], { stdio: "ignore", detached: true }).unref(); } catch (_) {}
}

function waitForServer(url, tries = 600) {
  return new Promise((resolve) => {
    const tick = (n) => {
      http.get(url + "/health", (res) => { res.resume(); res.statusCode === 200 ? resolve(true) : retry(n); }).on("error", () => retry(n));
    };
    const retry = (n) => (n <= 0 ? resolve(false) : setTimeout(() => tick(n - 1), 1000));
    tick(tries);
  });
}

let uv = findUv();
if (!uv) { installUv(); uv = findUv(); }
if (!uv) { console.error("  uv was installed but is not on PATH yet. Open a new terminal and run the command again."); process.exit(1); }

// Right after a release, uv may still hold an older copy of PyPI's index (or PyPI's CDN is still catching up) and
// answer that this version doesn't exist. Then ask PyPI again, a few times with a pause between: the CDN takes
// about a minute. (Refreshing on every start would break starting without internet.)
const STALE_INDEX = /no version of openring|No solution found when resolving/i;
const RETRIES = 3;
const RETRY_WAIT_MS = Number(process.env.OPENRING_RETRY_SECONDS ?? 20) * 1000;
const colors = process.stderr.isTTY ? { LOGURU_COLORIZE: "True" } : {};

// Whether a run that ended with `code` should be tried again: it failed saying the version isn't there (yet),
// this is a PyPI release (not OPENRING_SOURCE), and it was tried fewer than RETRIES times again.
function shouldRetry(code, attempt, seen, fromSource) {
  return Boolean(code) && !fromSource && attempt < RETRIES && STALE_INDEX.test(seen);
}

let waiting = null; // the pause before the next try

function run(attempt) {
  const refresh = attempt > 0;
  const args = ["tool", "run", ...(process.stderr.isTTY ? ["--color", "always"] : []),
    ...(refresh ? ["--refresh-package", "openring"] : []), "--python", "3.12", "--from", PACKAGE, "openring", ...userArgs];
  // stderr passes through as it comes; the launcher also reads it to spot the stale index.
  const child = spawn(uv, args, { stdio: ["inherit", "inherit", "pipe"], env: { ...process.env, PORT, ...colors } });
  let seen = "";
  child.stderr.on("data", (chunk) => {
    process.stderr.write(chunk);
    if (seen.length < 20000) seen += chunk.toString();
  });
  child.on("exit", (code) => {
    if (shouldRetry(code, attempt, seen, Boolean(SOURCE))) {
      say(`\n  OpenRing ${VERSION} is brand new and PyPI may not list it everywhere yet; asking again in ${RETRY_WAIT_MS / 1000} s (try ${attempt + 2} of ${RETRIES + 1})...\n`);
      waiting = setTimeout(() => { waiting = null; current = run(attempt + 1); }, RETRY_WAIT_MS);
      return;
    }
    process.exit(code ?? 0);
  });
  return child;
}

if (!MCP) console.log("\n  Starting OpenRing (the first run downloads dependencies and can take a few minutes)...\n");
let current = run(0);
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    if (waiting) process.exit(130); // between two tries there is no child to stop: leave
    current.kill(sig);
  });
}

const url = `http://localhost:${PORT}`;
if (!MCP) waitForServer(url).then((up) => { if (up) openBrowser(url); });
