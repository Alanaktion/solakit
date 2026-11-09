---
name: triage-inbox
summary: Check mail, report urgent items, mark handled messages read
tools: [email.list_mailboxes, email.search, email.read, email.mark_read]
---
1. email.list_mailboxes, then email.search(mailbox=<alias>, unseen_only=true, limit=20).
2. For each unread message that is not obviously bulk mail, email.read it.
3. Reply in chat with: sender, subject, a one-line gist, and whether action is needed.
4. Only mark messages read after reporting. Never reply or delete unless asked.
