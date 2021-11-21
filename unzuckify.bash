#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PATH="${SCRIPT_DIR}/unzuckify.bash"

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    export SAVED_WD="${PWD}"
    cd "${SCRIPT_DIR}"
    exec poetry run "${SCRIPT_PATH}" "$@"
fi

if [[ -n "${SAVED_WD:-}" ]]; then
    cd "${SAVED_WD}"
    unset SAVED_WD
fi

if ! diff -q "${SCRIPT_DIR}/poetry.lock" "${VIRTUAL_ENV}/poetry.lock" &>/dev/null; then
    (
        cd "${SCRIPT_DIR}"
        poetry install
        cp "${SCRIPT_DIR}/poetry.lock" "${VIRTUAL_ENV}/poetry.lock"
    )
fi

source "${SCRIPT_DIR}/.env"

function unzuckify {
    "${SCRIPT_DIR}/unzuckify.py" "$@"
}

inbox="$(unzuckify -u "${PRIMARY_EMAIL}" -p "${PRIMARY_PASSWORD}" inbox)"

read -r -d "" sendgrid_request_template <<"EOF" || :
{
  personalizations: [
    {
      to: [
        {
          email: env.TO_EMAIL
        }
      ]
    }
  ],
  from: {
    email: env.FROM_EMAIL,
    name: "Messenger"
  },
  subject: env.SUBJECT,
  content: [
    {
      type: "text/plain",
      value: env.CONTENT
    }
  ]
}
EOF

prelim='.users as $users | .conversations | to_entries | map(.key as $cid | .value | select(.unread)'
cname='if .group_name then .group_name elif .participants | length == 1 then $users["\(.participants[0])"].name else .participants | map($users["\(.)"].name | split(" ")[0]) | join(", ") end'

for category in main sentinel; do

    case "${category}" in
        main)
            email="${TO_EMAIL}"
            filter='select($users[$cid].name != env.SENTINEL_NAME)'
            ;;
        sentinel)
            email="${SENTINEL_EMAIL}"
            filter='select($users[$cid].name == env.SENTINEL_NAME)'
            ;;
    esac

    content="$(printf '%s\n' "${inbox}" | SENTINEL_NAME="${SENTINEL_NAME}" jq "${prelim} | ${filter}"' | "[\('"${cname}"')] \(.last_message)\n@ https://www.messenger.com/t/\($cid)") | join("\n\n")' -r)"
    subject="$(printf '%s\n' "${inbox}" | SENTINEL_NAME="${SENTINEL_NAME}" jq "${prelim} | ${filter}"' | "\('"${cname}"')") | join("; ") | "Message(s) from \(.)"' -r)"

    if [[ -z "${content}" ]]; then
        continue
    fi

    sendgrid_request="$(FROM_EMAIL="${FROM_EMAIL}" TO_EMAIL="${email}" SUBJECT="${subject}" CONTENT="${content}" jq -n "${sendgrid_request_template}")"

    curl -X POST https://api.sendgrid.com/v3/mail/send \
         --header "Authorization: Bearer ${SENDGRID_API_KEY}" \
         --header "Content-Type: application/json" \
         --data "${sendgrid_request}"

done

my_user_id="$(printf '%s\n' "${inbox}" | jq '.users | to_entries | .[] | select(.value.is_me) | .key' -r)"

args=()
while read -r thread; do
    args=(-t "${thread}")
done < <(printf '%s\n' "${inbox}" | jq '.conversations | to_entries | .[] | select(.value.unread) | .key' -r)

if (( "${#args[@]}" > 0 )); then
    unzuckify -u "${PRIMARY_EMAIL}" -p "${PRIMARY_PASSWORD}" read "${args[@]}"
fi

inspirational_quote="$(curl -s https://zenquotes.io/api/random | jq '.[0].q' -r)"

unzuckify -u "${SECONDARY_EMAIL}" -p "${SECONDARY_PASSWORD}" send \
          -t "${my_user_id}" -m "Your inspirational quote of the day: \"${inspirational_quote}\""
