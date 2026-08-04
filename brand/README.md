# Brand asset generator

The integration's icon/logo PNGs live in
[`custom_components/tuxedo_touch/brand/`](../custom_components/tuxedo_touch/brand/),
where Home Assistant 2026.3+'s Brands Proxy API serves them directly - local
brand images take priority over the brands CDN with no configuration, so no
home-assistant/brands submission is needed (that repo stopped accepting
custom-integration PRs in Feb 2026). On older HA versions the folder is
simply inert.

This directory holds only the tooling: the art is a stylized front view of
the actual device - the Tuxedo Touch's landscape touchscreen with its green
"ready" banner and round home-screen app icons - drawn programmatically by
[generate.py](generate.py) (Pillow only, rendered at 4x and downsampled).
`dark_logo*.png` are the dark-theme variants (white wordmark). To tweak,
edit the palette/geometry in the script, review with `python generate.py`
(writes a contact sheet to `preview/`), then run
`python generate.py --final grid` to regenerate the shipped PNGs.
