"""Checks on the single-file dashboard that need no browser."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "openring" / "static" / "index.html").read_text(encoding="utf-8")


def run_js(code: str):
    """Run a script with Node.js and return what it prints as JSON (skips without Node.js)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node.js")
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=60,
                         check=True).stdout  # Node prints UTF-8; the system default would garble "·" on Windows
    return json.loads(out)


def test_campaigns_warn_about_calls_outside_8am_to_9pm_where_the_person_is():
    start = PAGE.index("const US_ZONES")
    code = PAGE[start:PAGE.index("\n}\n", PAGE.index("function callingHours")) + 2]
    checks = """
    const at = (numbers, start, end) => callingHours(numbers, new Date(start), end && new Date(end), "Europe/Lisbon")
      .map(x => x.zone + " " + x.at.toISOString());
    console.log(JSON.stringify({
      // 8 AM in New York on 2 Nov 2026 is 5 AM in Los Angeles, and 6 AM in Denver, 7 AM in Chicago.
      us: at(["+12125550100"], "2026-11-02T13:00:00Z"),
      hawaii: at(["+18085550100"], "2026-11-02T17:00:00Z"),
      saoPaulo: at(["+5511912345678"], "2026-11-02T12:00:00Z"),
      acre: at(["+5568912345678"], "2026-11-02T12:00:00Z"),
      lateWindow: at(["+5511912345678"], "2026-11-02T23:30:00Z", "2026-11-03T01:00:00Z"),
      unknownCode: at(["+999123456789"], "2026-11-02T06:00:00Z"),
      london: at(["+447700900123"], "2026-11-02T10:00:00Z"),
    }));
    """
    got = run_js(code + checks)
    assert got["us"] == ["America/Chicago 2026-11-02T13:00:00.000Z", "America/Denver 2026-11-02T13:00:00.000Z",
                         "America/Los_Angeles 2026-11-02T13:00:00.000Z"]
    # 7 AM in Honolulu, past 9 AM on the whole mainland: the area code says Hawaii.
    assert got["hawaii"] == ["Pacific/Honolulu 2026-11-02T17:00:00.000Z"]
    assert got["saoPaulo"] == []  # 9 AM
    assert got["acre"] == ["America/Rio_Branco 2026-11-02T12:00:00.000Z"]  # 7 AM: Acre is two hours behind Brasília
    # 8:30 PM to 10 PM in São Paulo: the first check past 9 PM is at 9:15 PM.
    assert got["lateWindow"] == ["America/Sao_Paulo 2026-11-03T00:15:00.000Z"]
    assert got["unknownCode"] == ["Europe/Lisbon 2026-11-02T06:00:00.000Z"]  # this computer's zone: 6 AM
    assert got["london"] == []


def test_the_assistant_editor_escapes_call_limits_from_an_imported_assistant():
    """An import saves what the other account has, even a value OpenRing would refuse: it must not run as HTML."""
    assert not re.findall(r"\$\{a\.messagePlan\?\.idle(?:TimeoutSeconds|MessageMaxSpokenCount)", PAGE)
    assert 'value="${limitsShown.silence}"' not in PAGE and 'value="${esc(limitsShown.silence)}"' in PAGE


def test_the_assistant_editor_keeps_a_limit_left_as_shown():
    line = next(x for x in PAGE.splitlines() if "const limitTyped" in x)
    got = run_js(line + """
    console.log(JSON.stringify({
      rounded: limitTyped("2", 2, 60, 90),  // 90 s, shown as 2 minutes and left alone
      unset: limitTyped("10", 10, 60, undefined) ?? "unset",
      emptied: limitTyped("", 2, 60, 90),
      typed: limitTyped("5", 2, 60, 90),
      seconds: limitTyped("20", 30, 1, undefined),
    }));""")
    assert got == {"rounded": 90, "unset": "unset", "emptied": 90, "typed": 300, "seconds": 20}
    # A limit under 30 s would round to 0 minutes, which the field refuses: nothing on the page could be saved.
    assert "minutes: Math.max(1, Math.round(" in PAGE


