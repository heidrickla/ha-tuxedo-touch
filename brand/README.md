# Brand assets

Source images for a [home-assistant/brands](https://github.com/home-assistant/brands)
submission (`custom_integrations/tuxedo_touch/`). Home Assistant loads integration
icons/logos exclusively from brands.home-assistant.io - it never reads them from the
integration directory, which is why these live outside `custom_components/` (so HACS
doesn't ship dead weight to every install).
