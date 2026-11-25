---
name: messaging-and-social-operations
description: "Use when operating communication and social platforms from Hermes: Telegram bots, Telegram gateway behavior, email, X/Twitter, Yuanbao groups, and outbound-message safety."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [messaging, telegram, email, twitter, x, yuanbao, bots]
    related_skills: [productivity-workspace-automation]
---

# Messaging and Social Operations

## Overview

This umbrella covers user-visible communications: bot configuration, gateway behavior, email, social posting/search/DMs, and group chat operations. Treat all sends and external posts as side effects requiring target verification.

## When to Use

- Operate Telegram bots via Bot API, command menus, webhook/polling checks.
- Troubleshoot Hermes-on-Telegram gateway voice/STT and slash-command behavior.
- Send/search/manage email from CLI.
- Post/search/DM through X/Twitter tooling.
- Mention or query users/groups in Yuanbao.

## Operating Pattern

1. Resolve platform, account/bot, channel/thread/recipient, and permissions.
2. Read current state before mutation when possible.
3. Avoid exposing tokens, private thread content, or personal data in logs.
4. Send/update only after ambiguity is resolved.
5. Verify by reading platform state, delivery result, or API response.

## Sub-workflows

### Telegram bots and gateway
Separate Bot API operations from Hermes gateway behavior. Check webhook/polling state, command menus, voice/STT configuration, and gateway logs as appropriate.

### Email
Resolve mailbox, thread, recipient, subject, and attachments before sending; verify sent state.

### Social posting
Search and draft before posting; verify post URL/id and account identity.

### Group chat operations
For Yuanbao or similar group platforms, verify group and mention targets before sending.

## Common Pitfalls

1. Never log or repeat bot tokens/API secrets.
2. Channel/thread IDs matter; platform-only targets may go to the wrong place.
3. Do not treat an API 200 as human-visible delivery unless the platform state confirms it.

## Verification Checklist

- [ ] Target platform/account/channel/recipient verified.
- [ ] Secret values were not exposed.
- [ ] Resulting message/post/config state was read back.
