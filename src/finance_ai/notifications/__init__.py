"""Email notifications: templates, senders, the outbox and its dispatcher (EML-01..EML-03).

Detecting that something happened (a budget threshold was crossed) is kept apart from delivering
it (an email), so the same rules can later feed another channel.
"""
