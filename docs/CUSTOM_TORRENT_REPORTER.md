# Custom torrent reporter

The collector sends JSON pages to `POST /webhook/torrent` over HTTPS. Use the
same secret in the collector's `auth` field and the bot's
`CUSTOM_TORRENT_REPORTER_SECRET`. Set `CUSTOM_TORRENT_REPORTER_THREAD_ID` to the
topic in `LOG_CHAT_ID`, then enable `CUSTOM_TORRENT_REPORTER_ENABLED` in the
bot and scheduler environment. The cooldown defaults to 60 minutes.

Deploy both processes with the additive migration before pointing the collector
at the endpoint. A successful HTTP response acknowledges an entire committed
page. The collector may replay a page with the same `batch_id` and `page`.
Each alert retains the delivery key, which is `batch_id` for one page and
`<batch_id>:<page>` for multiple pages. The inbox and deduplication history
are retained in the database.

An alert may include `mode` (`inbound` or `outbound`) and `seconds` (an integer
duration). The same fields at the page level serve as defaults. The legacy
`mod` field is accepted when `mode` is absent. When the mode
and a positive duration are present, the personal notice describes a block
of the client's IP in inbound mode or of torrent peer IPs in outbound mode.
The notice uses the panel host name if the node and inbound identify a unique
host, or the node name otherwise. Older payloads continue to work.

Run one scheduler. It picks up pending alerts every five seconds, in bounded
batches, and sends an admin notice for every alert. Under **Admin → System
functions → Torrent reports**, automatic action defaults to off and the
threshold defaults to `medium`. The worker checks the current policy for each
new decision and again before starting a subscription reset.

Each series starts a fixed 24-hour window at the first counted alert's receipt
time. An alert received at the window boundary starts a new series. The
cooldown applies across series and is measured from the last counted alert
for the same panel account, including a confirmed 3/3.

Personal warnings are delivered independently of the admin topic. After both
warning deliveries finish, the third alert rotates the keys and clears the
registered devices using the shared subscription reset. A permanent Telegram
rejection finishes a delivery attempt. The reset preserves expiry, access
status and LTE balance. Once it succeeds, the next alert outside the cooldown
starts at 1/3. Alerts eligible to start a new series wait while a reset is
pending. A reset that has not yet rotated the keys expires 24 hours after the
third alert was received. If the keys already changed, the worker can finish
device cleanup and update the local subscription without rotating the keys again.

Failed reports stay in the inbox and are eligible for retry after a short
in-memory delay. The scheduler can advance other reports during that delay.

To upgrade from the disable policy, pause processing and update both
processes with the additive migration before resuming. Previously counted
warnings carry over only if the series started within the new 24-hour window.
Completed disables close their series and remain in history. Previously
queued disables can be processed as resets if they meet the new timing and
eligibility checks.

Disable the environment flag to pause intake and pending work. The collector
can also pause deliveries with `delivery.url: null`. Before rolling back,
pause upstream delivery and let pending resets finish. Rolling back the code
or the migration does not restore old links or device registrations. The
native Remnawave torrent blocker continues to use `/webhook/panel`.
