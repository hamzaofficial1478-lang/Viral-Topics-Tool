"""YouTube Shorts downloader — a second tool alongside the long-video clip maker.

Save channels, give each one a count, and download that many of its newest
Shorts per run at the best quality YouTube offers — never the same Short twice,
each with its title, description and hashtags written next to it, and a history
of everything downloaded.

It shares the program's basics rather than re-implementing them: the same
YouTube download path (cookies, player-client rotation, format fallback), the
same keep-awake and phone notifications, and the same crash-safe state files.

Modules:
    store   — channels + settings, the download history (the no-repeat archive),
              and the current run's plan. Everything on disk, written atomically.
    youtube — list a channel's Shorts, download one, build its metadata.
    worker  — work through a run: survives sleep, closing the window and power
              cuts, pauses on request, reports to the phone.
"""
