"""Local profile discovery and lifecycle receipts, independent of provider APIs."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import httpx

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
    listing.add_argument("--json", dest="json_output", action="store_true")
    showing = commands.add_parser("show", help="show one atomic discovery record")
    showing.add_argument("profile_id")
    showing.add_argument("--json", dest="json_output", action="store_true")
    apply = commands.add_parser("apply", help="apply a durable local lifecycle request")
    apply.add_argument("--file", required=True)
    apply.add_argument("--json", dest="json_output", action="store_true")
    operation = commands.add_parser("operation", help="read the original lifecycle receipt")
    operation.add_argument("request_id")
    operation.add_argument("--json", dest="json_output", action="store_true")
    signing = commands.add_parser("sign-in", help="sign a CLI account in inside a running profile")
    signing.add_argument("profile_id")
    signing.add_argument("--runtime", required=True, choices=("claude", "codex", "opencode"))
    # Internal: the local control plane launches this for a ready profile.
    serve = commands.add_parser("serve")
    serve.add_argument("--registry", required=True, type=Path)
    serve.add_argument("profile_id")
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
    if args.profiles_command == "serve":
        from agent_backbone.cli.server import serve_profile

        serve_profile(args.registry, args.profile_id)
        return 0
    path = Path(PROFILE_REGISTRY_PATH).expanduser()
    if args.profiles_command in {"list", "show", "operation"} and args.json_output:
        # The first-run answer an app branches on, separate from a broken registry.
        code = None if path.exists() else "registry_absent"
        try:
            if code is None:
                ProfileRegistry(path).list()
        except (ValueError, OSError, RecursionError):
            code = "registry_unavailable"
        if code:
            message = "No profiles exist yet." if code == "registry_absent" else "Registry error."
            print(json.dumps({"schema_version": 1, "error": {"code": code, "message": message}}))
            return 1
    try:
        if args.profiles_command == "sign-in":
            return _sign_in(args)
        if args.profiles_command in {"apply", "operation"}:
            return _lifecycle(args)
        registry = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser())
        result = (
            registry.list() if args.profiles_command == "list" else registry.show(args.profile_id)
        )
    except (ValueError, OSError, RecursionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(result))
    elif args.profiles_command == "list":
        print_table(
            "Profiles",
            ("Profile ID", "Name", "Kind", "Provisioning state"),
            [(r["profile_id"], r["display_name"], r["kind"], r["state"]) for r in result["items"]],
            empty="No profiles are reserved.",
        )
    else:
        binding = result["binding"]
        connection = (
            [
                ("Endpoint", binding["endpoint"]["path"]),
                ("Credential file", binding["credential_reference"]["path"]),
                ("Tmux socket", binding["tmux"]["socket_path"]),
            ]
            if binding
            else [("Binding", "Unavailable: profile is not ready")]
        )
        print_record(
            "Profile",
            [
                ("Profile ID", result["profile_id"]),
                ("Name", result["display_name"]),
                ("Kind", result["kind"]),
                ("Provisioning state", result["state"]),
                ("Git identity", result["git_identity"]),
                *connection,
            ],
        )
    return 0


def _sign_in(args) -> int:
    """Open the CLI's own sign-in on the profile's server; credentials stay in that CLI."""
    from agent_backbone.cli._profile_client import Refused, resolve_binding

    record = ProfileRegistry(Path(PROFILE_REGISTRY_PATH).expanduser()).show(args.profile_id)
    if not record["binding"]:
        raise ValueError("Profile is not ready")
    try:
        binding = resolve_binding(
            (
                args.profile_id,
                record["binding"]["installation_id"],
                record["binding"]["lifecycle_id"],
            )
        )
        asyncio.run(binding.handshake())
    except Refused as exc:
        raise ValueError(f"The profile is not reachable: {exc.code}. Start it first.") from None
    transport = httpx.HTTPTransport(uds=binding.endpoint, retries=0)
    with httpx.Client(transport=transport, trust_env=False, timeout=30) as client:
        response = client.post(
            "http://bound-provider/api/profile/accounts/sign-in",
            headers=binding.headers(),
            json={"schema_version": 1, "runtime": args.runtime},
        )
    if response.status_code != 200:
        try:
            reason = response.json()["detail"]["message"]
        except (ValueError, KeyError, TypeError):
            reason = str(response.status_code)
        raise ValueError(f"Sign-in could not start: {reason}")
    tmux = shutil.which("tmux")
    if tmux is None:
        raise ValueError("tmux is not available")
    os.execv(tmux, ["tmux", "-S", binding.tmux_socket, "attach", "-t", response.json()["session"]])
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
        from agent_backbone.cli._profile_services import ProviderServices

        registry = ProfileRegistry.create(path)
        operations = ProfileOperations(registry, services=ProviderServices(registry))
        result = operations.apply(request)
    else:
        operations = ProfileOperations(ProfileRegistry(path))
        result = operations.lookup(args.request_id)
    if args.json_output:
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
