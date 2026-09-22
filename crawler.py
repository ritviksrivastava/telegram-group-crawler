"""
Telegram group member crawler.

Lists the participants of the Telegram groups AND broadcast channels you belong
to, splits them into admins (owner + admins) and regular members/subscribers, and
exports each chat to its own CSV + JSON with their details (id, username, name,
phone if visible, role, admin title, bot/premium flags, last-seen status).

USAGE
-----
1. pip install -r requirements.txt
2. Copy .env.example to .env and fill in API_ID, API_HASH, PHONE
   (get API_ID/API_HASH from https://my.telegram.org -> API development tools).
3. Run:
       python crawler.py                       # interactive: pick from groups + channels
       python crawler.py --type channels       # only list/crawl broadcast channels
       python crawler.py --type groups         # only groups/supergroups
       python crawler.py --all                 # crawl every chat of the chosen type
       python crawler.py --type channels --all # crawl all your channels in one run
       python crawler.py --chat @somechat      # target one group/channel by @username
       python crawler.py --chat -1001234567    # or by numeric id  (--group is an alias)
       python crawler.py --list                # print your groups/channels and exit

IMPORTANT / LIMITS
------------------
* You authenticate as YOURSELF. On first run Telegram sends a login code to your
  app; enter it when prompted. A .session file is then saved so you stay logged in.
* You must belong to the chat. For supergroups/channels with more than ~200
  members, Telegram only returns the FULL participant list to admins. In a
  BROADCAST channel, only admins can list subscribers at all -- as a normal
  subscriber you'll get the admin roster but an empty members list. This is a
  Telegram-side rule, not a bug.
* `phone` is only returned when the user's privacy settings expose it (usually only
  mutual contacts). Most rows will have an empty phone. That is expected.
* Respect Telegram's Terms of Service and applicable privacy law. Use this only on
  groups you own/administer or belong to, for legitimate purposes.
"""

import argparse
import asyncio
import csv
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import ChatAdminRequiredError, FloodWaitError
from telethon.tl.types import (
    User,
    UserStatusOnline,
    UserStatusOffline,
    UserStatusRecently,
    UserStatusLastWeek,
    UserStatusLastMonth,
    Channel,
    Chat,
    # Participant roles (supergroups/channels)
    ChannelParticipantCreator,
    ChannelParticipantAdmin,
    # Participant roles (legacy small groups)
    ChatParticipantCreator,
    ChatParticipantAdmin,
)
from telethon.tl.types import ChannelParticipantsAdmins


def _status_to_text(status) -> str:
    """Human-readable last-seen status."""
    if isinstance(status, UserStatusOnline):
        return "online"
    if isinstance(status, UserStatusOffline):
        return f"offline (last {status.was_online.isoformat()})"
    if isinstance(status, UserStatusRecently):
        return "recently"
    if isinstance(status, UserStatusLastWeek):
        return "within a week"
    if isinstance(status, UserStatusLastMonth):
        return "within a month"
    return "hidden/long ago"


def _full_name(user: User) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip()


def _role_of(user: User) -> tuple[str, str]:
    """Return (role, admin_title) for a participant.

    role is "owner", "admin", or "member". admin_title is the custom rank text
    Telegram lets owners give admins (e.g. "Moderator"); empty when not set.
    Telethon attaches the participant info to `user.participant` during
    iter_participants, for both supergroups/channels and legacy small groups.
    """
    p = getattr(user, "participant", None)
    if isinstance(p, (ChannelParticipantCreator, ChatParticipantCreator)):
        return "owner", getattr(p, "rank", "") or ""
    if isinstance(p, (ChannelParticipantAdmin, ChatParticipantAdmin)):
        return "admin", getattr(p, "rank", "") or ""
    return "member", ""


def user_to_record(user: User) -> dict:
    """Flatten a Telethon User into a plain dict for export."""
    role, admin_title = _role_of(user)
    return {
        "id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "full_name": _full_name(user),
        "phone": user.phone or "",  # usually empty unless privacy allows it
        "role": role,  # owner | admin | member
        "admin_title": admin_title,  # custom rank, if any
        "is_bot": bool(user.bot),
        "is_premium": bool(getattr(user, "premium", False)),
        "is_verified": bool(user.verified),
        "is_scam": bool(getattr(user, "scam", False)),
        "is_deleted": bool(user.deleted),
        "status": _status_to_text(user.status),
    }


