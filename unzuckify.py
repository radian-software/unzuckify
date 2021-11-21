#!/usr/bin/env python3

import argparse
import asyncio
import collections
import concurrent.futures
import datetime
import json
import random
import re
import sys

import esprima
import requests
import xdg


global_config = {"verbose": False}


def log(msg):
    if global_config["verbose"]:
        print(msg, file=sys.stderr)


def get_cookies_path():
    return xdg.xdg_cache_home() / "unzuckify" / "cookies.json"


def load_cookies(session, email):
    session.cookies.clear()
    try:
        with open(get_cookies_path()) as f:
            cookies = json.load(f).get(email)
    except FileNotFoundError:
        return False
    except json.JSONDecodeError:
        return False
    if not cookies:
        return False
    session.cookies.update(cookies)
    return True


def save_cookies(session, email):
    path = get_cookies_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        f.seek(0)
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}
        data[email] = dict(session.cookies)
        f.seek(0)
        f.truncate()
        json.dump(data, f, indent=2)
        f.write("\n")


def clear_cookies(session, email):
    session.cookies.clear()
    path = get_cookies_path()
    try:
        with open(path, "a+") as f:
            f.seek(0)
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}
            try:
                data.pop(email)
            except KeyError:
                pass
            if not data:
                path.unlink()
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2)
            f.write("\n")
    except FileNotFoundError:
        pass


def get_unauthenticated_page_data(session):
    url = "https://www.messenger.com"
    log(f"[http] GET {url} (unauthenticated)")
    page = session.get(url, allow_redirects=False)
    page.raise_for_status()
    return {
        "datr": re.search(r'"_js_datr",\s*"([^"]+)"', page.text).group(1),
        "lsd": re.search(r'name="lsd"\s+value="([^"]+)"', page.text).group(1),
        "initial_request_id": re.search(
            r'name="initial_request_id"\s+value="([^"]+)"', page.text
        ).group(1),
    }


