"""Local operator discovery, independent of a selected provider or its secrets."""

import json
import os
import sys
from pathlib import Path

from agent_backbone.cli._common import is_agent_client
from agent_backbone.cli.presentation import print_record, print_table
from agent_backbone.config import PROFILE_REGISTRY_PATH
from agent_backbone.services.database import ProfileRegistry


def add_profiles_parser(sub) -> None:
    parser = sub.add_parser("profiles", help="operator-only local profile discovery")
    commands = parser.add_subparsers(dest="profiles_command", required=True)
    listing = commands.add_parser("list", help="list reserved profiles without starting them")
    listing.add_argument("--json", action="store_true")
    showing = commands.add_parser("show", help="show one atomic discovery record")
    showing.add_argument("profile_id")
    showing.add_argument("--json", action="store_true")
    parser.set_defaults(func=cmd_profiles)


def cmd_profiles(args) -> int:
    # No provider bootstrap: it can read a selected provider's operator secrets.
    # Local OS-user access is the operator boundary, as with other local admin
    # commands; managed agent contexts must not enumerate sibling profiles.
    if (
        os.environ.get("BACKBONE_AGENT")
        or os.environ.get("BACKBONE_CLIENT_AGENT")
        or os.environ.get("BACKBONE_CLIENT_REGISTRATION")
        or is_agent_client(os.environ.get("BACKBONE_API_KEY", ""))
    ):
        print("Profile discovery requires a local operator context.", file=sys.stderr)
        return 1
    try:
        registry = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser())
        result = (
            registry.list() if args.profiles_command == "list" else registry.show(args.profile_id)
        )
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result))
    elif args.profiles_command == "list":
        print_table(
            "Profiles",
            ("Profile ID", "Name", "Kind", "Provisioning state"),
            [(r["profile_id"], r["display_name"], r["kind"], r["state"]) for r in result["items"]],
            empty="No profiles are reserved.",
        )
    else:
        print_record(
            "Profile",
            [
                ("Profile ID", result["profile_id"]),
                ("Name", result["display_name"]),
                ("Kind", result["kind"]),
                ("Provisioning state", result["state"]),
                ("Binding", "Unavailable: profile is not ready"),
            ],
        )
    return 0