def test_nothing_else_is_called_app():
    # The page finds its root by this id; a form field with the same one would take its place.
    assert PAGE.count('id="app"') == 1


def test_dates_use_the_interface_language():
    # The page is in English; dates in the browser's language read "30 de out." among English words.
    assert not re.search(r"toLocale(Date|Time)?String\(\s*(\[\]|undefined)?\s*[,)]", PAGE)
    assert 'const LOCALE = "en-US"' in PAGE


def test_provider_disconnect_requires_a_second_click_before_expiry():
    helper_start = PAGE.find("const disconnectConfirmations")
    helper = PAGE[helper_start:PAGE.index("async function viewProviders")] if helper_start >= 0 else ""
    handlers = PAGE[PAGE.index("  const run = {", PAGE.index("async function viewProviders")):
                    PAGE.index('  v.querySelectorAll("[data-action]")', PAGE.index("async function viewProviders"))]
    got = run_js("""
    const assert = require('node:assert/strict');
    let now = 0, nextTimer = 0, requests = 0, refreshed = 0, failed = false;
    const timers = new Map();
    Date.now = () => now;
    const setTimeout = (fn) => { timers.set(++nextTimer, fn); return nextTimer; };
    const clearTimeout = id => timers.delete(id);
    const api = async () => { requests++; if (failed) throw Error('test failure'); };
    const viewProviders = () => { refreshed++; };
    let error;
    const status = (p, text) => { error = text; };
    const sentence = text => text;
    """ + helper + handlers + """
    (async () => {
      const p = {id: 'fake-provider'};
      const first = {textContent: 'Disconnect'}, other = {textContent: 'Disconnect'};
      await run.disconnect(p, first);
      assert.equal(requests, 0);
      assert.equal(first.textContent, 'Confirm disconnect');
      assert.equal(first.disabled, undefined);
      // Arming one card must not confirm another card.
      await run.disconnect(p, other);
      assert.equal(requests, 0);
      now = 1000;
      await run.disconnect(p, first);
      assert.equal(requests, 1);
      assert.equal(refreshed, 1);
      assert.equal(first.disabled, true);
      assert.equal(timers.size, 1); // The confirmed button's timer was cleared.
      // A late click must re-arm, even if the browser delayed the timeout callback.
      now = 4000;
      await run.disconnect(p, other);
      assert.equal(requests, 1);
      assert.equal(other.textContent, 'Confirm disconnect');
      for (const fn of timers.values()) fn();
      assert.equal(other.textContent, 'Disconnect');
      // After expiry a fresh click only asks for confirmation.
      await run.disconnect(p, other);
      assert.equal(requests, 1);
      failed = true;
      await run.disconnect(p, other);
      assert.equal(requests, 2);
      assert.equal(other.disabled, false);
      assert.equal(other.textContent, 'Disconnect');
      assert.equal(error, 'test failure');
      await run.disconnect(p, other);
      assert.equal(requests, 2); // A failed request also needs fresh confirmation.
      console.log(JSON.stringify({ok: true}));
    })().catch(err => { console.error(err); process.exitCode = 1; });
    """)
    assert got == {"ok": True}
    assert 'run[b.dataset.action](byCard(b.dataset.card), b)' in PAGE


def test_the_campaign_form_asks_for_retries_only_above_one_try_and_counts_people_not_calls():
    assert 'id="cmpTries"' in PAGE and "body.retryPlan = { maxAttempts: tries, delayMinutes: 60 }" in PAGE
    assert "const called = new Set(calls.map(([, e], n) => e.index ?? n))" in PAGE


