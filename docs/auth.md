# Authentication

pbi_cli calls the Power BI REST API with a bearer token that you provide. It does not sign
you in itself: you get a token the way you already do and store it with `pbi auth`.

## Sign in

Get a token from wherever you sign in to Power BI. For example, with the Power BI
PowerShell module, which opens the usual sign-in window:

```powershell
Connect-PowerBIServiceAccount
Get-PowerBIAccessToken -AsString
```

The second command prints `Bearer <token>`; pass only the token (`pbi auth` removes a
leading `Bearer` and warns). Then store it:

```bash
pbi auth --bearer-token <your_bearer_token>
```

## Profiles and groups

A token is stored under a *profile* name, and a profile belongs to a *group*:

- `user`: what the signed-in user can see (their workspaces, reports, apps);
- `admin`: a Fabric administrator's token, needed by the commands marked "Requires Admin".

```bash
pbi auth -t <token> -p user-nlm  -g user
pbi auth -t <token> -p admin-nlm -g admin

pbi profile list
pbi profile switch admin-nlm -g admin
```

Commands pick the active profile of the group they need. Without `-g`, a profile is stored
in the older, ungrouped list, which is still used as a fallback.

## Where tokens are kept

In your system keyring (Keychain, Credential Manager, Secret Service). When no keyring is
available they go to `~/.pbi_cli/credentials.json`, readable by you only. The profile names
and which profile is active are in `~/.pbi_cli/config.yaml`.

## What pbi_cli reads from a token

A token is a signed JWT. pbi_cli reads two claims from it, on your machine, without checking
the signature and without sending it anywhere:

- the tenant id (`tid`), which separates the data of different tenants in the
  [data lake](lake.md);
- the expiry (`exp`), so it can tell you when the token has run out instead of letting a
  request fail.

A token that is not a readable JWT is still accepted: the API decides.

## When the token expires

Tokens last a limited time, typically about an hour. When it has run out pbi_cli says so
and does not send the request:

```text
Error: The token for profile 'admin-nlm' expired at 2026-09-30 13:00 UTC. Sign in again
and store a fresh token with `pbi auth -t <token> -p admin-nlm -g admin`.
```

The same message appears when the API rejects the token (`401 Unauthorized`). Sign in
again and run `pbi auth` with the new token; the next command picks it up. In the
[terminal UI](tui.md) a dialog asks for the new token (press `a`, or wait for it to ask),
stores it the same way, and continues the sync that was interrupted. A long job such
as [`pbi sync run`](sync.md) stops when the token expires, keeps what it has done, and
continues from there when you run it again. An answer that is
still fresh in the [data lake](lake.md) is served even with an expired token, because
nothing has to be sent.

## Admin access

`403 Forbidden` from an admin operation means the token does not belong to a Fabric
administrator (or a service principal allowed to use the admin APIs). Store an
administrator's token in the `admin` group:

```bash
pbi auth -t <admin token> -p admin-nlm -g admin
```

## Keeping the token safe

- pbi_cli never writes a token to the data lake, to logs or to error messages, and the
  credentials object hides it from tracebacks.
- Do not paste tokens into scripts or shell history that are shared; `pbi auth` stores
  them in the keyring so they do not have to appear again.
