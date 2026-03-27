#!/usr/bin/env python3
"""Copy credential placeholders from source instances to target instance."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from n8n_common import (
    SyncError,
    InstanceConfig,
    http_json_request,
    join_url,
    load_config,
    write_json,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Copy credential placeholders from source to target.")
    parser.add_argument("--source", required=True, help="Source instance alias")
    parser.add_argument("--target", default="primary", help="Target instance alias")
    parser.add_argument("--dry-run", action="store_true", help="Show without creating")
    parser.add_argument("--output-report-path", help="Path to save JSON report")
    parser.add_argument("--dotenv", default="secrets/.env.n8n")
    parser.add_argument("--repo-root", default=".")
    return parser.parse_args()


def get_instances_extended(config):
    instances = {}
    alias_prefixes = {
        "primary": "N8N_PRIMARY",
        "secondary": "N8N_SECONDARY",
        "tertiary": "N8N_TERTIARY",
    }
    for alias, prefix in alias_prefixes.items():
        base_url = config.get(prefix + "_BASE_URL", "").strip().rstrip("/")
        api_key = config.get(prefix + "_API_KEY", "").strip()
        if base_url and api_key:
            instances[alias] = InstanceConfig(alias=alias, base_url=base_url, api_key=api_key)
    if not instances:
        raise SyncError("No n8n instances configured.")
    return instances


def list_credentials(instance):
    url = join_url(instance.base_url, "/api/v1/credentials")
    response = http_json_request("GET", url, instance.api_key)
    if isinstance(response.get("data"), list):
        return response["data"]
    if isinstance(response, list):
        return response
    return []


def get_credential_schema(instance, cred_type):
    url = join_url(instance.base_url, "/api/v1/credentials/schema/" + cred_type)
    return http_json_request("GET", url, instance.api_key)


def build_placeholder_data(schema, cred_type):
    data = {}
    all_of = schema.get("allOf", [])
    if all_of:
        all_props = {}
        all_req = set()
        for sub in all_of:
            all_props.update(sub.get("properties", {}))
            all_req.update(sub.get("required", []))
        properties = all_props
        required = all_req
    else:
        properties = schema.get("properties", {})
        required = schema.get("required", [])

    templates = {
        "gmailOAuth2": {"serverUrl": "https://accounts.google.com/o/oauth2/auth", "clientId": "__PLACEHOLDER__", "clientSecret": "__PLACEHOLDER__", "sendAdditionalBodyProperties": False, "additionalBodyProperties": {}},
        "googleCalendarOAuth2Api": {"serverUrl": "https://accounts.google.com/o/oauth2/auth", "clientId": "__PLACEHOLDER__", "clientSecret": "__PLACEHOLDER__", "sendAdditionalBodyProperties": False, "additionalBodyProperties": {}},
        "googleCloudStorageOAuth2Api": {"serverUrl": "https://accounts.google.com/o/oauth2/auth", "clientId": "__PLACEHOLDER__", "clientSecret": "__PLACEHOLDER__", "sendAdditionalBodyProperties": False, "additionalBodyProperties": {}},
        "googleDocsOAuth2Api": {"serverUrl": "https://accounts.google.com/o/oauth2/auth", "clientId": "__PLACEHOLDER__", "clientSecret": "__PLACEHOLDER__", "sendAdditionalBodyProperties": False, "additionalBodyProperties": {}},
        "googleDriveOAuth2Api": {"serverUrl": "https://accounts.google.com/o/oauth2/auth", "clientId": "__PLACEHOLDER__", "clientSecret": "__PLACEHOLDER__", "sendAdditionalBodyProperties": False, "additionalBodyProperties": {}},
        "googleOAuth2Api": {"serverUrl": "https://accounts.google.com/o/oauth2/auth", "clientId": "__PLACEHOLDER__", "clientSecret": "__PLACEHOLDER__", "scope": "openid profile email", "sendAdditionalBodyProperties": False, "additionalBodyProperties": {}},
        "googleSheetsOAuth2Api": {"serverUrl": "https://accounts.google.com/o/oauth2/auth", "clientId": "__PLACEHOLDER__", "clientSecret": "__PLACEHOLDER__", "sendAdditionalBodyProperties": False, "additionalBodyProperties": {}},
        "zohoOAuth2Api": {"serverUrl": "https://accounts.zoho.com/oauth/v2/auth", "authUrl": "https://accounts.zoho.com/oauth/v2/auth", "accessTokenUrl": "https://accounts.zoho.com/oauth/v2/token", "clientId": "__PLACEHOLDER__", "clientSecret": "__PLACEHOLDER__", "sendAdditionalBodyProperties": False, "additionalBodyProperties": {}},
        "openAiApi": {"headerName": "Authorization", "headerValue": "Bearer __PLACEHOLDER__"},
        "aws": {"accessKeyId": "__PLACEHOLDER__", "secretAccessKey": "__PLACEHOLDER__", "sessionToken": "__PLACEHOLDER__", "s3Endpoint": "https://s3.amazonaws.com", "lambdaEndpoint": "https://lambda.us-east-1.amazonaws.com", "snsEndpoint": "https://sns.us-east-1.amazonaws.com", "sesEndpoint": "https://email.us-east-1.amazonaws.com", "sqsEndpoint": "https://sqs.us-east-1.amazonaws.com", "ssmEndpoint": "https://ssm.us-east-1.amazonaws.com", "rekognitionEndpoint": "https://rekognition.us-east-1.amazonaws.com"},
    }

    if cred_type in templates:
        return templates[cred_type]

    for name, ps in properties.items():
        if name in required:
            default = ps.get("default")
            if default is not None:
                data[name] = default
            elif ps.get("type") == "string":
                enum = ps.get("enum")
                data[name] = enum[0] if enum else "__PLACEHOLDER__"
            elif ps.get("type") == "number":
                data[name] = 0
            elif ps.get("type") == "boolean":
                data[name] = False
            elif ps.get("type") == "array":
                data[name] = []
            elif ps.get("type") == "object":
                data[name] = {}
            else:
                data[name] = None
    return data


def create_credential(instance, cred_type, cred_name, data):
    url = join_url(instance.base_url, "/api/v1/credentials")
    return http_json_request("POST", url, instance.api_key, payload={"name": cred_name, "type": cred_type, "data": data})


def main():
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    config = load_config(repo_root, dotenv_relpath=args.dotenv)
    instances = get_instances_extended(config)

    print("=" * 70)
    print("CREDENTIAL COPY UTILITY")
    print("=" * 70)
    print("")
    print("[!] Copies PLACEHOLDERS only - fill values manually on target.")
    print("")

    if args.source not in instances:
        raise SyncError("Source instance not configured: " + args.source)
    if args.target not in instances:
        raise SyncError("Target instance not configured: " + args.target)

    source = instances[args.source]
    target = instances[args.target]

    print("Source: " + args.source)
    print("Target: " + args.target)
    print("Mode:   " + ("DRY RUN" if args.dry_run else "CREATE"))
    print("")

    source_creds = list_credentials(source)
    print("Found " + str(len(source_creds)) + " credentials on source")

    target_creds = list_credentials(target)
    target_names = {c.get("name"): c for c in target_creds}
    print("Found " + str(len(target_creds)) + " existing on target")
    print("")

    created, skipped, failed, manual = [], [], [], []

    for cred in sorted(source_creds, key=lambda c: (c.get("type", ""), c.get("name", ""))):
        name = cred.get("name", "")
        ctype = cred.get("type", "")
        if not name or not ctype:
            continue

        if name in target_names and target_names[name].get("type") == ctype:
            print("  [-] skip " + ctype + ": " + name)
            skipped.append({"credentialType": ctype, "credentialName": name})
            continue

        print("  [+] create " + ctype + ": " + name)

        try:
            schema = get_credential_schema(target, ctype)
        except SyncError as e:
            print("    [!] Schema error: " + str(e))
            manual.append({"credentialType": ctype, "credentialName": name, "reason": str(e)})
            continue

        try:
            data = build_placeholder_data(schema, ctype)
        except Exception as e:
            print("    [!] Data error: " + str(e))
            manual.append({"credentialType": ctype, "credentialName": name, "reason": str(e)})
            continue

        if not args.dry_run:
            try:
                result = create_credential(target, ctype, name, data)
                created.append({"credentialType": ctype, "credentialName": name, "id": result.get("id")})
                print("    [+] created id=" + str(result.get("id")))
            except SyncError as e:
                print("    [!] Failed: " + str(e))
                failed.append({"credentialType": ctype, "credentialName": name, "error": str(e)})
        else:
            created.append({"credentialType": ctype, "credentialName": name, "id": None})
            print("    (dry-run)")

    report = {
        "sourceInstance": args.source,
        "targetInstance": args.target,
        "sourceCredentialsFound": len(source_creds),
        "targetExistingCredentials": len(target_creds),
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "manualActionRequired": manual,
    }

    if args.output_report_path:
        write_json(Path(args.output_report_path), report)
        print("\n[+] Report saved")

    print("\n=== Summary ===")
    print("Source creds: " + str(report["sourceCredentialsFound"]))
    print("Target existing: " + str(report["targetExistingCredentials"]))
    print("Created: " + str(len(created)))
    print("Skipped: " + str(len(skipped)))
    print("Failed: " + str(len(failed)))
    print("Manual: " + str(len(manual)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as e:
        print("error: " + str(e), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
