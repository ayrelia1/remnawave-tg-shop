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

Run one scheduler. It picks up pending alerts every five seconds, in bounded
batches, and sends an admin notice for every alert. Under **Admin → System
functions → Torrent reports**, automatic action defaults to off and the
threshold defaults to `medium`. The worker checks the current policy for each
new decision and again before requesting a subscription disable. Warnings and
cooldown use the receipt time of counted alerts for that panel account.

Personal warnings are delivered independently of the admin topic. A third
violation within five days of the first can disable the subscription after
both warning deliveries finish. A permanent Telegram rejection finishes a
delivery attempt. A pending disable expires five days after the third alert
was received. Confirmed panel disables are recorded locally.

Failed reports stay in the inbox and are eligible for retry after a short
in-memory delay. The scheduler can advance other reports during that delay.

Disable the environment flag to pause intake and pending work. The collector
can also pause deliveries with `delivery.url: null`. The native Remnawave
torrent blocker continues to use `/webhook/panel`.