def test_routes_of_transcribers_and_voices_are_listed_edited_and_chosen_in_the_assistant_editor():
    # Every kind is listed, told apart by kindOf.
    assert "/model-route?kind" not in PAGE and PAGE.count('api("/model-route")') == 3
    assert 'href="#routes/new-transcriber"' in PAGE and 'href="#routes/new-voice"' in PAGE
    assert 'name="tr"' in PAGE and 'name="vr"' in PAGE  # a route for the transcriber and for the voice
    assert "layerSaved(" in PAGE and "routeId: route.id, provider: first.provider" in PAGE
    # A route of transcribers or voices has no speed to measure and no latency limit.
    assert "${r.id && isModel ?" in PAGE and 'isModel && $("#rl").checked' in PAGE


def test_the_assistant_editor_picks_a_kokoro_voice_from_a_list_grouped_by_language():
    start = PAGE.index("// Kokoro's voices by language")
    code = PAGE[start:PAGE.index("const NEW_ASSISTANT", start)]
    got = run_js("""
    const esc = value => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const option = (value, label, current) =>
      `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
    """ + code + """
    console.log(JSON.stringify({
      english: voiceField("kokoro", "am_adam"),
      portuguese: voiceField("kokoro", "pf_dora"),
      other: voiceField("11labs", "custom-voice"),
      unknown: voiceField("kokoro", "custom"),
    }));
    """)
    assert '<optgroup label="American English">' in got["english"]
    assert '<option value="am_adam" selected>am_adam</option>' in got["english"]
    assert '<option value="af_heart" selected>af_heart</option>' not in got["english"]
    assert '<optgroup label="Brazilian Portuguese">' in got["portuguese"]
    assert '<option value="pf_dora" selected>pf_dora</option>' in got["portuguese"]
    assert got["other"] == '<input name="vv" aria-label="Voice ID" value="custom-voice">'
    # A voice from an import that the list does not know is kept, not silently replaced.
    assert '<optgroup label="Other"><option value="custom" selected>custom</option></optgroup>' in got["unknown"]


EDITOR_PRELUDE = """
const usd = n => n == null ? "—" : "$" + (n === 0 ? "0" : n >= 1 ? n.toFixed(2) : n.toPrecision(2));
const SUGGESTED_MODEL = { groq: "openai/gpt-oss-20b" }, SUGGESTED_VOICE = { kokoro: "af_heart" };
const ORDER_NAMES = {}, PRESET_VALUES = {}, PRESETS = [], restLabel = s => s, ms = n => n;
"""

EDITOR_CHECKS = """
priceList = { llm: {},
  stt: { deepgram: { "*": 0.0077, "nova-3:multi": 0.0092 }, soniox: { "*": 0.002 }, whisper: { "*": 0 } },
  tts: { "11labs": { "*": 40 }, kokoro: { "*": 0 }, cartesia: { "*": 39.2 }, inworld: { "inworld-tts-2": 25 } } };
const none = () => ({});
const names = (kind, order, models) => nextOrder(models, order, none, kind).map(m => m.provider);
const p = n => ({ provider: n });
const resting = m => m.provider === "kokoro" ? { restingSeconds: 60 } : {};
console.log(JSON.stringify({
  voices: names("voice", "cheapest", [p("11labs"), p("inworld"), p("kokoro"), p("cartesia")]),
  listen: names("transcriber", "cheapest", [p("deepgram"), p("made-up"), p("soniox")]),
  listed: names("voice", "listed", [p("11labs"), p("kokoro")]),
  resting: nextOrder([p("kokoro"), p("cartesia")], "cheapest", resting, "voice").map(m => m.provider),
  label: [memberLabel("voice", { provider: "11labs", voiceId: "v1", model: "x" }),
          memberLabel("transcriber", { provider: "deepgram", model: "nova-3" }),
          memberLabel("transcriber", p("soniox")), memberLabel("model", { provider: "groq", model: "m" })],
  kinds: [kindOf({}), kindOf({ kind: "voice" })],
  text: [unitText("voice", p("kokoro")), unitText("voice", p("11labs")), unitText("transcriber", p("deepgram")),
         unitText("voice", p("inworld")), unitText("transcriber", p("nope"))],
  defaults: [DEFAULT_MEMBER.transcriber("deepgram"), DEFAULT_MEMBER.voice("kokoro"), DEFAULT_MEMBER.voice("11labs")],
}));
"""


