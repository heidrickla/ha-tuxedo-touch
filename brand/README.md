# Brand assets

Source images for a [home-assistant/brands](https://github.com/home-assistant/brands)
submission (`custom_integrations/tuxedo_touch/`). Home Assistant loads integration
icons/logos exclusively from brands.home-assistant.io - it never reads them from the
integration directory, which is why these live outside `custom_components/` (so HACS
doesn't ship dead weight to every install).

The art is a stylized front view of the actual device - the Tuxedo Touch's
landscape touchscreen with its green "ready" banner and round home-screen app
icons - drawn programmatically by [generate.py](generate.py) (Pillow only,
rendered at 4x and downsampled). `dark_logo*.png` are the dark-theme variants
(white wordmark). To tweak, edit the palette/geometry in the script, review
with `python generate.py` (writes a contact sheet to `preview/`), then run
`python generate.py --final grid`.
