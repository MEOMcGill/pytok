"""Click CLI for managing the PyTok accounts pool.

    python -m pytok.accounts.cli add --username you@example.com --password ...
    python -m pytok.accounts.cli list -v
    python -m pytok.accounts.cli login --username you@example.com   # open browser, log in

Uses ~/.pytok/accounts.db by default (override with --db or $PYTOK_HOME).
"""

import asyncio
import os

import click
from tabulate import tabulate

from ._utils import default_db_path
from .pool import AccountsPool


def run_async(coro):
    return asyncio.run(coro)


@click.group()
@click.option("--db", default=None, help="Path to accounts database (default ~/.pytok/accounts.db)")
@click.pass_context
def cli(ctx, db):
    """PyTok — TikTok accounts pool CLI."""
    ctx.ensure_object(dict)
    ctx.obj["db"] = db or default_db_path()


@cli.command()
@click.option("--username", required=True, help="Login identifier (email / phone / username)")
@click.option("--password", default=None, help="Account password")
@click.option("--email", default=None, help="Optional email on file")
@click.option("--email-password", default=None, help="Email password")
@click.option("--phone", default=None, help="Optional phone on file")
@click.option("--cookies", default=None, help="Cookies JSON string, header string, or file path")
@click.option("--profile-dir", default=None, help="Chrome profile dir (defaults to ~/.pytok/profiles/<user>)")
@click.pass_context
def add(ctx, username, password, email, email_password, phone, cookies, profile_dir):
    """Add a new account."""

    async def _add():
        pool = AccountsPool(ctx.obj["db"])
        cookie_data = cookies
        if cookies and os.path.exists(cookies):
            with open(cookies) as f:
                cookie_data = f.read()
        await pool.add_account(
            username=username,
            password=password,
            email=email,
            email_password=email_password,
            phone_number=phone,
            cookies=cookie_data,
            profile_dir=profile_dir,
        )
        acct = await pool.get(username)
        click.echo(f"Added account: {username}")
        click.echo(f"  profile_dir: {acct.profile_dir}")
        click.echo(f"  active:      {acct.active} (a session with cookies is active)")

    run_async(_add())


@cli.command()
@click.argument("username", nargs=-1)
@click.option("--all", "delete_all", is_flag=True, help="Delete all accounts")
@click.pass_context
def delete(ctx, username, delete_all):
    """Delete account(s) by login identifier."""
    if not username and not delete_all:
        raise click.UsageError("Provide username(s) or --all")

    async def _delete():
        pool = AccountsPool(ctx.obj["db"])
        if delete_all:
            accounts = await pool.get(None)
            if not accounts:
                click.echo("No accounts")
                return
            if not click.confirm(f"Delete ALL {len(accounts)} accounts? (Chrome profiles are left on disk)"):
                return
            await pool.delete_account([a.username for a in accounts])
            click.echo(f"Deleted {len(accounts)}")
        else:
            await pool.delete_account(list(username))
            click.echo(f"Deleted {len(username)}")

    run_async(_delete())


@cli.command(name="list")
@click.option("--active", is_flag=True, help="Active accounts only")
@click.option("--inactive", is_flag=True, help="Inactive accounts only")
@click.option("--verbose", "-v", is_flag=True, help="Verbose columns")
@click.pass_context
def list_accounts(ctx, active, inactive, verbose):
    """List accounts."""

    async def _list():
        pool = AccountsPool(ctx.obj["db"])
        if active:
            accounts = await pool.get_active_accounts()
        elif inactive:
            accounts = await pool.get_inactive_accounts()
        else:
            accounts = await pool.get(None)
        if not accounts:
            click.echo("No accounts")
            return

        if verbose:
            headers = ["Username", "Handle", "UID", "Active", "In Use", "Last Used", "Cookies", "Error"]
            rows = [
                [
                    a.username,
                    a.unique_id or "-",
                    a.user_id or "-",
                    "Y" if a.active else "N",
                    "Y" if a.in_use else "N",
                    str(a.last_used)[:19] if a.last_used else "-",
                    len(a.cookies),
                    (a.error_msg[:30] + "...") if a.error_msg and len(a.error_msg) > 30 else (a.error_msg or "-"),
                ]
                for a in accounts
            ]
        else:
            headers = ["Username", "Handle", "Active", "In Use", "Last Used"]
            rows = [
                [
                    a.username,
                    a.unique_id or "-",
                    "Y" if a.active else "N",
                    "Y" if a.in_use else "N",
                    str(a.last_used)[:19] if a.last_used else "-",
                ]
                for a in accounts
            ]
        click.echo(tabulate(rows, headers=headers, tablefmt="simple"))
        click.echo(f"\nTotal: {len(accounts)} accounts")

    run_async(_list())