def test_the_editor_orders_a_route_of_voices_and_transcribers_by_price_and_names_their_members():
    start = PAGE.index("const kindOf")
    got = run_js(EDITOR_PRELUDE + PAGE[start:PAGE.index("async function viewRoutes")] + EDITOR_CHECKS)
    # A price that is unknown (a member priced only per model, one nobody priced) comes last, in the order listed.
    assert got["voices"] == ["kokoro", "cartesia", "11labs", "inworld"]
    assert got["listen"] == ["soniox", "deepgram", "made-up"]
    assert got["listed"] == ["11labs", "kokoro"] and got["resting"] == ["cartesia", "kokoro"]  # resting goes last
    assert got["label"] == ["11labs · v1", "deepgram · nova-3", "soniox", "groq · m"]
    assert got["kinds"] == ["model", "voice"]
    assert got["text"] == ["free", "$40.00 per 1M characters", "$0.0077 per minute", "price unknown", "price unknown"]
    assert got["defaults"] == [{"provider": "deepgram", "model": "nova-3"},
                               {"provider": "kokoro", "voiceId": "af_heart"}, {"provider": "11labs", "voiceId": ""}]



def test_a_routes_member_names_reach_the_page_only_escaped():
    """A provider, model or voice ID may come from an import or the API: it must never run as HTML."""
    uses = [line for line in PAGE.splitlines()
            if ("memberLabel(" in line or "memberName(" in line) and "const memberLabel" not in line
            and "const memberName" not in line and "const keyOf" not in line]  # keyOf is a map key, never shown
    assert len(uses) >= 3
    for line in uses:
        assert "textContent" in line or "esc(memberLabel(" in line, line.strip()[:100]


def test_the_remember_callers_box_turns_memory_on_and_off_keeping_its_other_settings():
    line = next(x for x in PAGE.splitlines() if x.strip().startswith("callerMemory: f.get("))
    got = run_js("""
    const form = ticked => ({ get: () => ticked ? "on" : null });
    const saved = (ticked, a) => { const f = form(ticked); return ({ %s }).callerMemory ?? "unset"; };
    console.log(JSON.stringify({
      never: saved(false, {}),
      on: saved(true, {}),
      onKeeps: saved(true, { callerMemory: { enabled: false, calls: 5 } }),
      off: saved(false, { callerMemory: { enabled: true, calls: 5 } }),
      offFromTrue: saved(false, { callerMemory: true }),
      onFromTrue: saved(true, { callerMemory: true }),
    }));""" % line.strip().rstrip(","))
    assert got == {"never": "unset", "on": {"enabled": True}, "onKeeps": {"enabled": True, "calls": 5},
                   "off": {"enabled": False, "calls": 5}, "offFromTrue": {"enabled": False},
                   "onFromTrue": {"enabled": True}}
    box = next(x for x in PAGE.splitlines() if 'name="memOn"' in x)
    assert "A caller ID can be faked" in box  # the warning sits next to the box


def test_the_menu_folds_to_its_icons_and_scrolls_without_a_bar():
    assert 'id="fold"' in PAGE and ".app.folded .rail nav a { font-size: 0;" in PAGE
    assert ".rail { scrollbar-width: none; }" in PAGE and ".rail::-webkit-scrollbar { display: none; }" in PAGE
    assert 'store.get("openring-menu") === "folded"' in PAGE  # the choice is remembered
    assert "a.title = a.textContent.trim()" in PAGE  # a folded link still says what it is