def _entity_kind(entity) -> str | None:
    """Classify a dialog entity.

    Returns "group" for legacy small groups and supergroups (megagroups),
    "channel" for broadcast channels, or None for anything else (users, bots).
    """
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "group"
        if getattr(entity, "broadcast", False):
            return "channel"
        # Rare gigagroups (broadcast-scale supergroups) count as groups.
        if getattr(entity, "gigagroup", False):
            return "group"
    return None


async def list_my_chats(client: TelegramClient, kinds: set[str]):
    """Return (entity, kind) for dialogs whose kind is in `kinds`.

    `kinds` is a subset of {"group", "channel"}.
    """
    chats = []
    async for dialog in client.iter_dialogs():
        kind = _entity_kind(dialog.entity)
        if kind in kinds:
            chats.append((dialog.entity, kind))
    return chats


async def print_chat_list(client: TelegramClient, kinds: set[str]):
    chats = await list_my_chats(client, kinds)
    label = " / ".join(sorted(kinds)) or "chats"
    if not chats:
        print(f"You are not a member of any {label}.")
        return None
    print(f"\n{label.capitalize()} you are a member of:")
    print("-" * 66)
    for i, (c, kind) in enumerate(chats, 1):
        username = f"@{c.username}" if getattr(c, "username", None) else "(private)"
        print(f"  [{i}] ({kind}) {c.title}  {username}  id={c.id}")
    print("-" * 66)
    return chats


async def crawl_participants(client: TelegramClient, group) -> tuple[list[dict], list[dict]]:
    """Fetch participants and split them into (admins, members).

    Two passes:
      1. A dedicated admins-only fetch. Telegram lets even non-admins read the
         admin list of a group, so this is reliable and gives correct roles/titles.
      2. A full participant fetch for everyone else. (For big supergroups the full
         list is admin-only, so members may be partial — admins still come through
         from pass 1.)
    """
    by_id: dict[int, dict] = {}

    # Pass 1: admins (owner + admins). The filter is only valid for channels/
    # supergroups; legacy small groups raise, and we rely on pass 2's role tagging.
    try:
        async for u in client.iter_participants(group, filter=ChannelParticipantsAdmins):
            if isinstance(u, User):
                by_id[u.id] = user_to_record(u)  # role comes from .participant
    except (TypeError, ValueError):
        pass  # legacy Chat: no admin filter — roles still detected in pass 2
    except ChatAdminRequiredError:
        pass
    except FloodWaitError as e:
        print(f"\n! Rate limited fetching admins. Wait {e.seconds}s and try again.")

    # Pass 2: everyone reachable. Don't overwrite admin records from pass 1.
    try:
        async for u in client.iter_participants(group, aggressive=False):
            if isinstance(u, User) and u.id not in by_id:
                by_id[u.id] = user_to_record(u)
    except ChatAdminRequiredError:
        print(
            "\n! Telegram refused the full member list: admin rights are required "
            "for this group/channel. Returning only what was reachable."
        )
    except FloodWaitError as e:
        print(f"\n! Rate limited by Telegram. Wait {e.seconds}s and try again.")

    admins = [r for r in by_id.values() if r["role"] in ("owner", "admin")]
    members = [r for r in by_id.values() if r["role"] == "member"]

    # Owner first, then admins; both groups then by name.
    admins.sort(key=lambda r: (r["role"] != "owner", r["full_name"].lower()))
    members.sort(key=lambda r: r["full_name"].lower())
    return admins, members


FIELDNAMES = [
    "id", "username", "first_name", "last_name", "full_name", "phone",
    "role", "admin_title", "is_bot", "is_premium", "is_verified",
    "is_scam", "is_deleted", "status",
]


