# Brand asset generator

The integration's icon/logo PNGs live in [`custom_components/tuxedo_touch/brand/`](../custom_components/tuxedo_touch/brand/), where Home Assistant 2026.3+'s Brands Proxy API serves them directly - local brand images take priority over the brands CDN with no configuration, so no home-assistant/brands submission is needed (that repo stopped accepting custom-integration PRs in Feb 2026). On older HA versions the folder is simply inert.

The approved sculpted icon is [`icon-source.png`](icon-source.png). [`generate.py`](generate.py) scales it for Home Assistant and draws the wide logos. `dark_logo*.png` are the dark-theme variants. Run `python generate.py` for a contact sheet in `preview/`, then `python generate.py --final grid` to regenerate the shipped PNGs.
