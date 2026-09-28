# OpenJEV Support

This fork adds **optional** [OpenJEV](https://openjev.sh) support alongside the original TypeSafe integration. OpenJEV is a free community gateway to the same Jev model. TypeSafe remains the default — anyone with a TypeSafe key sees zero behaviour change.

## What was added

| File | Change |
|---|---|
| `hallmonitor/jev.py` | Added `_provider()` selection, `_OpenJEVClient` (stdlib HTTP, same `.system_one()` interface as `TypeSafeClient`), and `OPENJEV_ENDPOINT`/`OPENJEV_MODEL` constants. Modified `_get()` to choose the provider. Added `OPENJEV_API_KEY` to `load_key_from_user_env` defaults. |
| `hallmonitor/bob.py` | Added `OPENJEV_API_KEY` to the key-redaction list in error messages. |
| `scripts/install.py` | Added `OPENJEV_API_KEY` reference to the MCP server env in `.bob/mcp.json`. |
| `.env.example` | Documented `OPENJEV_API_KEY` and `JEV_PROVIDER`. |
| `README.md` | Added OpenJEV note after the project intro. |

No TypeSafe code was removed, renamed, or re-defaulted.

## Provider selection rule

Implemented in `hallmonitor/jev.py` `_provider()`:

1. **Explicit choice wins:** `JEV_PROVIDER=openjev` (or `JEV_PROVIDER=typesafe`).
2. **Otherwise, if `TYPESAFE_API_KEY` is set → TypeSafe** (default, unchanged).
3. **Otherwise, if only `OPENJEV_API_KEY` is set → OpenJEV.**

## How to configure

Set `OPENJEV_API_KEY` in your environment (get one at https://openjev.sh/dashboard):

```bash
export OPENJEV_API_KEY=your_openjev_key
```

If `TYPESAFE_API_KEY` is also set, TypeSafe is used by default. To force OpenJEV:

```bash
export JEV_PROVIDER=openjev
```

The OpenJEV client uses the same request/response contract as TypeSafe (`POST /v1/systemone`, model `openjev`) and adds retry on HTTP 429/503/529.

## How it was verified

A live `POST` to `https://api.openjev.sh/v1/systemone` with model `openjev`, state `ping`, and one `noul` question returned HTTP 200 with a valid answer. The repo's own tests were not executed (per safety policy). A `grep` confirmed no hardcoded `api.typesafe.ai` default was introduced — TypeSafe's endpoint remains unchanged.

## Upstream

Original project: https://github.com/monickverma/hall-monitor by @monickverma