def export(
    records: list[dict], group_title: str, out_dir: str, label: str
) -> tuple[str, str]:
    """Write `records` to CSV + JSON. `label` (e.g. 'admins'/'members') is part
    of the filename and the JSON payload key."""
    os.makedirs(out_dir, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in group_title)[:50]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(out_dir, f"{safe}_{label}_{stamp}")

    csv_path = base + ".csv"
    json_path = base + ".json"

    fieldnames = list(records[0].keys()) if records else FIELDNAMES
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "group": group_title,
                "category": label,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "count": len(records),
                label: records,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    return csv_path, json_path


async def resolve_chat(client: TelegramClient, chat_arg: str):
    """Turn a --chat argument (username or numeric id) into an entity."""
    try:
        if chat_arg.lstrip("-").isdigit():
            return await client.get_entity(int(chat_arg))
        return await client.get_entity(chat_arg)
    except Exception as e:
        print(f"Could not resolve chat '{chat_arg}': {e}")
        return None


async def process_chat(client: TelegramClient, entity, out_dir: str) -> None:
    """Crawl one group/channel, print a summary, and export admins + members."""
    kind = _entity_kind(entity) or "group"
    title = getattr(entity, "title", str(entity))
    # In a broadcast channel the non-admin participants are "subscribers".
    member_label = "subscribers" if kind == "channel" else "members"

    print(f"\nCrawling {kind}: {title} ...")
    admins, members = await crawl_participants(client, entity)
    print(f"Collected {len(admins)} admins and {len(members)} {member_label}.")

    if admins:
        print("Admins:")
        for r in admins:
            uname = f"@{r['username']}" if r["username"] else "(no username)"
            tag = f" [{r['admin_title']}]" if r["admin_title"] else ""
            print(f"  - {r['role']}: {r['full_name']} {uname}{tag} id={r['id']}")

    saved = []
    if admins:
        saved.append(export(admins, title, out_dir, "admins"))
    if members:
        saved.append(export(members, title, out_dir, member_label))

    if saved:
        print("Saved:")
        for csv_path, json_path in saved:
            print(f"  {csv_path}\n  {json_path}")
    else:
        print("No participants collected.")


async def main():
    parser = argparse.ArgumentParser(
        description="Telegram group/channel member crawler"
    )
    parser.add_argument(
        "--chat", "--group",
        dest="chat",
        help="@username or numeric id of a specific group/channel to crawl",
    )
    parser.add_argument(
        "--type",
        choices=["all", "groups", "channels"],
        default="all",
        help="Which chat kinds to list/crawl (default: all)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Crawl every chat of the selected --type (no interactive prompt)",
    )
    parser.add_argument(
        "--list", action="store_true", help="List your groups/channels and exit"
    )
    parser.add_argument("--out", default="output", help="Output directory")
    args = parser.parse_args()

    kinds = {"group", "channel"}
    if args.type == "groups":
        kinds = {"group"}
    elif args.type == "channels":
        kinds = {"channel"}

    load_dotenv()
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    phone = os.getenv("PHONE")

    if not api_id or not api_hash:
        print("Missing API_ID / API_HASH. Copy .env.example to .env and fill it in.")
        return

    client = TelegramClient("session", int(api_id), api_hash)
    await client.start(phone=phone)  # prompts for login code on first run
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username}) id={me.id}")

    try:
        if args.list:
            await print_chat_list(client, kinds)
            return

        # 1. Explicit target via --chat / --group.
        if args.chat:
            entity = await resolve_chat(client, args.chat)
            if entity is not None:
                await process_chat(client, entity, args.out)
            return

        # 2. Crawl everything of the selected type.
        if args.all:
            chats = await list_my_chats(client, kinds)
            if not chats:
                print("Nothing to crawl.")
                return
            print(f"Crawling {len(chats)} chat(s)...")
            for entity, _kind in chats:
                await process_chat(client, entity, args.out)
            return

        # 3. Interactive picker.
        chats = await print_chat_list(client, kinds)
        if not chats:
            return
        choice = input("\nEnter the number to crawl (or 'a' for all): ").strip()
        if choice.lower() == "a":
            for entity, _kind in chats:
                await process_chat(client, entity, args.out)
        elif choice.isdigit() and 1 <= int(choice) <= len(chats):
            await process_chat(client, chats[int(choice) - 1][0], args.out)
        else:
            print("Invalid choice.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
