"""Bounded executors used by the ASGI server.

Security screening and Talk report construction deliberately use different
workers.  A Talk worker may synchronously wait for screening, so sharing one
single-worker executor would deadlock.
"""

from concurrent.futures import ThreadPoolExecutor


SECURITY_WORK_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="zendesk-security",
)

TALK_ANALYTICS_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="zendesk-talk-analytics",
)