@cli.command()
@click.argument("username")
@click.pass_context
def info(ctx, username):
    """Show account details."""

    async def _info():
        pool = AccountsPool(ctx.obj["db"])
        try:
            a = await pool.get(username)
        except ValueError:
            click.echo(f"Account not found: {username}")
            return
        click.echo(f"Account: {a.username}")
        click.echo(f"  Handle (unique_id): {a.unique_id or '-'}")
        click.echo(f"  User ID (uid):      {a.user_id or '-'}")
        click.echo(f"  sec_uid:            {a.sec_uid or '-'}")
        click.echo(f"  Email:              {a.email or '-'}")
        click.echo(f"  Phone:              {a.phone_number or '-'}")
        click.echo(f"  Profile dir:        {a.profile_dir}")
        click.echo(f"  Active:             {a.active}")
        click.echo(f"  In Use:             {a.in_use}")
        click.echo(f"  Last Used:          {a.last_used or '-'}")
        click.echo(f"  Cookies backed up:  {len(a.cookies)}")
        click.echo(f"  Locks:              {a.locks or '-'}")
        click.echo(f"  Error:              {a.error_msg or '-'}")

    run_async(_info())


@cli.command()
@click.pass_context
def stats(ctx):
    """Pool statistics."""

    async def _stats():
        pool = AccountsPool(ctx.obj["db"])
        s = await pool.stats()
        if not s:
            click.echo("No stats")
            return
        click.echo("Account Pool Statistics")
        click.echo("-" * 30)
        for k in ("total", "active", "inactive", "in_use", "locked"):
            click.echo(f"  {k.capitalize():<10} {s.get(k, 0)}")

    run_async(_stats())


@cli.command()
@click.argument("username", nargs=-1)
@click.option("--all", "set_all", is_flag=True)
@click.pass_context
def activate(ctx, username, set_all):
    """Mark account(s) active."""
    if not username and not set_all:
        raise click.UsageError("Provide username(s) or --all")

    async def _a():
        pool = AccountsPool(ctx.obj["db"])
        await pool.set_active(None if set_all else list(username), True)
        click.echo("Done")

    run_async(_a())


@cli.command()
@click.argument("username", nargs=-1)
@click.option("--all", "set_all", is_flag=True)
@click.pass_context
def deactivate(ctx, username, set_all):
    """Mark account(s) inactive."""
    if not username and not set_all:
        raise click.UsageError("Provide username(s) or --all")

    async def _d():
        pool = AccountsPool(ctx.obj["db"])
        await pool.set_active(None if set_all else list(username), False)
        click.echo("Done")

    run_async(_d())


@cli.command()
@click.argument("username", nargs=-1)
@click.pass_context
def unlock(ctx, username):
    """Clear locks on account(s) (all if none given)."""

    async def _u():
        pool = AccountsPool(ctx.obj["db"])
        await pool.unlock(list(username) if username else None)
        click.echo("Done")

    run_async(_u())


@cli.command()
@click.argument("username", nargs=-1)
@click.pass_context
def release(ctx, username):
    """Mark account(s) not-in-use (recover from a crashed session)."""

    async def _r():
        pool = AccountsPool(ctx.obj["db"])
        await pool.release_account(list(username) if username else None)
        click.echo("Done")

    run_async(_r())


@cli.command()
@click.option("--username", required=True, help="Login identifier to log in")
@click.option("--headless", is_flag=True, help="Run headless (not recommended for login)")
@click.option("--force-relogin", is_flag=True,
              help="Clear the profile's session and force a fresh login (recover a stale account)")
@click.pass_context
def login(ctx, username, headless, force_relogin):
    """Open a browser for this account, log in, and capture identity + cookies.

    Use this once per new account: it launches the account's persistent Chrome
    profile, runs the login/verification flow, then stores the resolved TikTok
    identity and a cookie backup so future sessions start already logged in.

    Pass --force-relogin to recover an account whose cookies look valid but whose
    session TikTok has invalidated (app-context shows no logged-in user).
    """
    from ..tiktok import PyTok

    async def _login():
        pool = AccountsPool(ctx.obj["db"])
        account = await pool.get_account(username)
        if account is None:
            click.echo(f"Account {username} not found or already in use")
            return
        try:
            async with PyTok(account=account, accounts_pool=pool, headless=headless,
                             force_relogin=force_relogin) as api:
                ident = await api._get_logged_in_identity()
                if ident:
                    click.echo(f"Logged in as @{ident.get('unique_id')} (uid {ident.get('user_id')})")
                else:
                    click.echo("Warning: session verified but identity could not be read")
        finally:
            await pool.release_account(username)
        a = await pool.get(username)
        click.echo(f"Stored: handle={a.unique_id} uid={a.user_id} cookies={len(a.cookies)} active={a.active}")

    run_async(_login())


if __name__ == "__main__":
    cli()