def do_login(session, unauthenticated_page_data, credentials):
    url = "https://www.messenger.com/login/password/"
    log(f"[http] POST {url}")
    resp = session.post(
        url,
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
    url = "https://www.messenger.com"
    log(f"[http] GET {url}")
    redirect = session.get(
        url,
        allow_redirects=False,
    )
    redirect.raise_for_status()
    if redirect.status_code not in (301, 302):
        return None
    url = redirect.headers["Location"]
    log(f"[http] GET {url}")
    page = session.get(url)
    page.raise_for_status()
    return {
        "device_id": re.search(r'"deviceId"\s*:\s*"([^"]+)"', page.text).group(1),
        "schema_version": re.search(
            r'"schemaVersion"\s*:\s*"([^"]+)"', page.text
        ).group(1),
        "dtsg": re.search(r'DTSG.{,20}"token":"([^"]+)"', page.text).group(1),
        "scripts": sorted(
            set(re.findall(r'"([^"]+rsrc\.php/[^"]+\.js[^"]+)"', page.text))
        ),
    }


def get_script_data(session, chat_page_data):
    def get(url):
        log(f"[http] GET {url}")
        return requests.get(url)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        loop = asyncio.get_event_loop()
        scripts = loop.run_until_complete(
            asyncio.gather(
                *(
                    loop.run_in_executor(executor, get, url)
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
        return [node_to_literal(elt) for elt in node.elements]
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
    url = "https://www.messenger.com/api/graphql/"
    log(f"[http] POST {url}")
    graph = session.post(
        url,
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


def convert_fbid(l):
    return (2 ** 32) * l[0] + l[1]


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
            arg for arg in rest if isinstance(arg, list) and arg[0] > 0
        ]
        conversations[convert_fbid(thread_id)] = {
            "unread": last_sent_ts != last_read_ts,
            "last_message": last_msg,
            "last_message_author": convert_fbid(last_msg_author),
            "group_name": group_name,
            "participants": [],
        }

    for args in lightspeed_calls["addParticipantIdToGroupThread"]:
        thread_id, user_id, *rest = args
        conversations[convert_fbid(thread_id)]["participants"].append(
            convert_fbid(user_id)
        )

    for args in lightspeed_calls["verifyContactRowExists"]:
        user_id, _, _, name, *rest = args
        _, _, _, is_me = [arg for arg in rest if isinstance(arg, bool)]
        users[convert_fbid(user_id)] = {"name": name, "is_me": is_me}

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


def interact_with_thread(session, chat_page_data, script_data, thread_id, message=None):
    url = "https://www.messenger.com/api/graphql/"
    log(f"[http] POST {url}")
    timestamp = int(datetime.datetime.now().timestamp() * 1000)
    epoch = timestamp << 22
    tasks = [
        {
            "label": "21",
            "payload": json.dumps(
                {
                    "thread_id": thread_id,
                    "last_read_watermark_ts": timestamp,
                    "sync_group": 1,
                }
            ),
            "queue_name": str(thread_id),
            "task_id": 1,
        }
    ]
    if message:
        otid = epoch + random.randrange(2 ** 22)
        tasks.insert(
            0,
            {
                "label": "46",
                "payload": json.dumps(
                    {
                        "thread_id": thread_id,
                        "otid": str(otid),
                        "source": 0,
                        "send_type": 1,
                        "text": message,
                        "initiating_source": 1,
                    }
                ),
                "queue_name": str(thread_id),
                "task_id": 0,
            },
        )
    graph = session.post(
        url,
        data={
            "doc_id": script_data["query_id"],
            "fb_dtsg": chat_page_data["dtsg"],
            "variables": json.dumps(
                {
                    "deviceId": chat_page_data["device_id"],
                    "requestId": 0,
                    "requestPayload": json.dumps(
                        {
                            "version_id": chat_page_data["schema_version"],
                            "epoch_id": epoch,
                            "tasks": tasks,
                        }
                    ),
                    "requestType": 3,
                }
            ),
        },
    )
    graph.raise_for_status()


def do_main(args):
    with requests.session() as session:
        chat_page_data = None
        if load_cookies(session, args.email):
            log(f"[cookie] READ {get_cookies_path()}")
            log(json.dumps(dict(session.cookies), indent=2))
            chat_page_data = get_chat_page_data(session)
            if not chat_page_data:
                log(f"[cookie] CLEAR due to failed auth")
                clear_cookies(session, args.email)
        if not chat_page_data:
            unauthenticated_page_data = get_unauthenticated_page_data(session)
            log(json.dumps(unauthenticated_page_data, indent=2))
            do_login(
                session,
                unauthenticated_page_data,
                {
                    "email": args.email,
                    "password": args.password,
                },
            )
            log(json.dumps(dict(session.cookies), indent=2))
            save_cookies(session, args.email)
            log(f"[cookie] WRITE {get_cookies_path()}")
            chat_page_data = get_chat_page_data(session)
            assert chat_page_data, "auth failed"
        log(
            json.dumps(
                chat_page_data,
                indent=2,
            ),
        )
        script_data = get_script_data(session, chat_page_data)
        log(json.dumps(script_data, indent=2))
        if args.cmd == "inbox":
            inbox_js = get_inbox_js(session, chat_page_data, script_data)
            inbox_data = get_inbox_data(inbox_js)
            print(json.dumps(inbox_data, indent=2 if sys.stdout.isatty() else None))
        elif args.cmd == "send":
            interact_with_thread(
                session, chat_page_data, script_data, int(args.thread), args.message
            )
        elif args.cmd == "read":
            interact_with_thread(
                session,
                chat_page_data,
                script_data,
                int(args.thread),
            )
        else:
            assert False, args.cmd


def main():
    parser = argparse.ArgumentParser("unzuckify")
    parser.add_argument("-u", "--email", required=True)
    parser.add_argument("-p", "--password", required=True)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="cmd")
    cmd_inbox = subparsers.add_parser("inbox")
    cmd_send = subparsers.add_parser("send")
    cmd_send.add_argument("-t", "--thread", required=True)
    cmd_send.add_argument("-m", "--message", required=True)
    cmd_read = subparsers.add_parser("read")
    cmd_read.add_argument("-t", "--thread", required=True)
    args = parser.parse_args()
    if args.verbose:
        global_config["verbose"] = True
    do_main(args)


if __name__ == "__main__":
    main()
    sys.exit(0)
