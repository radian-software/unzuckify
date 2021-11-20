#!/usr/bin/env python3

import argparse
import asyncio
import collections
import concurrent.futures
import json
import re
import sys

import esprima
import requests
import xdg


def get_unauthenticated_page_data(session):
    page = session.get("https://www.messenger.com", allow_redirects=False)
    page.raise_for_status()
    return {
        "datr": re.search(r'"_js_datr",\s*"([^"]+)"', page.text).group(1),
        "lsd": re.search(r'name="lsd"\s+value="([^"]+)"', page.text).group(1),
        "initial_request_id": re.search(
            r'name="initial_request_id"\s+value="([^"]+)"', page.text
        ).group(1),
    }


def do_login(session, unauthenticated_page_data, credentials):
    resp = session.post(
        "https://www.messenger.com/login/password/",
        cookies={"datr": unauthenticated_page_data["datr"]},
        data={
            "lsd": unauthenticated_page_data["lsd"],
            "initial_request_id": unauthenticated_page_data["initial_request_id"],
            "email": credentials["email"],
            "pass": credentials["password"],
            "login": "1",
            "persistent": "1",
        },
        allow_redirects=False,
    )
    resp.raise_for_status()


def get_chat_page_data(session):
    redirect = session.get(
        "https://www.messenger.com",
        headers={
            "Sec-Fetch-Site": "same-origin",
        },
        allow_redirects=False,
    )
    redirect.raise_for_status()
    assert redirect.status_code in (301, 302), redirect.status
    page = session.get(redirect.headers["Location"])
    page.raise_for_status()
    return {
        "device_id": re.search(r'"deviceId"\s*:\s*"([^"]+)"', page.text).group(1),
        "schema_version": re.search(
            r'"schemaVersion"\s*:\s*"([^"]+)"', page.text
        ).group(1),
        "dtsg": re.search(r'DTSG.{,20}"token":"([^"]+)"', page.text).group(1),
        "scripts": [*re.findall(r'"([^"]+rsrc\.php/[^"]+\.js[^"]+)"', page.text)],
    }


def get_script_data(session, chat_page_data):
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        loop = asyncio.get_event_loop()
        scripts = loop.run_until_complete(
            asyncio.gather(
                *(
                    loop.run_in_executor(executor, requests.get, url)
                    for url in chat_page_data["scripts"]
                )
            )
        )
    for script in scripts:
        script.raise_for_status()
        if "LSPlatformGraphQLLightspeedRequestQuery" not in script.text:
            continue
        return {
            "query_id": re.search(
                r'id:\s*"([0-9]+)".{,50}name:\s*"LSPlatformGraphQLLightspeedRequestQuery"',
                script.text,
            ).group(1)
        }
    assert False, "no script had LSPlatformGraphQLLightspeedRequestQuery"


def node_to_literal(node):
    if node.type == "Literal":
        return node.value
    if node.type == "ArrayExpression":
        return tuple(node_to_literal(elt) for elt in node.elements)
    if node.type == "Identifier" and node.name == "U":
        return None
    if node.type == "UnaryExpression" and node.prefix and node.operator == "-":
        return -node_to_literal(node.argument)
    return f"<{node.type}>"


def read_lightspeed_call(node):
    if not (
        node.type == "CallExpression"
        and node.callee.type == "MemberExpression"
        and node.callee.object.type == "Identifier"
        and node.callee.object.name == "LS"
        and node.callee.property.type == "Identifier"
        and node.callee.property.name == "sp"
    ):
        return None
    return [node_to_literal(node) for node in node.arguments]


def get_inbox_js(session, chat_page_data, script_data):
    graph = session.post(
        "https://www.messenger.com/api/graphql/",
        data={
            "doc_id": script_data["query_id"],
            "fb_dtsg": chat_page_data["dtsg"],
            "variables": json.dumps(
                {
                    "deviceId": chat_page_data["device_id"],
                    "requestId": 0,
                    "requestPayload": json.dumps(
                        {
                            "database": 1,
                            "version": chat_page_data["schema_version"],
                            "sync_params": json.dumps({}),
                        }
                    ),
                    "requestType": 1,
                }
            ),
        },
    )
    graph.raise_for_status()
    return graph.json()["data"]["viewer"]["lightspeed_web_request"]["payload"]


def get_inbox_data(inbox_js):
    lightspeed_calls = collections.defaultdict(list)

    def delegate(node, meta):
        if not (args := read_lightspeed_call(node)):
            return
        (fn, *args) = args
        lightspeed_calls[fn].append(args)

    esprima.parseScript(inbox_js, delegate=delegate)

    users = {}
    conversations = {}

    for args in lightspeed_calls["deleteThenInsertThread"]:
        last_sent_ts, last_read_ts, last_msg, group_name, *rest = args
        thread_id, last_msg_author = [
            arg for arg in rest if isinstance(arg, tuple) and arg[0] > 0
        ]
        conversations[thread_id] = {
            "unread": last_sent_ts != last_read_ts,
            "last_message": last_msg,
            "last_message_author": last_msg_author,
            "group_name": group_name,
            "participants": set(),
        }

    for args in lightspeed_calls["addParticipantIdToGroupThread"]:
        thread_id, user_id, *rest = args
        conversations[thread_id]["participants"].add(user_id)

    for args in lightspeed_calls["verifyContactRowExists"]:
        user_id, _, _, name, *rest = args
        _, _, _, is_me = [arg for arg in rest if isinstance(arg, bool)]
        users[user_id] = {"name": name, "is_me": is_me}

    for user_id in users:
        if all(user_id in c["participants"] for c in conversations.values()):
            my_user_id = user_id
            break

    my_user_ids = [uid for uid in users if users[uid]["is_me"]]
    assert len(my_user_ids) == 1
    (my_user_id,) = my_user_ids

    for conversation in conversations.values():
        conversation["participants"].remove(my_user_id)

    return {
        "users": users,
        "conversations": conversations,
    }


def do_main(credentials):
    with requests.session() as session:
        unauthenticated_page_data = get_unauthenticated_page_data(session)
        print(unauthenticated_page_data)
        do_login(session, unauthenticated_page_data, credentials)
        print(dict(session.cookies))
        chat_page_data = get_chat_page_data(session)
        scripts = chat_page_data["scripts"]
        print(
            {
                **chat_page_data,
                "scripts": [scripts[0], f"... omitted {len(scripts) - 1} more ..."],
            }
        )
        script_data = get_script_data(session, chat_page_data)
        print(script_data)
        inbox_js = get_inbox_js(session, chat_page_data, script_data)
        inbox_data = get_inbox_data(inbox_js)
        print(inbox_data)


def main():
    parser = argparse.ArgumentParser("unzuckify")
    parser.add_argument("email")
    parser.add_argument("password")
    args = parser.parse_args()
    do_main({"email": args.email, "password": args.password})


if __name__ == "__main__":
    main()
    sys.exit(0)
