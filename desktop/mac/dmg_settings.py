# dmgbuild settings for Harvest-mac.dmg:
#   dmgbuild -s dmg_settings.py -D app=path/to/Harvest.app -D art=path/to/desktop/mac Harvest Harvest-mac.dmg
# (dmgbuild does not tell this file where it lives, so the art folder is passed in.)
import os

app = defines.get("app", "Harvest.app")  # noqa: F821 (provided by dmgbuild)
here = defines.get("art", ".")  # noqa: F821

format = "UDZO"
size = None
files = [app]
symlinks = {"Applications": "/Applications"}
icon = os.path.join(here, "Harvest.icns")  # the disk's own icon
badge_icon = None
background = os.path.join(here, "dmg_background.tiff")
window_rect = ((200, 140), (660, 420))
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
icon_size = 128
text_size = 13
icon_locations = {"Harvest.app": (180, 220), "Applications": (480, 220)}  # must match make_dmg_background.py
