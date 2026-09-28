"""Thin Jev wrapper: one request per decision point, retries, token accounting.

The model is pinned: `jev-latest` moves, and every threshold here was tuned against one model.
Jev bills input tokens only (output is free, docs.typesafe.ai/models), so `tokens()` counts input tokens.
"""
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from typesafe_sdk import TypeSafeAuthenticationError, TypeSafeClient, TypeSafeError, TypeSafePermissionDeniedError

CALIBRATED_MODEL = "jev-1.13.0"  # the harm weights, thresholds, control set and eval/ results were tuned on this
# HM_JEV_MODEL exists for re-calibrating a new model (run eval/control_set.py and eval/seeded.py with it).
# Any other model is marked as uncalibrated on every Hall Pass until its calibration is in place.
MODEL = os.environ.get("HM_JEV_MODEL", CALIBRATED_MODEL)
UNCALIBRATED = MODEL != CALIBRATED_MODEL
PRICE_PER_M_INPUT = 0.042  # $ per 1M input tokens; output tokens are free (docs.typesafe.ai/models)

# --- OpenJEV (optional community gateway to the same Jev model) ---
# OpenJEV is an additive option; TypeSafe stays the default. See OPENJEV.md.
OPENJEV_ENDPOINT = "https://api.openjev.sh/v1/systemone"
OPENJEV_MODEL = "openjev"

_client = None


class JevRefused(Exception):
    """Jev can't answer: HTTP 403 (a content block), a missing, wrong or expired key (401), or an outage that
    outlasted the retries. Callers fall back to code-only rules."""


def _provider():
    """Which Jev provider to use: 'openjev' or 'typesafe'.

    1. JEV_PROVIDER=openjev (or =typesafe) wins explicitly.
    2. Otherwise TypeSafe if TYPESAFE_API_KEY is set (default, unchanged).
    3. Otherwise OpenJEV if OPENJEV_API_KEY is set.
    Anyone with a TypeSafe key sees zero behaviour change.
    """
    explicit = os.environ.get("JEV_PROVIDER", "").strip().lower()
    if explicit == "openjev":
        return "openjev"
    if explicit == "typesafe":
        return "typesafe"
    if os.environ.get("TYPESAFE_API_KEY"):
        return "typesafe"
    if os.environ.get("OPENJEV_API_KEY"):
        return "openjev"
    return "typesafe"  # original default


class _OpenJEVResponse:
    """Mimics the TypeSafe SDK's pydantic model: .model_dump() returns a plain dict."""

    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


class _OpenJEVClient:
    """Minimal HTTP client for the OpenJEV gateway — same .system_one() interface as TypeSafeClient.

    Uses only the standard library so it does not add a dependency. Retries 429/503/529 with
    backoff (matching the ask() loop's transient-error handling); 401/403 are surfaced as
    TypeSafeError so ask() treats them as key/content problems without retrying.
    """

    def __init__(self, model=OPENJEV_MODEL):
        self.model = model
        key = os.environ.get("OPENJEV_API_KEY")
        if not key:
            raise TypeSafeError("OpenJEV selected but OPENJEV_API_KEY is not set.")
        self._key = key

    def system_one(self, *, state, questions):
        body = json.dumps(
            {"model": self.model, "state": state, "questions": questions},
            default=lambda o: o.model_dump() if hasattr(o, "model_dump") else o.__dict__,
        ).encode()
        for attempt in range(3):
            req = urllib.request.Request(
                OPENJEV_ENDPOINT,
                data=body,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                    data.setdefault("model", self.model)
                    if "usage" in data:
                        data["usage"].setdefault("input_tokens", 0)
                    return _OpenJEVResponse(data)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code in (429, 503, 529) and attempt < 2:
                    time.sleep(1.5 * 2 ** attempt)
                    continue
                raise TypeSafeError(f"OpenJEV error ({e.code}): {detail}") from e
            except (urllib.error.URLError, OSError) as e:
                if attempt < 2:
                    time.sleep(1.5 * 2 ** attempt)
                    continue
                raise TypeSafeError(f"OpenJEV unreachable: {e}") from e


def _get():
    global _client
    if _client is None:
        if _provider() == "openjev":
            _client = _OpenJEVClient()
        else:
            _client = TypeSafeClient(model=MODEL)
    return _client


def load_key_from_user_env(names=("TYPESAFE_API_KEY", "BOB_API_KEY", "OPENJEV_API_KEY")):
    """Bob starts the MCP server without the user's environment: in the probe (Sept 27), TYPESAFE_API_KEY
    set in the shell that ran `bob run` never reached it. On Windows, read the keys from where `setx`
    saved them, so they still never go in a file. BOB_API_KEY is for the Receipts auditor's own `bob run`
    (bob.shell_audit): without it, every audit failed with "Bob API key is required" (Sept 27).
    OPENJEV_API_KEY is the optional community-gateway key (see OPENJEV.md).
    Called by the entry scripts Bob launches, not by tests."""
    for name in names:
        if os.environ.get(name, "").startswith("${"):  # Bob leaves ${env:NAME} as is when NAME is unset
            del os.environ[name]
    if os.name != "nt":
        return
    import winreg
    for name in [n for n in names if not os.environ.get(n)]:
        for hive, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    key = winreg.QueryValueEx(k, name)[0]
            except OSError:
                continue
            if key:
                os.environ[name] = key
                break


def ask(state, questions, retries=3):
    """Ask every question over one state in a single parallel request.

    Returns (answers, usage) as plain dicts: answers[id] has `noul`, or `choice`/`probabilities`/
    `confidence`, or `score`/`probabilities`/`confidence`. usage has the token counts and the model id.
    """
    for attempt in range(retries):
        try:
            r = _get().system_one(state=state, questions=questions).model_dump()
            return r["answers"], {**r["usage"], "model": r.get("model") or MODEL}
        except TypeSafePermissionDeniedError as e:
            raise JevRefused(str(e)) from e  # retrying a content block only repeats it
        except (TypeSafeAuthenticationError, TypeSafeError) as e:
            if type(e) in (TypeSafeAuthenticationError, TypeSafeError):  # a missing, wrong or expired key
                # Before, this was retried and then raised as-is: the hooks' catch-all let every edit through
                # unchecked (fail_open), so a key problem quietly switched supervision off.
                raise JevRefused(f"Jev can't be reached with this key: {e}") from e
            if attempt == retries - 1:
                raise JevRefused(f"Jev is unavailable: {e}") from e
            time.sleep(1.5 * 2 ** attempt)
        except Exception as e:
            if attempt == retries - 1:  # an outage: fall back to code-only rules, like a refusal
                raise JevRefused(f"Jev is unavailable: {e}") from e
            time.sleep(1.5 * 2 ** attempt)


def ask_many(jobs, workers=8, return_refusals=False):
    """Run independent (state, questions) requests concurrently, preserving order. With
    return_refusals, a refused request yields its JevRefused in place of a result, so one refusal
    doesn't sink the others."""
    def one(job):
        try:
            return ask(*job)
        except JevRefused as e:
            if return_refusals:
                return e
            raise
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, jobs))


def p_levels(answer, levels):
    """Probability mass a Score answer puts on the given levels."""
    probs = {int(k): v for k, v in answer["probabilities"].items()}
    return sum(probs.get(l, 0.0) for l in levels)


def tokens(usage):
    """Billable tokens: Jev bills input tokens only."""
    return usage["input_tokens"]


def cost(n_tokens):
    return n_tokens * PRICE_PER_M_INPUT / 1e6
