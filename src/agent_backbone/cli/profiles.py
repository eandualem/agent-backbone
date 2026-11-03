"""Local profile discovery and lifecycle receipts, independent of provider APIs."""

import json
import os
import sys
from pathlib import Path

from agent_backbone.cli._common import is_agent_client
from agent_backbone.cli.presentation import print_record, print_table
from agent_backbone.config import PROFILE_REGISTRY_PATH
from agent_backbone.services.database import (
    ProfileOperations,
    ProfileRegistry,
    validate_profile_operation,
)


def add_profiles_parser(sub) -> None:
    parser = sub.add_parser("profiles", help="operator-only local profile management")
    commands = parser.add_subparsers(dest="profiles_command", required=True)
    listing = commands.add_parser("list", help="list reserved profiles without starting them")
    listing.add_argument("--json", action="store_true")
    showing = commands.add_parser("show", help="show one atomic discovery record")
    showing.add_argument("profile_id")
    showing.add_argument("--json", action="store_true")
    apply = commands.add_parser("apply", help="apply a durable local lifecycle request")
    apply.add_argument("--file", required=True)
    apply.add_argument("--json", action="store_true")
    operation = commands.add_parser("operation", help="read the original lifecycle receipt")
    operation.add_argument("request_id")
    operation.add_argument("--json", action="store_true")
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
        print("Profile commands require a local operator context.", file=sys.stderr)
        return 1
    try:
        if args.profiles_command in {"apply", "operation"}:
            return _lifecycle(args)
        registry = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser())
        result = (
            registry.list() if args.profiles_command == "list" else registry.show(args.profile_id)
        )
    except (ValueError, OSError, RecursionError) as exc:
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


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Profile operation request has duplicate fields")
        value[key] = item
    return value


def _lifecycle(args) -> int:
    path = Path(PROFILE_REGISTRY_PATH).expanduser()
    if args.profiles_command == "apply":
        # Bound input before initialization; never accept arbitrary environment,
        # credential or provider path authority from the requesting client.
        with Path(args.file).open("rb") as source:
            body = source.read(65537)
        if len(body) > 65536:
            raise ValueError("Profile operation request exceeds 65536 bytes")
        request = validate_profile_operation(json.loads(body, object_pairs_hook=_unique_object))
        operations = ProfileOperations(ProfileRegistry.create(path))
        result = operations.apply(request)
    else:
        operations = ProfileOperations(ProfileRegistry(path))
        result = operations.lookup(args.request_id)
    if args.json:
        print(json.dumps(result))
    else:
        print_record(
            "Profile operation",
            [
                ("Request ID", result["request_id"]),
                ("Action", result.get("action")),
                ("Profile ID", result.get("profile_id")),
                ("Installation ID", result.get("installation_id")),
                ("Lifecycle ID", result.get("lifecycle_id")),
                ("Status", result.get("status", "unavailable")),
                ("Error", result["error"]["code"] if result.get("error") else None),
                ("Detail", result["error"]["message"] if result.get("error") else None),
            ],
        )
    return 1 if result.get("error") else 0
