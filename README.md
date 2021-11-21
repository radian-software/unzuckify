# Unzuckify

This repository has a small Python application which allows me to
receive an email notification when somebody sends me a Facebook
message.

## Why?

I don't like Facebook as a company, and I don't want to support them
by using their products, including Messenger. However, when I came
around to this point of view, I already had a number of existing
contacts on Messenger. I migrated everyone I talked to regularly onto
other platforms, but in case someone messaged me out of the blue, I
still wanted to know about that, so I could redirect them onto Signal
or SMS.

With the help of this application, I can make sure I won't miss it
when a very old contact happens to message me on Facebook, while not
having to ever actively check Messenger or keep it on my phone.

## How?

I reverse-engineered the Messenger authentication flow and the
[recently
rewritten](https://engineering.fb.com/2020/03/02/data-infrastructure/messenger/)
GraphQL API, and created a command-line utility that can,
in the same manner as the browser:

* Fetch the list of threads that would show up on Messenger, their
  names, the users in each thread, the last message that was sent, who
  sent it, and whether it is unread according to the server.
* Send a message to a thread.
* Mark a thread as read if it is unread.

This all turned out to be fairly straightforward. The protocol and
code is obfuscated, but nowhere near enough to foil basic reverse
engineering techniques.

After creating the command-line utility, I wrote a Bash script that
wrapped it with the following logic:

1. Fetch my list of threads.
2. Identify any threads that have unread messages and use
   [SendGrid](https://sendgrid.com/) to notify me about them via
   email.
3. Mark those threads as read.
4. Login to a separate Facebook account and send my primary account a
   random inspirational quote by direct message.
5. In step 2, the notifications for messages from the account in step
   4 are sent to a separate email address that is monitored by [Dead
   Man's Snitch](https://deadmanssnitch.com/). This, combined with
   step 4, ensures that as long as everything is in working order,
   Dead Man's Snitch will get an email every time I run the script.

Then I run the script on a cron job every few hours. If I get a
message, it's forwarded to email. If the API changes out from under
me, or something else goes wrong, I also get an email because Dead
Man's Snitch will stop receiving notifications.

## Prior work

I previously used [Messenger
Mirror](https://github.com/raxod502/messenger-mirror) to accomplish
the same thing as this project. However, because Messenger Mirror
relied on having an entire Chrome instance running in Selenium 24/7, I
didn't want to have that running on my laptop (it would eat resources
for no good reason). Unfortunately, after a couple weeks, Facebook
banned the IP for my VPS, so I couldn't run the application there
anymore. This is what inspired me to try reverse engineering the
browser API directly, since if I did that, it would be far less
resource intensive to run the application on my laptop in the
background.

## Setup

If you just want to use the CLI (perhaps as proof of concept for
developing your own Messenger client using the reverse engineered
API), setup is quite simple. Install
[Poetry](https://python-poetry.org/), run `poetry install` and `poetry
shell`, then you are good to go:

```
% ./unzuckify.py -u you@example.com -p your-password -v inbox
% ./unzuckify.py -u you@example.com -p your-password -v send
    -t thread-id-from-inbox -m "Some text message"
% ./unzuckify.py -u you@example.com -p your-password -v read
    -t thread-id-from-inbox
```

Cookies are automatically cached in `~/.cache/unzuckify/cookies.json`,
and are separated per email address so you can use different accounts
in parallel. Omit `-v` to not log all the intermediate debugging info.
Only `inbox` prints to stdout, and the output is JSON.

If you additionally want to set up a Messenger-to-email bridge like I
have, then you should install [jq](https://stedolan.github.io/jq/) and
sign up for a free [SendGrid](https://sendgrid.com/) account. Also go
to [Heroku](https://heroku.com/), provision a [Dead Man's
Snitch](https://deadmanssnitch.com/) addon, and get the email endpoint
for the snitch. Then create a `.env` file in the repo as follows:

```bash
PRIMARY_EMAIL=you@example.com  # facebook login
PRIMARY_PASSWORD='your-password'

SECONDARY_EMAIL=finsta@example.com  # 2nd account login
SECONDARY_PASSWORD='other-password'

SENDGRID_API_KEY=SG.REDACTED  # from SendGrid
SENTINEL_EMAIL=some-hash@nosnch.in  # from Dead Man's Snitch
SENTINEL_NAME='John Smith'  # Facebook name of 2nd account

FROM_EMAIL=radon@intuitiveexplanations.com  # SendGrid verified sender
TO_EMAIL=radon.neon@gmail.com  # where to receive notifications
```

Note for `FROM_EMAIL`, ideally you own a domain and can prove
ownership of it, and this email is on that domain. According to the
SendGrid documentation, if you use something like a Gmail address,
your notifications are likely to get flagged by spam filters because
it can be proven that Gmail was not actually the one to send the
email, which looks suspicious. If you don't own a personal domain, may
I suggest doing business with [Namecheap](https://namecheap.com/) and
[Forward Email](https://forwardemail.net/en)?

Now you just need to set up the script to run on a semi-regular basis,
for example by creating a cron job:

```bash
crontab - <<"EOF"
0 */3 * * * sh -c '. "$HOME/.profile" && ~/dev/unzuckify/unzuckify.bash'
EOF
```
