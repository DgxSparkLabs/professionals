# Messaging apps

Status: paper. No account is created, no login is performed, and nothing is sent.

The five professionals are already in the marketplace. This note records, for each app, what that professional covers, which account a later study may use, and what stays forbidden. Names, addresses, hosts and secrets stay out of this repository. Private runbooks stay outside it.

## Shared

- A later study uses a dedicated account the operator owns. It never uses the person's own accounts, sessions or browsers.
- Before any new account, check the operator vault and reuse an existing operator account.
- No message, invite, comment or connection is sent as the person.
- Outreach, applications and recruiter replies stay off while that work is paused. A study login does not resume it.
- Each professional has no send tool. A real send is a separate decision: that text, that recipient, approved first.
- Official routes are preferred. Unofficial clients are named as account risks and are not used for bulk or cold contact.

## WhatsApp

Professional: `whatsapp-engineer`.

What exists: the skill compares the official business API, a linked-device client and the website. It requires a consent gate, a test number first, and a session store kept out of the repository. It has no send tool.

Login, later: the operator's own number, as a linked device with a descriptive name, or the official API on a test number. The person's website session is not opened.

Must not: read or answer the person's chats, reply to recruiters, change two-step or privacy settings, or send without approval of that message.

## Telegram

Professional: `telegram-engineer`.

What exists: the skill chooses a bot or a user-account client, treats a session file as a credential, and obeys flood waits. It has no send tool.

Login, later: an operator bot, or the operator user account (application id, login code, two-step password). A user client that already places calls stays that operator account. The person's website session is not opened.

Must not: read the person's chats, message or join as the person, or ignore a flood wait.

## LinkedIn

Professional: `linkedin-messaging-engineer`.

What exists: the skill states there is no public inbox API for an individual, and that driving the website can restrict the account. Quotas, pacing and a stop after throttling are in the skill. It has no send tool.

Login, later: only a dedicated operator account, and only after outreach is allowed again. None is created in this pass. The person's profile is not opened.

Must not: invitations, paid messages, comments, applications or inbox replies as the person. No password or profile change.

## Instagram

Professional: `instagram-messaging-engineer`.

What exists: the skill. The supported route is the platform messaging API for a professional account, with review before anyone outside testers can be reached. Unofficial clients are an account risk. It has no send tool.

Login, later: an operator account already kept for looking, if the vault has one, stays read-only until a messaging study is approved. A person's follow export is private and is not copied here. The person's account is not opened.

Must not: direct messages, follows or engagement as the person, or a new developer app in this pass.

## Facebook Messenger

Professional: `facebook-messenger-engineer`.

What exists: the skill. The supported route is a page the operator administers. A personal account has no supported automation route. It has no send tool.

Login, later: none is on record for an operator page or developer app. A live session was not opened. Creating one waits. The person's account is not opened.

Must not: automate personal messages, or send as the person.

## Order

1. This paper. It is done when this file is linked from the spec sheet and a publish check of this file is clean.
2. No login and no account creation in this pass.
3. A later study is one app at a time, vault first, operator account only, read-only first.
4. Conversation products for WhatsApp and Telegram stay in the private specs. This repository keeps the generic skills.
5. LinkedIn, Instagram and Facebook stay skills-only until outreach is allowed and an account is approved.

