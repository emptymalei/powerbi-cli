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

## Which accounts you need

You need **one account, of either kind**: an administrator's token is not required. What
pbi_cli can do depends on which ones you have stored:

| You store | What pbi_cli can do |
| --- | --- |
| an administrator's token (`-g admin`) | the lists of the whole tenant, scans, who has access, data sources, audit events: everything in [`pbi sync`](sync.md) except what only a user can read |
| a user's token (`-g user`), a service account for example | what that account can see: its workspaces and apps, and the reports, datasets, dashboards, dataflows and users of those workspaces, and the pages of the reports. `pbi sync` with no names does the first part, and the [terminal UI](tui.md) shows it like the tenant's lists |
| both | the tenant's lists from the administrator, and the workspaces that only the user account can open from the user account. The [Explorer](tui.md) merges them (see below) |
| none | nothing is fetched. A lake that someone else made can still be opened, view only and without any account, see [Sharing a lake](sharing.md) |

A user account sees only the workspaces it is a member of, and there is no list of the
whole tenant for it, so it cannot scan and cannot list the users of a report (the API gives
those only to administrators). Naming such a target without the account fails before
anything is fetched, and tells you what to store:

```text
$ pbi sync run scan
Error: 'scan' needs an administrator account, and none is stored. Store one with
`pbi auth -t <token> -g admin`. The accounts you have are: user.
```

## Several accounts

Store as many profiles as you like in each group. A sync uses the **active** profile of
each group, and `--admin-profile` and `--user-profile` use another one for one run:

```bash
pbi auth -t <token> -p svc-finance -g user
pbi auth -t <token> -p svc-sales   -g user

pbi profile list
pbi profile switch svc-sales -g user                    # for every command from now on
pbi sync run user-groups --user-profile svc-finance     # for this run only
```

What a user account sees is its own, so the lake keeps the lists *of an account* (its
workspaces and its apps) apart for every account. The key is the object id that is in the
token (`oid`, or `appid` for a service principal), not the profile name: two profiles of
one person share their lists, a renamed profile keeps its history, and two accounts never
overwrite each other's. Everything that is the same for everybody who can open a workspace
(its reports, datasets, dashboards, dataflows and users) is kept once.

In the [terminal UI](tui.md) the header shows every account you have, with how long each
token lasts, and `p` lists the stored profiles of both groups, makes one of them active and
stores a new token for it.

When both kinds of account are stored, the Explorer lists a workspace from the
administrator's lists when it has them, and from the user's own lists otherwise; for an item
the administrator's list wins. The Info tab of a workspace says which accounts see it.

## Where tokens are kept

In your system keyring (Keychain, Credential Manager, Secret Service). When no keyring is
available they go to `~/.pbi_cli/credentials.json`, readable by you only. The profile names
and which profile is active are in `~/.pbi_cli/config.yaml`.

## What pbi_cli reads from a token

A token is a signed JWT. pbi_cli reads a few claims from it, on your machine, without
checking the signature and without sending it anywhere:

- the tenant id (`tid`), which separates the data of different tenants in the
  [data lake](lake.md);
- the expiry (`exp`), so it can tell you when the token has run out instead of letting a
  request fail;
- who the token is for: the object id (`oid`, else `appid`), which tells accounts apart in
  the lake, and a name to show (`upn`, else the first of `unique_name`,
  `preferred_username`, `name`, `email`), which the [terminal UI](tui.md) lists next to
  each profile.

A token that is not a readable JWT is still accepted: the API decides. Its account is then
told apart by the name of its profile.

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

The administrator's account is optional (see [Which accounts you need](#which-accounts-you-need)).
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
