# omp history retention: measured evidence

Recorded 2026-10-27 against omp 18.11.3, `anthropic/claude-haiku-4-5`, omp-rpc pinned at
`71c5eec978b0e7ce9ff057eb4e311f67f4f03eb9`, Ember `8bebc82`/`a32cdd2`. One turn each, through
`prompt_and_wait` on the real wire (prompt: 900 words on the history of the transistor, no tools),
history memory measured with `tracemalloc` across the turn.

| client | message updates | history | retained events | reply text |
| --- | --- | --- | --- | --- |
| `omp_rpc.RpcClient` | 762 | 19.55 MiB | all, each with the whole message so far | intact |
| `LeanRpcClient` | 707 | 0.16 MiB | 8 (31 KB estimated) | intact |

Event kinds returned by `prompt_and_wait` were the same except `MessageUpdateEvent`, which `LeanRpcClient`
does not archive (a listener heard every update, with its full content). Scraped through `observability.render()`
while the client ran: `annealage_omp_sessions 1`, `child_rss_bytes` ~379 MB, `child_cpu_seconds` 4.2,
`rpc_retained_events 8`, `rpc_listeners 1`, `rpc_pending_requests 0`. After `stop()`: no child, 0 retained, 0 listeners.

Synthetic shape (`_append_event` only, 4,000 updates of a growing 16-character-chunk message): stock 129 MiB, lean ~0.2 MiB.
A turn of 10,500 updates completes with `LeanRpcClient` and raises "Event history limit was exceeded" with `RpcClient`
(`tests/test_rpc_client.py`).

## Not measured, on purpose

- **Bytes received from omp.** omp-rpc decodes each stdout line inside `_read_stdout`, which gives a subclass no hook
  for its length; reimplementing the reader to count it was declined. `annealage_omp_rpc_retained_estimated_bytes` is the
  UTF-8 length of the compact JSON of what the history holds, not what omp sent.
- **The current turn's bytes or token count so far.** Nothing here reads a turn in progress; completed turns are counted
  (`annealage_omp_turns_total`, `annealage_omp_turn_seconds`).
- A real server `/metrics` scrape under load, the WebSocket gauge, a real cancelled turn's counter, and the hooks' CPU cost
  were not exercised in this smoke.
