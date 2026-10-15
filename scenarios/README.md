# Scenario cases

Use a fresh SQLite file per case. These are expected behaviors for future live-model proof.

1. **Ordinary intake:** Submit `fixtures/ordinary-lead.json`. Gemini should assign a route, summarize the stated need, upsert one contact and notify one team channel.
2. **Incomplete submission:** Submit `fixtures/incomplete-lead.json`. Validation should reject it before the model or connector is called.
3. **Webhook replay:** Submit the ordinary event twice, unchanged. The second response must return the stored decision without a second contact or notification.
4. **Conflicting replay:** Reuse the event ID with a changed email. Return HTTP 409 without replacing the previous contact.
5. **CRM failure:** Fail the CRM action once; retry the same event. The stored decision is reused and an upsert is repeated by unique email.
6. **Notification timeout:** Treat the outcome as uncertain and require operator review. A blind retry could duplicate the Slack message.
